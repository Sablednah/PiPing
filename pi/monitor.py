#!/usr/bin/env python3
"""
monitor.py - site status panel for a 480x320 SPI LCD on a Pi 3B+.

Runs WITHOUT a desktop, drawing directly to the framebuffer via Qt's
linuxfb platform plugin. Launch with:

    QT_QPA_PLATFORM=linuxfb:fb=/dev/fb1 python3 monitor.py

Two views:
  - Grid: one row per site. Two lights (host / page) + CPU·MEM·DISK arcs.
  - Detail: tap a row to see status, history graphs, and a screenshot preview.
            Swipe to scroll, tap to go back.

Design notes for this panel:
  - 480x320, slow SPI refresh -> dark background, few colours, large targets.
  - Network I/O happens on a worker thread; the UI never blocks on a request.
"""

import sys
import os
import json
import time
import re
import threading
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QBrush, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

CONFIG_PATH  = Path(__file__).with_name("config.json")
HISTORY_PATH = Path(__file__).with_name("history.json")
HISTORY_MAX  = 240  # data points kept per site (~2 hours at 30s intervals)

# ---- palette (kept tiny: dark bg is cheap to repaint on SPI) -------------
BG       = QColor("#11141a")
PANEL    = QColor("#1b1f29")
TEXT     = QColor("#e6e9ef")
MUTED    = QColor("#7c8597")
GREEN    = QColor("#3ec46d")
AMBER    = QColor("#e7b54a")
RED      = QColor("#e1543f")
TRACK    = QColor("#2b3140")

ROW_H = 64   # fixed row height — enables scrolling regardless of site count


# ---------------------------------------------------------------------------
# History helpers (module-level, main-thread only via Qt signal delivery)
# ---------------------------------------------------------------------------
def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except Exception:
            return {}
    return {}

def save_history(history: dict):
    try:
        HISTORY_PATH.write_text(json.dumps(history))
    except Exception:
        pass

def append_history(history: dict, results: list):
    t = int(time.time())
    for r in results:
        key = r.url or r.name
        entry = {
            "t": t,
            "host_ok": r.host_ok, "page_ok": r.page_ok,
            "http": r.http_status, "ms": r.response_ms, "grep": r.grep_found,
            "cpu":  r.cpu.get("percent")  if r.cpu    else None,
            "mem":  r.memory.get("percent") if r.memory else None,
            "disk": r.disk.get("percent")  if r.disk   else None,
        }
        bucket = history.setdefault(key, [])
        bucket.append(entry)
        if len(bucket) > HISTORY_MAX:
            del bucket[:-HISTORY_MAX]
    save_history(history)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class SiteResult:
    name: str
    url: str = ""
    host_ok: bool = False        # agent reachable OR url reachable at all
    page_ok: bool = False        # HTTP 200 AND grep matched (if grep set)
    http_status: int = 0
    response_ms: int = 0
    title: str = ""
    grep_found: bool | None = None   # None = no grep configured
    grep_phrase: str = ""
    cpu: dict | None = None
    memory: dict | None = None
    disk: dict | None = None
    error: str = ""
    has_agent: bool = False


def _status_priority(r: SiteResult) -> int:
    """Sort key: 0=red, 1=amber, 2=green. Dots + CPU/MEM only (disk excluded)."""
    if not r.host_ok or not r.page_ok:
        return 0
    cpu_pct = r.cpu.get("percent") if r.cpu else 0
    mem_pct = r.memory.get("percent") if r.memory else 0
    worst = max(cpu_pct or 0, mem_pct or 0)
    if worst >= 90:
        return 0
    if worst >= 70:
        return 1
    return 2


def _gauge_col(pct):
    return GREEN if pct < 70 else (AMBER if pct < 90 else RED)


