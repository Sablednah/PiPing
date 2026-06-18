# PiPing

A site-monitoring kiosk for a Raspberry Pi 3B+ with a 3.5" SPI touchscreen. Displays a scrollable grid of monitored websites — status lights, response checks, grep phrase validation, and live server gauges (CPU / memory / disk) from an optional PHP agent on each host.

![PiPing panel](screenshot.png)

---

## Hardware

| Part | Notes |
|------|-------|
| Raspberry Pi 3B+ | 1 GB RAM |
| Waveshare-style 3.5" Display-G | 480×320, ST7796S silkscreen, GW1NZ-1 FPGA bridge, XPT2046 touch |

> **Display driver note:** the working driver is `fbtft` / `mhs35` overlay presenting as `fb_ili9486`. The modern DRM `panel-mipi-dbi` / `st7796s` driver does **not** work with this board — the FPGA bridge doesn't behave like a bare ST7796S. Do not attempt to use it.

---

## How it works

- **`pi/monitor.py`** — PyQt6 app. Polls all sites on a background thread; renders directly to `/dev/fb1` (no desktop). Two views: scrollable grid and tap-to-detail.
- **`pi/config.json`** — list of sites and shared agent token. One-line edit to add a site.
- **`agent/status.php`** — deploy to each web host you want server stats from. Reports CPU load, memory, and disk via non-root methods (works on shared hosting). Sites without an agent get HTTP-only checks.
- **`pi/monitor.service`** — systemd unit for autostart on boot.
- **`deploy.sh`** — rsync `pi/` from your dev machine to the Pi and restart the service.

---

## First-time Pi setup

### 1. Display driver (if not already configured)

In `/boot/firmware/config.txt`, ensure KMS is off and the `mhs35` overlay is loaded:

```
# Comment out or remove this line:
# dtoverlay=vc4-kms-v3d

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

In `/boot/firmware/cmdline.txt`, add to the **single existing line** (do not add a new line):

```
fbcon=map:10 fbcon=font:ProFont6x11
```

Reboot. You should see `/dev/fb1` appear.

### 2. Install Python dependency

```bash
sudo apt install python3-pyqt6 -y
```

### 3. Create the app directory

```bash
mkdir -p /home/sable/pi-status-panel/pi
```

---

## Deploying the app

Clone this repo on your **development machine** (not the Pi — it only has 1 GB RAM):

```bash
git clone https://github.com/Sablednah/PiPing.git
cd PiPing
```

Edit `pi/config.json` — at minimum set a strong `agent_token` and add your sites (see [Configuration](#configuration) below).

Push to the Pi:

```bash
./deploy.sh
# or with an explicit host:
./deploy.sh sable@192.168.1.x
```

The default host is `sable@192.168.6.130` — edit `deploy.sh` to change it.

### Install the systemd service (first time only)

```bash
ssh sable@<pi-ip> "sudo cp /home/sable/pi-status-panel/pi/monitor.service /etc/systemd/system/monitor.service \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable --now monitor.service"
```

After the first install, `deploy.sh` handles restarting the service automatically on every subsequent deploy.

### Check it's running

```bash
ssh sable@<pi-ip> "journalctl -fu monitor.service"
```

---

## Configuration

`pi/config.json`:

```json
{
  "poll_interval_seconds": 30,
  "http_timeout_seconds": 10,
  "agent_token": "your-secret-token-here",

  "sites": [
    {
      "name": "My Site",
      "url": "https://example.com/",
      "agent": "https://example.com/_status/status.php",
      "grep": "Expected phrase on the page"
    },
    {
      "name": "HTTP Only",
      "url": "https://another.com/",
      "agent": null,
      "grep": ""
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | Display name (keep short) |
| `url` | Full URL for the outsider HTTP check |
| `agent` | URL of the deployed `status.php`, or `null` for HTTP-only |
| `grep` | A phrase that must appear in the page HTML for `page_ok` to be green. Leave empty to skip. |

The **host light** (left dot) goes green if the server responds at all. The **page light** (right dot) goes green if the response is HTTP 200 and the grep phrase is found (or no phrase is set).

---

## Deploying the PHP agent

The agent gives you live CPU / memory / disk gauges for each host.

1. Copy `agent/status.php` to a web-accessible path on the host, e.g.:
   ```
   public_html/_status/status.php
   ```
   A non-obvious path is fine — the token is the real protection.

2. Set the token. Either edit `$EXPECTED_TOKEN` in the file, or set a `STATUS_TOKEN` environment variable on the host. **The script refuses to run if the token is left as the default placeholder.**

3. Use the **same token** in `pi/config.json` → `agent_token`.

4. Test from the Pi:
   ```bash
   curl -H "X-Status-Token: YOUR_TOKEN" https://example.com/_status/status.php
   ```
   You should get a JSON response with `"ok": true`.

> **Note for 20i / Heart Internet hosted sites:** the server blocks requests without a `User-Agent` and `Referer` header. The app sends both automatically.

---

## Using the panel

| Action | Result |
|--------|--------|
| Swipe up/down | Scroll the site list |
| Tap a site row | Open detail view (HTTP status, response time, title, grep result, server stats) |
| Tap anywhere in detail | Return to grid |
| Tap the **"Site Status"** header | Force an immediate repoll |

---

## Taking a screenshot

```bash
./screenshot.sh              # saves panel.png in the repo root
./screenshot.sh myshot.png   # custom filename
PI_HOST=sable@192.168.1.x ./screenshot.sh
```

---

## Development workflow

Edit files on your dev machine, then:

```bash
./deploy.sh
```

This rsyncs `pi/` to the Pi and restarts the service. If you change `monitor.service` itself, re-run the install command above to copy it into `/etc/systemd/system/` and reload systemd.
