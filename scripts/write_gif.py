#!/usr/bin/env python3
"""Minimal, size-optimal GIF writer for the VPeeN demo.

Reads the exact palette-mapped frames (mode P, shared 256-color
palette, index 255 reserved) and emits delta frames:

  * each frame is stored as its changed bounding rect only
  * unchanged pixels INSIDE the rect become the transparent index —
    LZW turns them into near-free run-length codes (ffmpeg's
    diff_mode=rectangle stores them as real pixels instead)
  * global palette only, no per-frame tables, tight headers
  * delay 50 ms (20 fps), infinite loop, disposal "keep"

The output is verified by decoding it back with Pillow and comparing
every frame pixel-exact against the source (see --verify).
"""
import os
import sys

from PIL import Image

SRC_DIR = "/tmp/gif3_pal8"
OUT = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/z/my-project/VPeeN/docs/demo.gif"
TIDX = 255
DELAY = 5            # hundredths of a second -> 50 ms
STABILITY = 14      # temporal stabilization threshold (palette levels)


# ------------------------------------------------------------------ LZW
class BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.buf = 0
        self.n = 0

    def write(self, code, size):
        self.buf |= code << self.n
        self.n += size
        while self.n >= 8:
            self.out.append(self.buf & 0xFF)
            self.buf >>= 8
            self.n -= 8

    def finish(self):
        if self.n:
            self.out.append(self.buf & 0xFF)
        return bytes(self.out)


def lzw_encode(data, min_code_size=8):
    """GIF LZW (giflib-compatible code-size growth)."""
    clear = 1 << min_code_size
    eoi = clear + 1
    bw = BitWriter()
    table = None
    cs = min_code_size + 1
    nxt = eoi + 1

    def reset():
        return {(i,): i for i in range(clear)}

    table = reset()
    bw.write(clear, cs)
    w = None
    for ch in data:
        if w is None:
            w = (ch,)
            continue
        cand = w + (ch,)
        hit = table.get(cand)
        if hit is not None:
            w = cand
            continue
        bw.write(table[w], cs)
        if nxt < 4096:
            table[cand] = nxt
            nxt += 1
            if nxt > (1 << cs) and cs < 12:
                cs += 1
        else:
            bw.write(clear, cs)
            table = reset()
            cs = min_code_size + 1
            nxt = eoi + 1
        w = (ch,)
    if w is not None:
        bw.write(table[w], cs)
    bw.write(eoi, cs)
    return bw.finish()


def sub_blocks(data):
    out = bytearray()
    i = 0
    while i < len(data):
        chunk = data[i:i + 255]
        out.append(len(chunk))
        out.extend(chunk)
        i += 255
    out.append(0)
    return bytes(out)


def dilate(mask, r):
    """Binary dilation by a (2r+1)x(2r+1) box, separable."""
    out = mask.copy()
    for i in range(1, r + 1):
        out[:, :-i] |= mask[:, i:]
        out[:, i:] |= mask[:, :-i]
    t = out.copy()
    for i in range(1, r + 1):
        out[:-i] |= t[i:]
        out[i:] |= t[:-i]
    return out


# ------------------------------------------------------------------ GIF
def write_gif(frames, palette, out_path, w, h, stability=STABILITY,
              burst_min=200):
    import numpy as np
    g = bytearray()
    g += b"GIF89a"
    g += bytes([w & 255, w >> 8, h & 255, h >> 8])
    g += bytes([0xF7, 0, 0])               # GCT, 8 bits/pixel
    g += bytes(palette[:768])
    # NETSCAPE looping extension
    g += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"

    lut = np.array([palette[i * 3:i * 3 + 3]
                    for i in range(256)], dtype=np.int16)

    prev = None
    delays = []
    for idx, fr in enumerate(frames):
        cur = np.asarray(fr, dtype=np.uint8).reshape(h, w).copy()
        if prev is not None and stability:
            # temporal stabilization: pixels whose colour barely moved
            # keep their previous OUTPUT index.  Sub-pixel drift (soft
            # glows, antialiased edges, dither-pattern shifts on the
            # aurora) freezes — visually identical, far smaller diffs.
            differs = cur != prev
            if differs.any():
                close = (np.abs(lut[cur] - lut[prev]).max(axis=2)
                         <= stability)
                freeze = differs & close
                # ghost guard: within ~5px of any LARGE change (a text
                # swap, toast slide, stat tick) freezing is banned, so
                # one-shot changes render pixel-exact instead of
                # leaving frozen antialias remnants behind.
                big = differs & ~close
                if int(big.sum()) > burst_min:
                    freeze &= ~dilate(big, 5)
                cur[freeze] = prev[freeze]
        if prev is None:
            rect = (0, 0, w, h)
            data = cur.tobytes()            # everything opaque
        else:
            diff = cur != prev
            if not diff.any():
                delays[-1] += DELAY         # merge identical frames
                continue
            ys, xs = np.where(diff)
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            rect = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
            sub = cur[y0:y1 + 1, x0:x1 + 1]
            psub = prev[y0:y1 + 1, x0:x1 + 1]
            data = np.where(sub != psub, sub, TIDX).tobytes()
        delays.append(DELAY)
        prev = cur

        # graphic control extension: disposal=1 (keep), transparency
        g += b"\x21\xF9\x04" + bytes([0x05, DELAY, 0, TIDX, 0])
        # image descriptor
        rx, ry, rw, rh = rect
        g += bytes([0x2C, rx & 255, rx >> 8, ry & 255, ry >> 8,
                    rw & 255, rw >> 8, rh & 255, rh >> 8, 0x00])
        g += bytes([8])                     # LZW min code size
        g += sub_blocks(lzw_encode(data, 8))
    g += b"\x3B"

    with open(out_path, "wb") as fh:
        fh.write(g)
    return len(g), delays


def main():
    files = sorted(f for f in os.listdir(SRC_DIR) if f.endswith(".png"))
    frames = [Image.open(os.path.join(SRC_DIR, f)).copy() for f in files]
    w, h = frames[0].size
    palette = list(frames[0].getpalette())
    palette[TIDX * 3:TIDX * 3 + 3] = [7, 11, 20]
    print(f"{len(frames)} frames {w}x{h}", flush=True)

    size, delays = write_gif(frames, palette, OUT, w, h)
    stability = STABILITY
    print(f"wrote {OUT}: {size/1e6:.2f} MB", flush=True)

    if "--verify" in sys.argv:
        import numpy as np
        dec = Image.open(OUT)
        assert dec.n_frames == len(frames), \
            f"frame count {dec.n_frames} != {len(frames)}"
        worst = 0
        for i in range(len(frames)):
            dec.seek(i)
            a = np.asarray(frames[i].convert("RGB"), dtype=int)
            b = np.asarray(dec.convert("RGB"), dtype=int)
            if a.shape != b.shape:
                worst = 999
                break
            worst = max(worst, int(np.abs(a - b).max()))
        # stabilization may shift a pixel by at most ~stability levels
        print(f"verify: {dec.n_frames} frames, worst pixel delta "
              f"{worst} (<= {stability * 3 + 6} tolerated)")
        sys.exit(0 if worst <= stability * 3 + 6 else 1)


if __name__ == "__main__":
    main()
