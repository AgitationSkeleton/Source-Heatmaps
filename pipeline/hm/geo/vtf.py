"""Decode the top mip of a VTF texture to a PIL image (formats used by HUD icons)."""
import struct

import numpy as np
from PIL import Image

# format id -> bytes per pixel (or block size for DXT)
RGBA8888, ABGR8888, RGB888, BGR888, I8, IA88, A8, ARGB8888, BGRA8888, DXT1, DXT3, DXT5, BGRX8888 = 0, 1, 2, 3, 5, 6, 8, 11, 12, 13, 14, 15, 16
BPP = {RGBA8888: 4, ABGR8888: 4, RGB888: 3, BGR888: 3, I8: 1, IA88: 2, A8: 1, ARGB8888: 4, BGRA8888: 4, BGRX8888: 4}
DXT_BLOCK = {DXT1: 8, DXT3: 16, DXT5: 16}


def _size(fmt, w, h):
    if fmt in DXT_BLOCK:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * DXT_BLOCK[fmt]
    return w * h * BPP[fmt]


def _565(c):
    r = ((c >> 11) & 31) * 255 // 31
    g = ((c >> 5) & 63) * 255 // 63
    b = (c & 31) * 255 // 31
    return np.array([r, g, b], dtype=np.int32)


def _dxt(data, w, h, fmt):
    out = np.zeros((max(h, 4), max(w, 4), 4), dtype=np.uint8)
    bs = DXT_BLOCK[fmt]
    p = 0
    for by in range(0, max(h, 4), 4):
        for bx in range(0, max(w, 4), 4):
            blk = data[p:p + bs]
            p += bs
            alpha = None
            cb = blk
            if fmt == DXT3:
                a = int.from_bytes(blk[:8], "little")
                alpha = [((a >> (4 * i)) & 15) * 17 for i in range(16)]
                cb = blk[8:]
            elif fmt == DXT5:
                a0, a1 = blk[0], blk[1]
                bits = int.from_bytes(blk[2:8], "little")
                pal = [a0, a1]
                if a0 > a1:
                    pal += [((6 - i) * a0 + (1 + i) * a1) // 7 for i in range(6)]
                else:
                    pal += [((4 - i) * a0 + (1 + i) * a1) // 5 for i in range(4)] + [0, 255]
                alpha = [pal[(bits >> (3 * i)) & 7] for i in range(16)]
                cb = blk[8:]
            c0, c1, idx = struct.unpack("<HHI", cb[:8])
            k0, k1 = _565(c0), _565(c1)
            if c0 > c1 or fmt != DXT1:
                cols = [k0, k1, (2 * k0 + k1) // 3, (k0 + 2 * k1) // 3]
                ca = [255, 255, 255, 255]
            else:
                cols = [k0, k1, (k0 + k1) // 2, np.zeros(3, np.int32)]
                ca = [255, 255, 255, 0]
            for i in range(16):
                s = (idx >> (2 * i)) & 3
                y, x = by + i // 4, bx + i % 4
                out[y, x, :3] = cols[s]
                out[y, x, 3] = alpha[i] if alpha is not None else ca[s]
    return out[:h, :w]


def decode(blob):
    if blob[:4] != b"VTF\0":
        raise ValueError("not a VTF")
    vmaj, vmin, hsize = struct.unpack_from("<III", blob, 4)
    w, h, flags, frames, first = struct.unpack_from("<HHIHH", blob, 16)
    fmt, = struct.unpack_from("<i", blob, 52)
    mips = blob[56]
    lfmt, = struct.unpack_from("<i", blob, 57)
    lw, lh = blob[61], blob[62]
    faces = 6 if flags & 0x4000 else 1
    if fmt not in BPP and fmt not in DXT_BLOCK:
        raise ValueError("unsupported VTF format %d" % fmt)
    low = _size(lfmt, lw, lh) if lfmt >= 0 and lw and lh else 0
    start = None
    if (vmaj, vmin) >= (7, 3):
        nres, = struct.unpack_from("<I", blob, 68)
        for i in range(nres):
            tag, rflags, off = struct.unpack_from("<3sBI", blob, 80 + i * 8)
            if tag == b"\x30\x00\x00":
                start = off
    if start is None:
        start = hsize + low
    # mips are stored smallest first; the full-size image comes last
    off = start
    for m in range(mips - 1, 0, -1):
        off += _size(fmt, max(1, w >> m), max(1, h >> m)) * frames * faces
    data = blob[off:off + _size(fmt, w, h)]
    if fmt in DXT_BLOCK:
        arr = _dxt(data, w, h, fmt)
    else:
        a = np.frombuffer(data, dtype=np.uint8).reshape(h, w, BPP[fmt])
        if fmt == RGBA8888:
            arr = a
        elif fmt == BGRA8888:
            arr = a[:, :, [2, 1, 0, 3]]
        elif fmt == ABGR8888:
            arr = a[:, :, [3, 2, 1, 0]]
        elif fmt == ARGB8888:
            arr = a[:, :, [1, 2, 3, 0]]
        elif fmt == BGRX8888:
            arr = np.dstack([a[:, :, [2, 1, 0]], np.full((h, w), 255, np.uint8)])
        elif fmt == RGB888:
            arr = np.dstack([a, np.full((h, w), 255, np.uint8)])
        elif fmt == BGR888:
            arr = np.dstack([a[:, :, [2, 1, 0]], np.full((h, w), 255, np.uint8)])
        elif fmt == I8:
            arr = np.dstack([a[:, :, 0]] * 3 + [np.full((h, w), 255, np.uint8)])
        elif fmt == IA88:
            arr = np.dstack([a[:, :, 0]] * 3 + [a[:, :, 1]])
        else:  # A8
            arr = np.dstack([np.full((h, w), 255, np.uint8)] * 3 + [a[:, :, 0]])
    return Image.fromarray(np.ascontiguousarray(arr), "RGBA")
