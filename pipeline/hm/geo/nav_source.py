"""Source engine .nav (versions 6-16) reader: areas, their corners and connections.

Mods extend the per-area data (TF2 adds 4 attribute bytes, some 4-team mods store 4 occupy times),
so the loader tries the plausible layouts and keeps the one that parses every area consistently.
"""
import struct


class _R:
    __slots__ = ("b", "p")

    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u8(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def u16(self):
        v, = struct.unpack_from("<H", self.b, self.p)
        self.p += 2
        return v

    def u32(self):
        v, = struct.unpack_from("<I", self.b, self.p)
        self.p += 4
        return v

    def f32(self):
        v, = struct.unpack_from("<f", self.b, self.p)
        self.p += 4
        return v

    def vec(self):
        v = struct.unpack_from("<3f", self.b, self.p)
        self.p += 12
        return v

    def skip(self, n):
        self.p += n
        if self.p > len(self.b):
            raise EOFError


def _header(b):
    r = _R(b)
    if r.u32() != 0xFEEDFACE:
        raise ValueError("not a nav file")
    ver = r.u32()
    sub = r.u32() if ver >= 10 else 0
    if ver >= 4:
        r.u32()             # bsp size
    if ver >= 14:
        r.u8()              # analyzed
    places = []
    if ver >= 5:
        for _ in range(r.u16()):
            ln = r.u16()
            places.append(b[r.p:r.p + ln].rstrip(b"\0").decode("utf-8", "replace"))
            r.skip(ln)
        if ver > 11:
            r.u8()          # has unnamed areas
    return r, ver, sub, places


def _areas(b, start, ver, occupy, custom, limit=None):
    r = _R(b, start)
    count = r.u32()
    if count == 0 or count > 200000:
        raise ValueError("bad area count")
    areas = []
    for _ in range(count if limit is None else min(count, limit)):
        aid = r.u32()
        if ver <= 8:
            attr = r.u8()
        elif ver <= 12:
            attr = r.u16()
        else:
            attr = r.u32()
        nw = r.vec()
        se = r.vec()
        nez = r.f32()
        swz = r.f32()
        for v in nw + se + (nez, swz):
            if not (-40000.0 < v < 40000.0):   # also false for nan/inf
                raise ValueError("bad coordinate")
        conns = []
        for d in range(4):
            n = r.u32()
            if n > 4096:
                raise ValueError("bad connection count")
            for _ in range(n):
                conns.append(r.u32())
        hs = r.u8()
        r.skip(hs * (17 if ver >= 2 else 16))
        if ver < 15:
            r.skip(r.u8() * 14)       # approach areas
        enc = r.u32()
        if enc > 100000:
            raise ValueError("bad encounter count")
        for _ in range(enc):
            r.skip(4 + 1 + 4 + 1)
            r.skip(r.u8() * 5)
        place = r.u16() if ver >= 5 else 0
        ladders = []
        for d in range(2):
            n = r.u32()
            if n > 1024:
                raise ValueError("bad ladder count")
            for _ in range(n):
                ladders.append(r.u32())
        if ver >= 8:
            r.skip(4 * occupy)
        if ver >= 11:
            r.skip(16)                # light intensity x4
        if ver >= 16:
            vis = r.u32()
            if vis > 200000:
                raise ValueError("bad visibility count")
            r.skip(vis * 5 + 4)
        r.skip(custom)
        areas.append({"id": aid, "attr": attr, "nw": nw, "se": se, "nez": nez, "swz": swz,
                      "conn": conns, "ladders": ladders, "place": place})
    return areas, r.p, count


def _tail_left(b, end):
    """Bytes left after the ladder list (0 = the layout consumed the file exactly)."""
    if end + 4 > len(b):
        return -1
    n, = struct.unpack_from("<I", b, end)
    if n > 5000:
        return -1
    left = len(b) - (end + 4 + n * 60)
    return left


MAX_NAV = 128 * 1024 * 1024


def load(path):
    import os
    if os.path.getsize(path) > MAX_NAV:
        raise ValueError("nav file too large")
    b = open(path, "rb").read()
    r, ver, sub, places = _header(b)
    best = None
    for occupy in (2, 4):
        for custom in list(range(0, 33)):
            for pre in (0, 4):
                try:
                    areas, end, count = _areas(b, r.p + pre, ver, occupy, custom)
                except (ValueError, EOFError, struct.error, IndexError):
                    continue
                ids = {a["id"] for a in areas}
                if len(ids) != count:
                    continue
                bad = sum(1 for a in areas for c in a["conn"] if c not in ids)
                if bad > count * 0.01:
                    continue
                left = _tail_left(b, end)
                if left < 0:
                    continue
                # prefer an exact fit, then the smallest unexplained tail, then the simplest layout;
                # keep only the best candidate, and stop at the first exact fit
                key = (left != 0, left, pre, custom, occupy)
                if best is None or key < best[0]:
                    best = (key, areas)
                if left == 0:
                    break
            if best and best[0][0] is False:
                break
        if best and best[0][0] is False:
            break
    if best is None:
        raise ValueError("could not parse nav layout (version %d.%d)" % (ver, sub))
    (_, left, pre, custom, occupy), areas = best
    return {"version": ver, "subversion": sub, "places": places, "areas": areas,
            "layout": {"occupy": occupy, "custom": custom, "pre": pre, "tail": left}}
