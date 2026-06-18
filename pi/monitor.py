#!/usr/bin/env python3
"""
monitor.py - site status panel for a 480x320 SPI LCD on a Pi 3B+.

Runs WITHOUT a desktop, drawing directly to the framebuffer via Qt's
linuxfb platform plugin. Launch with:

    QT_QPA_PLATFORM=linuxfb:fb=/dev/fb1 python3 monitor.py

Two views:
  - Grid: one row per site. Two lights (host / page) + CPU·MEM·DISK arcs.
  - Detail: tap a row to see HTTP status, response time, title, grep result,
            and raw server stats. Tap anywhere to go back.

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
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QBrush
from PyQt6.QtWidgets import QApplication, QWidget

CONFIG_PATH = Path(__file__).with_name("config.json")

# ---- palette (kept tiny: dark bg is cheap to repaint on SPI) -------------
BG       = QColor("#11141a")
PANEL    = QColor("#1b1f29")
TEXT     = QColor("#e6e9ef")
MUTED    = QColor("#7c8597")
GREEN    = QColor("#3ec46d")
AMBER    = QColor("#e7b54a")
RED      = QColor("#e1543f")
TRACK    = QColor("#2b3140")

ROW_H = 72   # fixed row height — enables scrolling regardless of site count


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class SiteResult:
    name: str
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
        while self._running:
            self._force.clear()
            results = [self.check_site(s) for s in self.config.get("sites", [])]
            self.updated.emit(results)
            # sleep in small slices so stop() and force_poll() are responsive
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

        r = SiteResult(name=name, grep_phrase=grep, has_agent=bool(agent))

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
                    # title
                    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                    if m:
                        r.title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
                    # grep
                    if grep:
                        r.grep_found = grep in body
                    # page_ok = 200 and (grep matched, or no grep configured)
                    r.page_ok = (resp.status == 200) and (r.grep_found is not False)
            except urllib.error.HTTPError as e:
                r.host_ok = True               # server answered, just not 200
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
                # agent failure doesn't change host/page lights, just no gauges
                if not r.error:
                    r.error = f"agent:{type(e).__name__}"

        return r


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
        self._drag_start_y = 0.0
        self._drag_start_scroll = 0
        self._is_dragging = False

        self.poller = Poller(config)
        self.poller.updated.connect(self.on_update)
        self.poller.start()

        # clock tick so the "updated Ns ago" stays live
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.update)
        self.clock.start(1000)

    def on_update(self, results):
        self.results = results
        self.last_update = time.time()
        self.update()

    # ---- input ----------------------------------------------------------
    def _touch_y(self, ev):
        # Touch Y axis tracks physical left-right; scale [0,320) → portrait [0,480).
        return ev.position().y() * (480.0 / 320.0)

    def mousePressEvent(self, ev):
        y = self._touch_y(ev)
        if self.detail_index is not None:
            self.detail_index = None   # any press exits detail
            self.update()
            return
        self._drag_start_y = y
        self._drag_start_scroll = self.scroll_offset
        self._is_dragging = False

    def mouseMoveEvent(self, ev):
        if self.detail_index is not None:
            return
        y = self._touch_y(ev)
        delta = self._drag_start_y - y
        if not self._is_dragging and abs(delta) > 6:
            self._is_dragging = True
        if self._is_dragging:
            max_scroll = max(0, len(self.results) * ROW_H - (480 - 40))
            self.scroll_offset = max(0, min(self._drag_start_scroll + int(delta), max_scroll))
            self.update()

    def mouseReleaseEvent(self, ev):
        if self.detail_index is not None or self._is_dragging:
            return
        y = self._touch_y(ev)
        if y < 40:
            # header tap → force immediate repoll
            self.poller.force_poll()
            return
        if self.results:
            idx = int((y + self.scroll_offset - 40) / ROW_H)
            if 0 <= idx < len(self.results):
                self.detail_index = idx
                self.update()

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        # Rotate content 90° CW so 320×480 portrait fills the 480×320 landscape framebuffer.
        p.translate(480, 0)
        p.rotate(90)
        if self.detail_index is not None and self.detail_index < len(self.results):
            self.paint_detail(p, self.results[self.detail_index])
        else:
            self.paint_grid(p)
        p.end()

    def _header(self, p, title):
        p.setPen(TEXT)
        f = QFont("DejaVu Sans", 13, QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(10, 6, 210, 28), Qt.AlignmentFlag.AlignVCenter, title)
        # freshness
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

            # lights - side by side, vertically centred
            self._light(p, 13, cy, r.host_ok)
            self._light(p, 32, cy, r.page_ok)

            # name + title as a compact centred block
            name_y = top + (ROW_H - 35) / 2
            p.setPen(TEXT)
            p.setFont(QFont("DejaVu Sans", 12, QFont.Weight.Bold))
            p.drawText(QRectF(46, name_y, 140, 22), Qt.AlignmentFlag.AlignVCenter, r.name)
            if r.title:
                p.setPen(MUTED)
                p.setFont(QFont("DejaVu Sans", 7))
                p.drawText(QRectF(46, name_y + 23, 140, 12), Qt.AlignmentFlag.AlignVCenter, r.title)

            # gauges - right side, vertically centred
            if r.cpu or r.memory or r.disk:
                self._arc(p, 210, cy, r.cpu.get("percent") if r.cpu else None, "CPU")
                self._arc(p, 254, cy, r.memory.get("percent") if r.memory else None, "MEM")
                self._arc(p, 298, cy, r.disk.get("percent") if r.disk else None, "DSK")
            else:
                p.setPen(MUTED)
                p.setFont(QFont("DejaVu Sans", 7))
                tag = "HTTP only" if not r.has_agent else (r.error or "no data")
                p.drawText(QRectF(185, top, 130, ROW_H),
                           Qt.AlignmentFlag.AlignCenter, tag)

        p.setClipping(False)

        # scrollbar
        total_h = len(self.results) * ROW_H
        if total_h > 440:
            thumb_h = max(24, int(440 * 440 / total_h))
            thumb_y = 40 + int(self.scroll_offset * (440 - thumb_h) / (total_h - 440))
            p.fillRect(QRectF(317, 40, 3, 440), TRACK)
            p.fillRect(QRectF(317, thumb_y, 3, thumb_h), MUTED)

    def paint_detail(self, p, r: SiteResult):
        self._header(p, r.name)
        p.setFont(QFont("DejaVu Sans Mono", 9))
        y = 50
        def line(label, value, colour=TEXT):
            nonlocal y
            p.setPen(MUTED); p.drawText(QRectF(12, y, 100, 20), Qt.AlignmentFlag.AlignVCenter, label)
            p.setPen(colour); p.drawText(QRectF(115, y, 193, 20), Qt.AlignmentFlag.AlignVCenter, value)
            y += 22

        line("HTTP", str(r.http_status or "—"),
             GREEN if r.http_status == 200 else (RED if r.http_status else MUTED))
        line("Response", f"{r.response_ms} ms" if r.response_ms else "—")
        line("Title", r.title or "—")
        if r.grep_phrase:
            line("Phrase", "found" if r.grep_found else "MISSING",
                 GREEN if r.grep_found else RED)
        else:
            line("Phrase", "(none set)", MUTED)
        if r.error:
            line("Error", r.error, RED)

        # server stats block
        y += 6
        p.setPen(QPen(TRACK, 1)); p.drawLine(12, y, 308, y); y += 8
        if r.cpu:
            c = r.cpu
            line("CPU", f"{c.get('percent')}%  load {c.get('load1')} / {c.get('cores')} cores")
        if r.memory:
            m = r.memory
            line("Memory", f"{m.get('percent')}%  ({m.get('used_mb')}/{m.get('total_mb')} MB)")
        if r.disk:
            d = r.disk
            line("Disk", f"{d.get('percent')}%  ({d.get('used_gb')}/{d.get('total_gb')} GB)")
        if not (r.cpu or r.memory or r.disk):
            line("Server", "HTTP-only site (no agent)", MUTED)

        # back hint
        p.setPen(MUTED); p.setFont(QFont("DejaVu Sans", 8))
        p.drawText(QRectF(0, 460, 320, 18), Qt.AlignmentFlag.AlignCenter, "tap to go back")

    # ---- primitives -----------------------------------------------------
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
            # label + number both inside the arc
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
    app.setOverrideCursor(Qt.CursorShape.BlankCursor)   # no mouse pointer on kiosk
    w = Panel(config)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