# ---------------------------------------------------------------------------
# Worker thread: does all network I/O, emits results back to the UI
# ---------------------------------------------------------------------------
class Poller(QThread):
    updated = pyqtSignal(list)   # list[SiteResult]

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._running = True
        self._force = threading.Event()

    def stop(self):
        self._running = False
        self._force.set()

    def force_poll(self):
        self._force.set()

    def run(self):
        interval = self.config.get("poll_interval_seconds", 30)
        sites = self.config.get("sites", [])
        while self._running:
            self._force.clear()
            with ThreadPoolExecutor(max_workers=len(sites) or 1) as ex:
                futures = [ex.submit(self.check_site, s) for s in sites]
                results = [f.result() for f in futures]
            self.updated.emit(results)
            for _ in range(interval * 2):
                if not self._running or self._force.is_set():
                    break
                time.sleep(0.5)

    # ---- individual checks ----------------------------------------------
    def check_site(self, site) -> SiteResult:
        name   = site.get("name", "?")
        url    = site.get("url", "")
        agent  = site.get("agent")
        grep   = site.get("grep", "") or ""
        token  = self.config.get("agent_token", "")
        tout   = self.config.get("http_timeout_seconds", 10)

        r = SiteResult(name=name, url=url, grep_phrase=grep, has_agent=bool(agent))

        # --- outsider HTTP check (what a visitor sees) ---
        if url:
            try:
                start = time.time()
                req = urllib.request.Request(url, headers={"User-Agent": "PiStatusPanel/1.0"})
                with urllib.request.urlopen(req, timeout=tout) as resp:
                    body = resp.read(524288).decode("utf-8", "ignore")
                    r.http_status = resp.status
                    r.response_ms = int((time.time() - start) * 1000)
                    r.host_ok = True
                    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                    if m:
                        r.title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
                    if grep:
                        r.grep_found = grep in body
                    r.page_ok = (resp.status == 200) and (r.grep_found is not False)
            except urllib.error.HTTPError as e:
                r.host_ok = True
                r.http_status = e.code
                r.error = f"HTTP {e.code}"
            except Exception as e:
                r.error = type(e).__name__
                r.host_ok = False

        # --- agent stats (server internals) ---
        if agent:
            try:
                req = urllib.request.Request(agent, headers={
                    "X-Status-Token": token,
                    "User-Agent": "PiStatusPanel/1.0",
                    "Referer": url or agent,
                })
                with urllib.request.urlopen(req, timeout=tout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    r.cpu    = data.get("cpu")
                    r.memory = data.get("memory")
                    r.disk   = data.get("disk")
            except Exception as e:
                if not r.error:
                    r.error = f"agent:{type(e).__name__}"

        return r


# ---------------------------------------------------------------------------
# Thumbnail fetcher: fires once per site URL on first detail-view open
# ---------------------------------------------------------------------------
class ThumbnailFetcher(QThread):
    ready = pyqtSignal(str, bytes)   # site_url, raw image bytes (b"" = failed)

    def __init__(self, site_url, api_token):
        super().__init__()
        self.site_url = site_url
        self.api_token = api_token

    def run(self):
        try:
            api_url = (
                "https://shot.screenshotapi.net/screenshot"
                f"?token={self.api_token}"
                f"&url={urllib.parse.quote(self.site_url, safe='')}"
                "&output=image&file_type=png"
            )
            print(f"[thumb] fetching {self.site_url}", flush=True)
            req = urllib.request.Request(api_url, headers={"User-Agent": "PiStatusPanel/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
            print(f"[thumb] {len(data)} bytes for {self.site_url}", flush=True)
            self.ready.emit(self.site_url, data)
        except Exception as e:
            print(f"[thumb] failed for {self.site_url}: {e}", flush=True)
            self.ready.emit(self.site_url, b"")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class Panel(QWidget):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.results: list[SiteResult] = []
        self.detail_index: int | None = None
        self.last_update = 0
        self.setFixedSize(480, 320)

        self.scroll_offset = 0
        self.detail_scroll = 0
        self._detail_content_h = 480
        self._drag_start_y = 0.0
        self._drag_start_scroll = 0
        self._is_dragging = False

        self._thumb_cache: dict[str, bytes] = {}
        self._thumb_failed: set[str] = set()
        self._fetching: set[str] = set()
        self._fetchers: list = []

        self.history = load_history()
        self._hist_pixmaps: dict[str, QPixmap] = {}

        self.poller = Poller(config)
        self.poller.updated.connect(self.on_update)
        self.poller.start()

        self.clock = QTimer(self)
        self.clock.timeout.connect(self.update)
        self.clock.start(5000)

    def _fetch_thumbnail(self, url: str):
        token = self.config.get("screenshot_api_token", "")
        if not token or not url or url in self._thumb_cache or url in self._fetching:
            return
        self._thumb_failed.discard(url)
        self._fetching.add(url)
        f = ThumbnailFetcher(url, token)
        f.ready.connect(self._on_thumbnail)
        self._fetchers.append(f)
        f.start()

    def _on_thumbnail(self, url: str, data: bytes):
        self._fetching.discard(url)
        self._fetchers = [f for f in self._fetchers if f.isRunning()]
        if data:
            self._thumb_cache[url] = data
        else:
            self._thumb_failed.add(url)
        if self.detail_index is not None:
            self.update()

    def _rebuild_hist_pixmaps(self):
        self._hist_pixmaps.clear()
        for r in self.results:
            hist = self.history.get(r.url or r.name, [])
            if not hist:
                continue
            pm = QPixmap(317, 5)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            n = len(hist)
            bw = 317 / n
            for i, e in enumerate(hist):
                x = i * bw
                pp.fillRect(QRectF(x, 0, max(1, bw), 2), GREEN if e["host_ok"] else RED)
                pp.fillRect(QRectF(x, 3, max(1, bw), 2), GREEN if e["page_ok"] else RED)
            pp.end()
            self._hist_pixmaps[r.url or r.name] = pm

    def on_update(self, results):
        append_history(self.history, results)
        detail_name = (
            self.results[self.detail_index].name
            if self.detail_index is not None and self.detail_index < len(self.results)
            else None
        )
        self.results = [r for _, r in sorted(enumerate(results),
                                             key=lambda x: (_status_priority(x[1]), x[0]))]
        for r in self.results:
            if _status_priority(r) < 2:
                self._thumb_cache.pop(r.url, None)
                self._thumb_failed.discard(r.url)
        if detail_name is not None:
            for i, r in enumerate(self.results):
                if r.name == detail_name:
                    self.detail_index = i
                    break
        self._rebuild_hist_pixmaps()
        self.last_update = time.time()
        self.update()

    # ---- input ----------------------------------------------------------
    def _touch_y(self, ev):
        return ev.position().y() * (480.0 / 320.0)

    def mousePressEvent(self, ev):
        y = self._touch_y(ev)
        self._drag_start_y = y
        self._drag_start_scroll = (
            self.detail_scroll if self.detail_index is not None else self.scroll_offset
        )
        self._is_dragging = False

    def mouseMoveEvent(self, ev):
        y = self._touch_y(ev)
        delta = self._drag_start_y - y
        if not self._is_dragging and abs(delta) > 6:
            self._is_dragging = True
        if self._is_dragging:
            if self.detail_index is not None:
                max_scroll = max(0, self._detail_content_h - 420)
                self.detail_scroll = max(0, min(self._drag_start_scroll + int(delta), max_scroll))
            else:
                max_scroll = max(0, len(self.results) * ROW_H - 440)
                self.scroll_offset = max(0, min(self._drag_start_scroll + int(delta), max_scroll))
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._is_dragging:
            self._is_dragging = False
            return
        y = self._touch_y(ev)
        if self.detail_index is not None:
            self.detail_index = None
            self.detail_scroll = 0
            self.update()
            return
        if y < 40:
            self.poller.force_poll()
            return
        if self.results:
            idx = int((y + self.scroll_offset - 40) / ROW_H)
            if 0 <= idx < len(self.results):
                self.detail_index = idx
                self.detail_scroll = 0
                self._fetch_thumbnail(self.results[idx].url)
                self.update()

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        p.translate(480, 0)
        p.rotate(90)
        if self.detail_index is not None and self.detail_index < len(self.results):
            self.paint_detail(p, self.results[self.detail_index])
        else:
            self.paint_grid(p)
        p.end()

    def _header(self, p, title):
        p.setPen(TEXT)
        p.setFont(QFont("DejaVu Sans", 13, QFont.Weight.Bold))
        p.drawText(QRectF(10, 6, 210, 28), Qt.AlignmentFlag.AlignVCenter, title)
        ago = int(time.time() - self.last_update) if self.last_update else -1
        p.setPen(MUTED)
        p.setFont(QFont("DejaVu Sans", 8))
        label = f"{ago}s ago" if ago >= 0 else "…"
        p.drawText(QRectF(225, 6, 85, 28),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, label)
        p.setPen(QPen(TRACK, 1))
        p.drawLine(0, 38, 320, 38)

    def paint_grid(self, p):
        self._header(p, "Site Status")
        if not self.results:
            p.setPen(MUTED)
            p.setFont(QFont("DejaVu Sans", 10))
            p.drawText(QRectF(0, 0, 320, 480), Qt.AlignmentFlag.AlignCenter, "Polling…")
            return

        p.setClipRect(QRectF(0, 40, 320, 440))

        for i, r in enumerate(self.results):
            top = 40 + i * ROW_H - self.scroll_offset
            if top + ROW_H < 40 or top > 480:
                continue
            if i % 2 == 0:
                p.fillRect(QRectF(0, top, 320, ROW_H), PANEL)

            cy = top + ROW_H / 2
            self._light(p, 13, cy, r.host_ok)
            self._light(p, 32, cy, r.page_ok)

            name_y = top + (ROW_H - 35) / 2
            p.setPen(TEXT)
            p.setFont(QFont("DejaVu Sans", 12, QFont.Weight.Bold))
            p.drawText(QRectF(46, name_y, 140, 22), Qt.AlignmentFlag.AlignVCenter, r.name)
            if r.title:
                p.setPen(MUTED)
                p.setFont(QFont("DejaVu Sans", 7))
                p.drawText(QRectF(46, name_y + 23, 140, 12), Qt.AlignmentFlag.AlignVCenter, r.title)

            if r.cpu or r.memory or r.disk:
                self._arc(p, 210, cy, r.cpu.get("percent") if r.cpu else None, "CPU")
                self._arc(p, 254, cy, r.memory.get("percent") if r.memory else None, "MEM")
                self._arc(p, 298, cy, r.disk.get("percent") if r.disk else None, "DSK")
            else:
                p.setPen(MUTED)
                p.setFont(QFont("DejaVu Sans", 7))
                tag = "HTTP only" if not r.has_agent else (r.error or "no data")
                p.drawText(QRectF(185, top, 130, ROW_H), Qt.AlignmentFlag.AlignCenter, tag)

            # history bars — pre-rendered pixmap, one drawPixmap per card
            pm = self._hist_pixmaps.get(r.url or r.name)
            if pm:
                p.drawPixmap(0, int(top + ROW_H - 6), pm)

        p.setClipping(False)

        total_h = len(self.results) * ROW_H
        if total_h > 440:
            thumb_h = max(24, int(440 * 440 / total_h))
            thumb_y = 40 + int(self.scroll_offset * (440 - thumb_h) / (total_h - 440))
            p.fillRect(QRectF(317, 40, 3, 440), TRACK)
            p.fillRect(QRectF(317, thumb_y, 3, thumb_h), MUTED)

    def paint_detail(self, p, r: SiteResult):
        self._header(p, r.name)

        # fixed footer (outside scroll clip)
        p.setPen(MUTED)
        p.setFont(QFont("DejaVu Sans", 8))
        p.drawText(QRectF(0, 460, 317, 18), Qt.AlignmentFlag.AlignCenter, "tap to go back")

        # scrollable content
        p.save()
        p.setClipRect(QRectF(0, 40, 317, 420))
        p.translate(0, -self.detail_scroll)

        y = 50

        def sep():
            nonlocal y
            y += 4
            p.setPen(QPen(TRACK, 1))
            p.drawLine(12, y, 308, y)
            y += 6

        # ---- compact status line: HTTP code | response time | grep ----
        p.setFont(QFont("DejaVu Sans Mono", 9))
        http_col = GREEN if r.http_status == 200 else (RED if r.http_status else MUTED)
        p.setPen(MUTED)
        p.drawText(QRectF(12, y, 36, 20), Qt.AlignmentFlag.AlignVCenter, "HTTP")
        p.setPen(http_col)
        p.drawText(QRectF(50, y, 38, 20), Qt.AlignmentFlag.AlignVCenter,
                   str(r.http_status or "—"))
        p.setPen(MUTED)
        p.drawText(QRectF(90, y, 60, 20), Qt.AlignmentFlag.AlignVCenter,
                   f"{r.response_ms}ms" if r.response_ms else "—")
        if r.grep_phrase:
            p.setPen(MUTED)
            p.drawText(QRectF(155, y, 28, 20), Qt.AlignmentFlag.AlignVCenter, "grep")
            p.setPen(GREEN if r.grep_found else RED)
            p.drawText(QRectF(185, y, 14, 20), Qt.AlignmentFlag.AlignVCenter,
                       "✓" if r.grep_found else "✗")
        y += 24

        # ---- title ----
        if r.title:
            p.setPen(MUTED)
            p.setFont(QFont("DejaVu Sans", 8))
            p.drawText(QRectF(12, y, 295, 18), Qt.AlignmentFlag.AlignVCenter, r.title)
            y += 20

        # ---- error ----
        if r.error:
            p.setPen(RED)
            p.setFont(QFont("DejaVu Sans", 8))
            p.drawText(QRectF(12, y, 295, 18), Qt.AlignmentFlag.AlignVCenter, r.error)
            y += 20

        # ---- history dots ----
        hist = self.history.get(r.url or r.name, [])
        if hist:
            sep()
            host_cols = [GREEN if e["host_ok"] else RED for e in hist]
            page_cols = [GREEN if e["page_ok"] else RED for e in hist]
            self._history_dots(p, y, 14, host_cols, "host")
            y += 16
            self._history_dots(p, y, 14, page_cols, "page")
            y += 16

        # ---- sparklines ----
        if r.has_agent and hist:
            sep()
            cpu_vals  = [(e["cpu"],  _gauge_col(e["cpu"]))  for e in hist if e.get("cpu")  is not None]
            mem_vals  = [(e["mem"],  _gauge_col(e["mem"]))  for e in hist if e.get("mem")  is not None]
            disk_vals = [(e["disk"], _gauge_col(e["disk"])) for e in hist if e.get("disk") is not None]
            if cpu_vals:
                self._sparkline(p, y, 20, cpu_vals, "CPU")
                y += 23
            if mem_vals:
                self._sparkline(p, y, 20, mem_vals, "MEM")
                y += 23
            if disk_vals:
                self._sparkline(p, y, 20, disk_vals, "DSK")
                y += 23

        # ---- current server stats (numbers) ----
        if r.cpu or r.memory or r.disk:
            sep()
            p.setFont(QFont("DejaVu Sans Mono", 8))
            def stat_line(label, value):
                nonlocal y
                p.setPen(MUTED)
                p.drawText(QRectF(12, y, 70, 18), Qt.AlignmentFlag.AlignVCenter, label)
                p.setPen(TEXT)
                p.drawText(QRectF(84, y, 221, 18), Qt.AlignmentFlag.AlignVCenter, value)
                y += 20
            if r.cpu:
                c = r.cpu
                stat_line("CPU", f"{c.get('percent')}%  load {c.get('load1')} / {c.get('cores')} cores")
            if r.memory:
                m = r.memory
                stat_line("Memory", f"{m.get('percent')}%  ({m.get('used_mb')} / {m.get('total_mb')} MB)")
            if r.disk:
                d = r.disk
                stat_line("Disk", f"{d.get('percent')}%  ({d.get('used_gb')} / {d.get('total_gb')} GB)")
        elif not r.has_agent:
            sep()
            p.setPen(MUTED)
            p.setFont(QFont("DejaVu Sans", 8))
            p.drawText(QRectF(12, y, 295, 18), Qt.AlignmentFlag.AlignVCenter,
                       "HTTP-only site — no agent installed")
            y += 20

        # ---- thumbnail ----
        if self.config.get("screenshot_api_token") and r.url:
            sep()
            if r.url in self._thumb_cache and self._thumb_cache[r.url]:
                pm = QPixmap()
                if pm.loadFromData(self._thumb_cache[r.url]) and not pm.isNull():
                    tw, th = 295, int(pm.height() * 295 / pm.width())
                    scaled = pm.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                    p.drawPixmap(12, y, scaled)
                    y += th + 4
                else:
                    p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 8))
                    p.drawText(QRectF(0, y, 317, 20), Qt.AlignmentFlag.AlignCenter,
                               "Preview unavailable")
                    y += 22
            elif r.url in self._fetching:
                p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 8))
                p.drawText(QRectF(0, y, 317, 20), Qt.AlignmentFlag.AlignCenter,
                           "Loading preview…")
                y += 22
            elif r.url in self._thumb_failed:
                p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 8))
                p.drawText(QRectF(0, y, 317, 20), Qt.AlignmentFlag.AlignCenter,
                           "Preview unavailable")
                y += 22

        y += 8
        self._detail_content_h = y - 50

        p.restore()
        p.setClipping(False)

        # scrollbar
        if self._detail_content_h > 420:
            thumb_h = max(24, int(420 * 420 / self._detail_content_h))
            thumb_y = 40 + int(self.detail_scroll * (420 - thumb_h) /
                                max(1, self._detail_content_h - 420))
            p.fillRect(QRectF(317, 40, 3, 420), TRACK)
            p.fillRect(QRectF(317, thumb_y, 3, thumb_h), MUTED)

    # ---- drawing helpers ------------------------------------------------
    def _history_dots(self, p, y, h, values, label):
        LABEL_W = 28
        BAR_X   = 12 + LABEL_W
        BAR_W   = 296 - LABEL_W - 28   # matches sparkline width
        p.setPen(MUTED)
        p.setFont(QFont("DejaVu Sans", 6))
        p.drawText(QRectF(12, y, LABEL_W, h), Qt.AlignmentFlag.AlignVCenter, label)
        n = len(values)
        if n:
            bw = BAR_W / n
            ly = y + (h - 2) / 2
            for i, col in enumerate(values):
                p.fillRect(QRectF(BAR_X + i * bw, ly, max(1, bw), 2), col)

    def _sparkline(self, p, y, h, values, label):
        LABEL_W = 28
        VAL_W   = 28
        BAR_X   = 12 + LABEL_W
        BAR_W   = 296 - LABEL_W - VAL_W
        p.setPen(MUTED)
        p.setFont(QFont("DejaVu Sans", 6))
        p.drawText(QRectF(12, y, LABEL_W, h), Qt.AlignmentFlag.AlignVCenter, label)
        p.fillRect(QRectF(BAR_X, y + 2, BAR_W, h - 4), TRACK)
        n = len(values)
        if n:
            bw = BAR_W / n
            for i, (pct, col) in enumerate(values):
                bh = max(1, int((h - 4) * pct / 100))
                p.fillRect(QRectF(BAR_X + i * bw, y + h - 2 - bh, max(1, bw - 1), bh), col)
            last_pct, last_col = values[-1]
            p.setPen(last_col)
            p.setFont(QFont("DejaVu Sans", 7, QFont.Weight.Bold))
            p.drawText(QRectF(BAR_X + BAR_W + 2, y, VAL_W, h),
                       Qt.AlignmentFlag.AlignVCenter, f"{last_pct}%")

    def _light(self, p, cx, cy, ok):
        col = GREEN if ok else RED
        p.setBrush(QBrush(col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 7, 7)

    def _arc(self, p, cx, cy, percent, label):
        R = 16
        rect = QRectF(cx - R, cy - R, R * 2, R * 2)
        p.setPen(QPen(TRACK, 4)); p.drawArc(rect, 0, 360 * 16)
        if percent is not None:
            col = GREEN if percent < 70 else (AMBER if percent < 90 else RED)
            p.setPen(QPen(col, 4))
            p.drawArc(rect, 90 * 16, int(-360 * 16 * percent / 100))
            p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 5))
            p.drawText(QRectF(cx - R, cy - 9, R * 2, 10), Qt.AlignmentFlag.AlignCenter, label)
            p.setPen(TEXT); p.setFont(QFont("DejaVu Sans", 8, QFont.Weight.Bold))
            p.drawText(QRectF(cx - R, cy - 1, R * 2, 12), Qt.AlignmentFlag.AlignCenter, f"{percent}")
        else:
            p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 5))
            p.drawText(QRectF(cx - R, cy - 9, R * 2, 10), Qt.AlignmentFlag.AlignCenter, label)
            p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 7))
            p.drawText(QRectF(cx - R, cy - 1, R * 2, 12), Qt.AlignmentFlag.AlignCenter, "—")

    def closeEvent(self, ev):
        self.poller.stop()
        self.poller.wait(2000)
        ev.accept()


def main():
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = json.loads(CONFIG_PATH.read_text())

    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.CursorShape.BlankCursor)
    w = Panel(config)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
