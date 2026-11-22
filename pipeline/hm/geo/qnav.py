"""fteqw-sv NextBot nav meshes (<gamedir>/nav/<map>.qnav, version 8; engine/server/nextbot/nb_nav.c):

  header 52 bytes: int magic 'QNAV', int version, uint checksum, uint numareas, uint numlinks,
                   char gamedir[32]
  area   52 bytes: float mins[3], maxs[3], z[4] (floor heights at (minx,miny), (maxx,miny),
                   (maxx,maxy), (minx,maxy)), uint flags, uint firstlink, ushort numlinks,
                   uchar teamblock, pad
  link    8 bytes: uint to, uchar type, pad, ushort cost

Returned in the same shape as nav_source.load (areas with nw/se corners and per-corner heights), so
the site's nav drawing and gap / missing-link analysis work unchanged.
"""
import os
import struct

MAGIC = (ord("V") << 24) | (ord("A") << 16) | (ord("N") << 8) | ord("Q")
VERSIONS = (8,)
MAX_FILE = 64 * 1024 * 1024
MAX_AREAS = 200000


def load(path):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("nav file too large")
    d = open(path, "rb").read()
    if len(d) < 52:
        raise ValueError("not a qnav file")
    magic, version, checksum, na, nl = struct.unpack_from("<iiIII", d, 0)
    if magic != MAGIC:
        raise ValueError("not a qnav file")
    if version not in VERSIONS:
        raise ValueError("unsupported qnav version %d" % version)
    if na > MAX_AREAS or nl > MAX_AREAS * 64 or len(d) != 52 + 52 * na + 8 * nl:
        raise ValueError("qnav size mismatch")
    links = [struct.unpack_from("<IBxH", d, 52 + 52 * na + 8 * i) for i in range(nl)]
    areas = []
    for i in range(na):
        o = 52 + 52 * i
        v = struct.unpack_from("<10f", d, o)
        flags, first, cnt = struct.unpack_from("<IIH", d, o + 40)
        if not all(-65536.0 < c < 65536.0 for c in v):
            raise ValueError("bad area coordinates")
        mins, maxs, z = v[0:3], v[3:6], v[6:10]
        conn = [links[j][0] for j in range(first, min(first + cnt, nl)) if links[j][0] < na]
        areas.append({"id": i, "attr": flags,
                      "nw": (mins[0], mins[1], z[0]), "se": (maxs[0], maxs[1], z[2]),
                      "nez": z[1], "swz": z[3], "conn": conn})
    gamedir = d[20:52].split(b"\0")[0].decode("latin-1")
    return {"version": version, "subversion": 0, "areas": areas, "places": [], "gamedir": gamedir}
