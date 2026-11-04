#!/usr/bin/env python3
"""Generate assets/icon.icns from assets/icon.png (PNG-compressed icns).

Modern macOS icns containers accept PNG-compressed entries, so no
icns-encoder dependency is needed: we render each required size with
Pillow, wrap it in the 4-byte OSType + big-endian length header, and
concatenate inside the overall 'ICNS' container.
"""
import io
import struct

from PIL import Image

SRC = "/home/z/my-project/VPeeN/assets/icon.png"
DST = "/home/z/my-project/VPeeN/assets/icon.icns"

# OSType -> pixel size (PNG-compressed).  Covers Finder/Dock/Quick Look.
SIZES = [
    (b"icp4", 16),    # 16x16
    (b"icp5", 32),    # 32x32
    (b"ic07", 128),   # 128x128
    (b"ic08", 256),   # 256x256
    (b"ic09", 512),   # 512x512
    (b"ic10", 1024),  # 512@2x / 1024x1024
]


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def main() -> None:
    base = Image.open(SRC).convert("RGBA")
    entries = []
    for ostype, px in SIZES:
        img = base.resize((px, px), Image.LANCZOS)
        png = _png_bytes(img)
        # entry = OSType(4) + total_length(4, includes header) + payload
        entries.append(ostype + struct.pack(">I", len(png) + 8) + png)

    body = b"".join(entries)
    # container magic is LOWERCASE 'icns' (Apple spec + PyInstaller's
    # hex_signatures check) - uppercase 'ICNS' is rejected as invalid.
    blob = b"icns" + struct.pack(">I", len(body) + 8) + body
    with open(DST, "wb") as fh:
        fh.write(blob)
    print(f"wrote {DST}: {len(blob)} bytes, {len(entries)} entries")


if __name__ == "__main__":
    main()
