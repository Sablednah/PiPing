# Pi Status Panel

A site-monitoring kiosk: a 3.5" touchscreen on a Raspberry Pi 3B+ shows a
grid of monitored websites with status lights and per-host CPU/memory/disk
gauges. Tap a site for a detail page. PHP agents on each host gather server
stats; the Pi polls them plus does its own outsider HTTP checks.

## Architecture

- **`agent/status.php`** — deployed to each *web host* (not the Pi). Reports
  CPU load, memory, disk using **non-root** methods (works on shared hosting).
  Auth via `X-Status-Token` header (`hash_equals`). Hybrid model: hosts with
  an agent get full gauges; sites without one get HTTP-only checks.
- **`pi/monitor.py`** — PyQt6 app on the Pi. Polls agents + does outsider
  HTTP 200 + grep-phrase + `<title>` checks on a worker thread. Renders
  directly to the framebuffer (no desktop).
- **`pi/config.json`** — the list of sites + the shared agent token. Adding a
  site is a one-line edit. `agent` set → full stats; `agent: null` → HTTP only.
- **`pi/monitor.service`** — systemd unit, autostart on boot.
- **`deploy.sh`** — rsync the `pi/` folder from dev machine → Pi, restart svc.

## Dev workflow

Develop on the **WSL2 / desktop** machine (the Pi 3B+ has only 1GB RAM — do
NOT run Claude Code or heavy tooling on the Pi itself; it will thrash swap).
Edit here, then `./deploy.sh` to push to the Pi and restart the service.

The PHP agent is tested separately by deploying `status.php` to a web host and
curling it.

## THE DISPLAY — hard-won knowledge, read before touching display config

The panel is a **Waveshare-style "3.5 inch Display-G"**, 480x320. Critically:

- The visible controller silkscreen says ST7796S, and there's a **GW1NZ-1
  FPGA** (U1) bridging SPI to the panel, plus an **XPT2046** touch controller.
- **The modern DRM `panel-mipi-dbi` / st7796s driver does NOT work** with this
  board. We proved the driver sends a complete, correct init sequence and full
  pixel flush over SPI, yet the panel stayed white. The FPGA bridge does not
  behave like a bare ST7796S to that driver. Hours were lost here — do not
  revisit the `panel-mipi-dbi` route.
- **What works: the legacy `fbtft` `fb_ili9486` driver via the `mhs35`
  overlay.** The FPGA presents as an ILI9486. This is confirmed working on
  64-bit Bookworm kernel `6.12.47+rpt-rpi-v8` (the overlay + module ship in
  the kernel's staging tree — no compiling/DKMS needed).

### Working `/boot/firmware/config.txt` (display section)

```
# KMS must be OFF for fbtft (they conflict):
#dtoverlay=vc4-kms-v3d

[all]
dtparam=spi=on
dtparam=i2c_arm=on
enable_uart=1
dtoverlay=mhs35
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt 480 320 60 6 0 0 0
hdmi_drive=2
```

### Working `/boot/firmware/cmdline.txt` additions (single line!)

```
fbcon=map:10 fbcon=font:ProFont6x11
```

### Result
- `/dev/fb1` with name `fb_ili9486`, 480x320.
- Touch: `ads7846` module, registers as an input device automatically.
- The app draws to `/dev/fb1` via Qt's `linuxfb` plugin.

### Fallback
A 32-bit goodtft/Waveshare vendor image (kernel 4.14, `mhs35` overlay,
`fb_ili9486` + `fbcp`) is known to fully work (display+touch+desktop). Kept as
a spare SD card if the 64-bit setup ever breaks.

### Power
Use the official Pi supply. Generic 65W USB-PD bricks caused undervoltage
warnings (they sag below 5.1V under load + LCD backlight current). Undervoltage
can corrupt the SD and cause flaky SPI.

## Running locally (on the Pi)

```
QT_QPA_PLATFORM=linuxfb:fb=/dev/fb1 python3 monitor.py
```

## First install (on the Pi)

```
sudo apt install python3-pyqt6 -y
# copy repo to /home/sable/pi-status-panel
sudo cp pi/monitor.service /etc/systemd/system/monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now monitor.service
```

## Deploying the PHP agent (per host)

1. Copy `agent/status.php` to a web-accessible (non-obvious) path, e.g.
   `public_html/_status/status.php`.
2. Set `STATUS_TOKEN` env var, or edit `$EXPECTED_TOKEN` in the file. It
   REFUSES to run while the placeholder token is unchanged.
3. Put the same token in `pi/config.json` `agent_token`.
4. Test: `curl -H "X-Status-Token: TOKEN" https://site/_status/status.php`

## Monitored sites (current)

- 10K Used (10kused.com) — grep: "This is a trade website for B2B sales."
- Clonezone (clonezonedirect.co.uk) — grep: "Clonezone is the trading name of Libertybelle UK Ltd"
- 1on1 Wholesale (1on1wholesale.co.uk) — HTTP-only for now
- Cara Sutra (carasutra.com) — HTTP-only for now

## Roadmap / v2 ideas
- Preview thumbnail per site (needs a headless renderer — deferred).
- Magento/WordPress app-level health (cron, DB reachable) via the agent.
- Touch gestures beyond tap (swipe between pages of sites if list grows).
- Alert state / colour flash when a site drops.
