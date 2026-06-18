#!/usr/bin/env bash
# screenshot.sh - capture the Pi panel framebuffer as a PNG.
# Usage: ./screenshot.sh [output.png]
# Default output: panel.png in the repo root.

set -euo pipefail

PI_HOST="${PI_HOST:-sable@192.168.6.130}"
OUT="${1:-$(dirname "$0")/panel.png}"

ssh "${PI_HOST}" "python3 -c \"
from PIL import Image
import struct

W, H = 480, 320
with open('/dev/fb1', 'rb') as f:
    raw = f.read(W * H * 2)

pixels = []
for i in range(0, len(raw), 2):
    px = struct.unpack_from('<H', raw, i)[0]
    r = (px >> 11 & 0x1f) << 3
    g = (px >> 5  & 0x3f) << 2
    b = (px       & 0x1f) << 3
    pixels.extend([r, g, b])

Image.frombytes('RGB', (W, H), bytes(pixels)).rotate(90, expand=True).save('/tmp/panel.png')
\""

scp "${PI_HOST}:/tmp/panel.png" "${OUT}"
echo "Saved to ${OUT}"
