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
import dataclasses
import http.server
import socketserver
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

_web_lock          = threading.Lock()
_web_snapshot      = b'{"sites":[],"last_update":0}'
_web_thumbs: dict  = {}   # url → png bytes, shared with web handler
_web_fetch_pending: set = set()
_screenshot_api_token: str = ""
_web_token: str = ""

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

def _web_thumb_fetch(url: str):
    try:
        api_url = (
            "https://shot.screenshotapi.net/screenshot"
            f"?token={_screenshot_api_token}"
            f"&url={urllib.parse.quote(url, safe='')}"
            "&output=image&file_type=png"
        )
        req = urllib.request.Request(api_url, headers={"User-Agent": "PiStatusPanel/1.0"})
        with urllib.request.urlopen(req, timeout=50) as resp:
            data = resp.read()
        if data:
            _web_thumbs[url] = data
            print(f"[web-thumb] cached {len(data)} bytes for {url}", flush=True)
    except Exception as e:
        print(f"[web-thumb] failed for {url}: {e}", flush=True)
    finally:
        _web_fetch_pending.discard(url)


def _make_web_snapshot(results: list, history: dict, last_update: float):
    global _web_snapshot
    sites = []
    for r in results:
        key = r.url or r.name
        d = dataclasses.asdict(r)
        d["history"] = history.get(key, [])
        sites.append(d)
    data = json.dumps({"last_update": last_update, "sites": sites}).encode()
    with _web_lock:
        _web_snapshot = data

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
    skip_disk: bool = False


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

        r = SiteResult(name=name, url=url, grep_phrase=grep, has_agent=bool(agent),
                       skip_disk=bool(site.get("skip_disk", False)))

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
# Web server (daemon thread — shares _web_snapshot with the Qt side)
# ---------------------------------------------------------------------------
_LOGIN_HTML = b"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiPing</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#11141a;color:#e6e9ef;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px}
.box{background:#1b1f29;border-radius:8px;padding:28px;width:100%;max-width:320px}
h1{font-size:18px;font-weight:700;margin-bottom:20px}
label{font-size:12px;color:#7c8597;display:block;margin-bottom:6px}
input{width:100%;background:#11141a;border:1px solid #2b3140;color:#e6e9ef;padding:9px 11px;border-radius:4px;font-size:15px;margin-bottom:14px}
input:focus{outline:none;border-color:#7c8597}
button{width:100%;background:#3ec46d;color:#11141a;border:none;padding:11px;border-radius:4px;font-size:14px;font-weight:700;cursor:pointer}
#err{color:#e1543f;font-size:12px;margin-bottom:12px;min-height:16px}
</style>
</head>
<body>
<div class="box">
<h1>PiPing</h1>
<form method="POST" action="/login">
<label for="k">Access key</label>
<input type="password" name="key" id="k" autofocus autocomplete="current-password">
<p id="err"></p>
<button type="submit">Enter</button>
</form>
</div>
<script>if(location.search.indexOf('err')>-1)document.getElementById('err').textContent='Incorrect key'</script>
</body>
</html>
"""

_WEB_HTML = b"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiPing</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#11141a;color:#e6e9ef;font-family:system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #2b3140;position:sticky;top:0;background:#11141a;z-index:10}
h1{font-size:17px;font-weight:700}
#ago{color:#7c8597;font-size:12px}
button{background:none;border:1px solid #2b3140;color:#7c8597;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px}
button:hover{border-color:#7c8597;color:#e6e9ef}
#grid{padding:8px;display:grid;gap:6px;grid-template-columns:1fr}
@media(min-width:560px){#grid{grid-template-columns:1fr 1fr}}
@media(min-width:900px){#grid{grid-template-columns:repeat(3,1fr)}}
.card{background:#1b1f29;border-radius:6px;padding:10px 10px 0;cursor:pointer;transition:background .15s}
.card:hover{background:#212636}
.card-top{display:flex;align-items:center;gap:8px;padding-bottom:8px}
.dots{display:flex;flex-direction:column;gap:5px}
.dot{width:13px;height:13px;border-radius:50%;flex-shrink:0}
.g{background:#3ec46d}.r{background:#e1543f}
.info{flex:1;min-width:0;overflow:hidden;max-width:calc(100vw - 200px)}
.nm{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tt{color:#7c8597;font-size:10px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gauges{display:flex;gap:1px;flex-shrink:0;margin-left:auto}
.htag{color:#7c8597;font-size:10px;padding:0 6px}
canvas.hist{display:block;width:100%;max-width:100%;height:14px}
#ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;align-items:flex-end;justify-content:center}
#ov.open{display:flex}
#modal{background:#1b1f29;border-radius:12px 12px 0 0;width:100%;max-width:600px;max-height:88vh;overflow-y:auto;padding:16px 16px 24px}
@media(min-width:640px){#ov{align-items:center}#modal{border-radius:12px;max-height:80vh}}
.mhdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.mhdr h2{font-size:16px;font-weight:700}
.mhdr button{border:none;font-size:20px;padding:2px 6px;line-height:1}
hr{border:none;border-top:1px solid #2b3140;margin:10px 0}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.tag{display:inline-block;padding:3px 7px;border-radius:3px;font-size:11px;font-weight:600}
.ok{background:#1a3328;color:#3ec46d}.er{background:#3a1a17;color:#e1543f}.dm{background:#1b2030;color:#7c8597}
.srow{display:flex;gap:8px;margin-bottom:5px;font-size:12px}
.sl{color:#7c8597;width:64px;flex-shrink:0}
.sv{font-family:monospace}
.sp{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.sp-l{color:#7c8597;font-size:11px;width:32px;flex-shrink:0}
.sp-c{flex:1;height:24px;background:#2b3140;border-radius:2px}
.sp-v{font-size:12px;font-weight:700;width:36px;text-align:right;flex-shrink:0}
</style>
</head>
<body>
<header>
  <h1>PiPing</h1>
  <div style="display:flex;align-items:center;gap:10px">
    <span id="ago">&#8212;</span>
    <button onclick="load()">Refresh</button>
  </div>
</header>
<div id="grid"><p style="color:#7c8597;padding:24px;text-align:center">Loading&#8230;</p></div>
<div id="ov" onclick="if(event.target===this)closeModal()">
  <div id="modal">
    <div class="mhdr"><h2 id="mt">&#8212;</h2><button onclick="closeModal()">&#x2715;</button></div>
    <div id="mb"></div>
  </div>
</div>
<script>
const G='#3ec46d',A='#e7b54a',R='#e1543f',T='#2b3140',M='#7c8597';
let data=null;
function gc(p){return p<70?G:p<90?A:R}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function arc(pct,lbl,naText){
  const r=14,cx=18,cy=18,C=+(2*Math.PI*r).toFixed(2);
  const col=pct!=null?gc(pct):T,off=pct!=null?+(C-C*pct/100).toFixed(2):C,txt=pct!=null?pct:(naText||'&#8212;');
  return '<svg width="36" height="36" viewBox="0 0 36 36" style="flex-shrink:0">'
    +'<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="'+T+'" stroke-width="3.5"/>'
    +(pct!=null?'<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="'+col+'" stroke-width="3.5" stroke-dasharray="'+C+'" stroke-dashoffset="'+off+'" transform="rotate(-90 '+cx+' '+cy+')"/>'
:'')
    +'<text x="'+cx+'" y="'+(cy-3)+'" text-anchor="middle" fill="'+M+'" font-size="5.5" font-family="sans-serif">'+lbl+'</text>'
    +'<text x="'+cx+'" y="'+(cy+7)+'" text-anchor="middle" fill="'+(pct!=null?'#e6e9ef':M)+'" font-size="8.5" font-weight="bold" font-family="monospace">'+txt+'</text>'
    +'</svg>';
}
function hcanv(c,hist,skip){
  if(!hist||!hist.length)return;
  const dpr=devicePixelRatio||1,w=c.offsetWidth||320,h=14;
  c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);
  c.style.width='100%';c.style.height=h+'px';
  const x=c.getContext('2d');x.scale(dpr,dpr);
  const rows=[[0,e=>e.host_ok?G:R],[3,e=>e.page_ok?G:R],
    [6,e=>e.cpu!=null?gc(e.cpu):T],[9,e=>e.mem!=null?gc(e.mem):T],
    [12,e=>(!skip&&e.disk!=null)?gc(e.disk):T]];
  const n=hist.length,bw=w/n;
  for(const[y,cf]of rows)for(let i=0;i<n;i++){x.fillStyle=cf(hist[i]);x.fillRect(i*bw,y,Math.max(1,bw),2)}
}
function spark(c,vals,h){
  if(!vals||!vals.length)return;
  const dpr=devicePixelRatio||1,w=c.offsetWidth||200;
  c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);
  const x=c.getContext('2d');x.scale(dpr,dpr);
  x.fillStyle=T;x.fillRect(0,0,w,h);
  const n=vals.length,bw=w/n;
  for(let i=0;i<n;i++){const bh=Math.max(1,Math.round((h-2)*vals[i]/100));x.fillStyle=gc(vals[i]);x.fillRect(i*bw,h-1-bh,Math.max(1,bw-1),bh)}
}
function render(d){
  data=d;
  const g=document.getElementById('grid');
  if(!d.sites||!d.sites.length){g.innerHTML='<p style="color:#7c8597;padding:24px;text-align:center">No sites</p>';return}
  g.innerHTML=d.sites.map((s,i)=>{
    const hg=s.has_agent&&(s.cpu||s.memory||s.disk);
    const ga=hg?'<div class="gauges">'+arc(s.cpu?s.cpu.percent:null,'CPU',null)+arc(s.memory?s.memory.percent:null,'MEM',null)+arc(s.skip_disk?null:(s.disk?s.disk.percent:null),'DSK',s.skip_disk?'NA':null)+'</div>'
               :'<span class="htag">'+(s.has_agent?(s.error||'no data'):'HTTP only')+'</span>';
    return '<div class="card" data-i="'+i+'">'
      +'<div class="card-top">'
      +'<div class="dots"><div class="dot '+(s.host_ok?'g':'r')+'"></div><div class="dot '+(s.page_ok?'g':'r')+'"></div></div>'
      +'<div class="info"><div class="nm">'+esc(s.name)+'</div>'+(s.title?'<div class="tt">'+esc(s.title)+'</div>':'')+'</div>'
      +ga+'</div>'
      +'<canvas class="hist" data-i="'+i+'"></canvas></div>';
  }).join('');
  g.querySelectorAll('.card').forEach(c=>c.addEventListener('click',()=>openModal(+c.dataset.i)));
  requestAnimationFrame(()=>{g.querySelectorAll('canvas.hist').forEach(c=>{const s=d.sites[+c.dataset.i];hcanv(c,s.history,s.skip_disk)})});
}
function openModal(i){
  const s=data.sites[i],hist=s.history||[];
  document.getElementById('mt').textContent=s.name;
  let h='<div class="tags">'
    +'<span class="tag '+(!s.host_ok||!s.page_ok?'er':'ok')+'">'+((!s.host_ok||!s.page_ok)?'DOWN':'UP')+'</span>';
  if(s.http_status)h+='<span class="tag '+(s.http_status===200?'ok':'er')+'">HTTP '+s.http_status+'</span>';
  if(s.response_ms)h+='<span class="tag dm">'+s.response_ms+'ms</span>';
  if(s.grep_phrase)h+='<span class="tag '+(s.grep_found?'ok':'er')+'">grep '+(s.grep_found?'&#10003;':'&#10007;')+'</span>';
  h+='</div>';
  if(s.title)h+='<p style="color:'+M+';font-size:12px;margin-bottom:8px">'+esc(s.title)+'</p>';
  if(s.error)h+='<p style="color:'+R+';font-size:12px;margin-bottom:8px">'+esc(s.error)+'</p>';
  if(hist.length){
    h+='<hr><div style="font-size:11px;color:'+M+';margin-bottom:6px">History ('+hist.length+' polls)</div>'
      +'<canvas id="mh1" style="display:block;width:100%;height:14px;margin-bottom:3px"></canvas>'
      +'<canvas id="mh2" style="display:block;width:100%;height:14px;margin-bottom:6px"></canvas>';
  }
  const cpu=hist.filter(e=>e.cpu!=null).map(e=>e.cpu);
  const mem=hist.filter(e=>e.mem!=null).map(e=>e.mem);
  const dsk=s.skip_disk?[]:hist.filter(e=>e.disk!=null).map(e=>e.disk);
  if(cpu.length||mem.length||dsk.length){
    h+='<hr>';
    if(cpu.length)h+='<div class="sp"><span class="sp-l">CPU</span><canvas id="msc" class="sp-c"></canvas><span class="sp-v" style="color:'+gc(cpu[cpu.length-1])+'">'+cpu[cpu.length-1]+'%</span></div>';
    if(mem.length)h+='<div class="sp"><span class="sp-l">MEM</span><canvas id="msm" class="sp-c"></canvas><span class="sp-v" style="color:'+gc(mem[mem.length-1])+'">'+mem[mem.length-1]+'%</span></div>';
    if(dsk.length)h+='<div class="sp"><span class="sp-l">DSK</span><canvas id="msd" class="sp-c"></canvas><span class="sp-v" style="color:'+gc(dsk[dsk.length-1])+'">'+dsk[dsk.length-1]+'%</span></div>';
  }
  if(s.cpu||s.memory||(s.disk&&!s.skip_disk)){
    h+='<hr>';
    if(s.cpu)h+='<div class="srow"><span class="sl">CPU</span><span class="sv">'+s.cpu.percent+'% &nbsp;load '+s.cpu.load1+' / '+s.cpu.cores+' cores</span></div>';
    if(s.memory)h+='<div class="srow"><span class="sl">Memory</span><span class="sv">'+s.memory.percent+'% &nbsp;('+s.memory.used_mb+' / '+s.memory.total_mb+' MB)</span></div>';
    if(s.disk&&!s.skip_disk)h+='<div class="srow"><span class="sl">Disk</span><span class="sv">'+s.disk.percent+'% &nbsp;('+s.disk.used_gb+' / '+s.disk.total_gb+' GB)</span></div>';
    else if(s.skip_disk)h+='<div class="srow"><span class="sl">Disk</span><span class="sv" style="color:'+M+'">N/A (unlimited hosting)</span></div>';
  }else if(!s.has_agent){
    h+='<hr><p style="color:'+M+';font-size:12px">HTTP-only &#8212; no agent installed</p>';
  }
  if(s.url){
    h+='<hr><img id="mthumb" src="/api/thumb?url='+encodeURIComponent(s.url)+'" style="width:100%;border-radius:4px;display:none">'
      +'<p id="mthumb-msg" style="color:#7c8597;font-size:11px;margin-top:4px;display:none">Preview not cached yet &mdash; open detail on the panel first</p>';
  }
  document.getElementById('mb').innerHTML=h;
  const ti=document.getElementById('mthumb');
  if(ti){
    ti.onload=function(){this.style.display='block';const m=document.getElementById('mthumb-msg');if(m)m.remove()};
    ti.onerror=function(){
      this.remove();
      const m=document.getElementById('mthumb-msg');
      if(!m)return;
      m.textContent='Loading preview...';m.style.display='block';
      fetch('/api/fetch-thumb?url='+encodeURIComponent(s.url)).then(function(){
        let n=0;
        const poll=setInterval(function(){
          n++;
          fetch('/api/thumb?url='+encodeURIComponent(s.url)).then(function(r){
            if(r.ok){
              clearInterval(poll);
              const img=document.createElement('img');
              img.src='/api/thumb?url='+encodeURIComponent(s.url)+'&_='+n;
              img.style.cssText='width:100%;border-radius:4px';
              m.replaceWith(img);
            }else if(n>=12){clearInterval(poll);m.textContent='Preview unavailable'}
          }).catch(function(){if(n>=12)clearInterval(poll)});
        },5000);
      }).catch(function(){m.textContent='Preview unavailable'});
    };
  }
  document.getElementById('ov').classList.add('open');
  requestAnimationFrame(()=>{
    if(hist.length){
      const h1=document.getElementById('mh1'),h2=document.getElementById('mh2');
      if(h1){
        const dpr=devicePixelRatio||1,w=h1.offsetWidth,n=hist.length,bw=w/n;
        [h1,h2].forEach(c=>{c.width=Math.round(w*dpr);c.height=Math.round(14*dpr)});
        const c1=h1.getContext('2d'),c2=h2.getContext('2d');
        c1.scale(dpr,dpr);c2.scale(dpr,dpr);
        for(let i=0;i<n;i++){
          c1.fillStyle=hist[i].host_ok?G:R;c1.fillRect(i*bw,6,Math.max(1,bw),2);
          c2.fillStyle=hist[i].page_ok?G:R;c2.fillRect(i*bw,6,Math.max(1,bw),2);
        }
      }
    }
    const sc=document.getElementById('msc'),sm=document.getElementById('msm'),sd=document.getElementById('msd');
    if(sc&&cpu.length)spark(sc,cpu,24);
    if(sm&&mem.length)spark(sm,mem,24);
    if(sd&&dsk.length)spark(sd,dsk,24);
  });
}
function closeModal(){document.getElementById('ov').classList.remove('open')}
function updateAgo(){
  if(!data)return;
  const s=Math.max(0,Math.round(Date.now()/1000-data.last_update));
  document.getElementById('ago').textContent=s<60?s+'s ago':Math.floor(s/60)+'m '+s%60+'s ago';
}
async function load(){
  try{const r=await fetch('/api/status');render(await r.json())}catch(e){console.error(e)}
}
load();setInterval(load,35000);setInterval(updateAgo,5000);
</script>
</body>
</html>
"""

class _WebHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass   # silence access log

    def _is_authed(self):
        if not _web_token:
            return True
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k.strip() == 'piping_auth' and v.strip() == _web_token:
                return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == '/login':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_LOGIN_HTML)
            return

        if not self._is_authed():
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return

        if path == '/api/status':
            with _web_lock:
                body = _web_snapshot
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/fetch-thumb':
            url = params.get('url', [''])[0]
            started = False
            if url and _screenshot_api_token:
                with _web_lock:
                    if url not in _web_thumbs and url not in _web_fetch_pending:
                        _web_fetch_pending.add(url)
                        started = True
                if started:
                    threading.Thread(target=_web_thumb_fetch, args=(url,), daemon=True).start()
            status = ('cached' if url in _web_thumbs else
                      'pending' if url in _web_fetch_pending else
                      'started' if started else 'unavailable')
            self.send_response(202 if started else 200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': status}).encode())
        elif path == '/api/thumb':
            url = params.get('url', [''])[0]
            img = _web_thumbs.get(url)
            if img:
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(img)
            else:
                self.send_response(404)
                self.end_headers()
        elif path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_WEB_HTML)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != '/login':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode('utf-8', 'ignore')
        key    = urllib.parse.parse_qs(body).get('key', [''])[0]
        if _web_token and key == _web_token:
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie',
                f'piping_auth={_web_token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax')
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header('Location', '/login?err=1')
            self.end_headers()


class _WebServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_web_server(port: int):
    server = _WebServer(("", port), _WebHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[web] listening on http://0.0.0.0:{port}", flush=True)


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
            _web_thumbs[url] = data
        else:
            self._thumb_failed.add(url)
        if self.detail_index is not None:
            self.update()

    def _rebuild_hist_pixmaps(self):
        self._hist_pixmaps.clear()
        # 5 lines × 2px + 4 gaps × 1px = 14px total
        # order top→bottom: host, page, CPU, MEM, DSK
        BARS = [
            (0,  lambda e: GREEN if e["host_ok"] else RED),
            (3,  lambda e: GREEN if e["page_ok"] else RED),
            (6,  lambda e: _gauge_col(e["cpu"])  if e.get("cpu")  is not None else TRACK),
            (9,  lambda e: _gauge_col(e["mem"])  if e.get("mem")  is not None else TRACK),
            (12, lambda e: _gauge_col(e["disk"]) if e.get("disk") is not None else TRACK),
        ]
        for r in self.results:
            hist = self.history.get(r.url or r.name, [])
            if not hist:
                continue
            bars = BARS[:]
            if r.skip_disk:
                bars[4] = (12, lambda e: TRACK)
            pm = QPixmap(317, 14)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            n = len(hist)
            bw = 317 / n
            for bar_y, col_fn in bars:
                for i, e in enumerate(hist):
                    pp.fillRect(QRectF(i * bw, bar_y, max(1, bw), 2), col_fn(e))
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
        _make_web_snapshot(self.results, self.history, self.last_update)
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

            cy = top + (ROW_H - 14) / 2
            self._light(p, 13, cy, r.host_ok)
            self._light(p, 32, cy, r.page_ok)

            name_y = top + (ROW_H - 14 - 35) / 2
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
                self._arc(p, 298, cy,
                          None if r.skip_disk else (r.disk.get("percent") if r.disk else None),
                          "DSK", na_text="NA" if r.skip_disk else "—")
            else:
                p.setPen(MUTED)
                p.setFont(QFont("DejaVu Sans", 7))
                tag = "HTTP only" if not r.has_agent else (r.error or "no data")
                p.drawText(QRectF(185, top, 130, ROW_H), Qt.AlignmentFlag.AlignCenter, tag)

            # history bars — pre-rendered pixmap, one drawPixmap per card
            pm = self._hist_pixmaps.get(r.url or r.name)
            if pm:
                p.drawPixmap(0, int(top + ROW_H - 15), pm)

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
            if disk_vals and not r.skip_disk:
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
            if r.disk and not r.skip_disk:
                d = r.disk
                stat_line("Disk", f"{d.get('percent')}%  ({d.get('used_gb')} / {d.get('total_gb')} GB)")
            elif r.skip_disk:
                p.setPen(MUTED)
                p.drawText(QRectF(12, y, 295, 18), Qt.AlignmentFlag.AlignVCenter, "Disk")
                p.setPen(MUTED)
                p.drawText(QRectF(84, y, 221, 18), Qt.AlignmentFlag.AlignVCenter, "N/A (unlimited hosting)")
                y += 20
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

    def _arc(self, p, cx, cy, percent, label, na_text="—"):
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
            p.drawText(QRectF(cx - R, cy - 1, R * 2, 12), Qt.AlignmentFlag.AlignCenter, na_text)

    def closeEvent(self, ev):
        self.poller.stop()
        self.poller.wait(2000)
        ev.accept()


def main():
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = json.loads(CONFIG_PATH.read_text())

    global _screenshot_api_token, _web_token
    _screenshot_api_token = config.get("screenshot_api_token", "")
    _web_token            = config.get("web_token", "")

    start_web_server(config.get("web_port", 8080))

    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.CursorShape.BlankCursor)
    w = Panel(config)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
