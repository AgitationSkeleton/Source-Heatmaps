"""Bot waypoint graphs (GoldSrc bots) -> {"format", "nodes": [[x, y, z, flags]], "links": [[a, b]]}.

  jkbotti   jk_botti .wpt   (HPB_bot lineage; gzip-compressed): 56-byte header, 32-byte waypoints
                            (flags, origin, ...), then per waypoint: int16 count + int16 indices
  foxbot    FoXBot .fwp     (waypoint.h): 52-byte header "FoXBot", 20-byte waypoints
                            (int32 flags, int8 script flags, origin), per-waypoint int16 path lists,
                            255-byte author; older versions are detected by an exact-EOF layout check
  parabot   Parabot .pnf    (pb_mapgraph.cpp): 32-char id, passCount, navpoints (32 bytes: id, type,
                            origin, visits, special, damageComp), then paths (48-byte record + nested
                            waypoint/attack/platform lists when the path owns its data)

Links are directed (a -> b) as the bots store them. Files are untrusted input: sizes and counts are
capped and every index is checked.
"""
import gzip
import os
import struct

MAX_FILE = 32 * 1024 * 1024
MAX_NODES = 65536
MAX_LINKS_PER_NODE = 256


def _read(path):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("waypoint file too large")
    return open(path, "rb").read()


def _finite(v):
    return all(-65536.0 < c < 65536.0 for c in v)       # also rejects nan


def _paths(d, p, n, end):
    """HPB-style path lists: for each node an int16 count then int16 indices."""
    links = []
    for i in range(n):
        if p + 2 > end:
            raise ValueError("path list truncated")
        c, = struct.unpack_from("<h", d, p)
        p += 2
        if not 0 <= c <= MAX_LINKS_PER_NODE or p + 2 * c > end:
            raise ValueError("bad path count")
        for j in struct.unpack_from("<%dh" % c, d, p):
            if not 0 <= j < n:
                raise ValueError("path index out of range")
            links.append([i, j])
        p += 2 * c
    return links, p


def load_jkbotti(path):
    raw = _read(path)
    d = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if len(d) > MAX_FILE or not d.startswith(b"jkbotti\0"):
        raise ValueError("not a jk_botti waypoint file")
    n, = struct.unpack_from("<i", d, 20)
    if not 0 < n <= MAX_NODES or 56 + 32 * n > len(d):
        raise ValueError("bad waypoint count")
    nodes = []
    for i in range(n):
        flags, x, y, z = struct.unpack_from("<i3f", d, 56 + 32 * i)
        if not _finite((x, y, z)):
            raise ValueError("bad waypoint position")
        nodes.append([round(x, 1), round(y, 1), round(z, 1), flags])
    links, _ = _paths(d, 56 + 32 * n, n, len(d))
    return {"format": "jkbotti", "nodes": nodes, "links": links}


def load_foxbot(path):
    d = _read(path)
    if not d.startswith(b"FoXBot\0"):
        raise ValueError("not a FoXBot waypoint file")
    version, flags_, n = struct.unpack_from("<iii", d, 8)
    if not 0 < n <= MAX_NODES:
        raise ValueError("bad waypoint count")
    # version 5: 20-byte waypoints and a 255-byte author at the end; other versions: find the record
    # size and origin offset whose path section ends exactly at EOF (with or without the author)
    for rec, oo, tail in ((20, 8, 255), (20, 8, 0), (16, 4, 255), (16, 4, 0), (24, 12, 255), (24, 12, 0)):
        if 52 + rec * n > len(d):
            continue
        try:
            nodes = []
            for i in range(n):
                fl, = struct.unpack_from("<i", d, 52 + rec * i)
                v = struct.unpack_from("<3f", d, 52 + rec * i + oo)
                if not _finite(v):
                    raise ValueError
                nodes.append([round(v[0], 1), round(v[1], 1), round(v[2], 1), fl])
            links, end = _paths(d, 52 + rec * n, n, len(d))
        except (ValueError, struct.error):
            continue
        if len(d) - end == tail:
            return {"format": "foxbot", "nodes": nodes, "links": links, "version": version}
    raise ValueError("could not parse FoXBot layout (version %d)" % version)


def load_parabot(path):
    d = _read(path)
    if not d.startswith(b"Parabot Waypoint File"):
        raise ValueError("not a Parabot navpoint file")
    p = 32
    pass_count, n = struct.unpack_from("<ii", d, p)
    p += 8
    if not 0 < n <= MAX_NODES or p + 32 * n > len(d):
        raise ValueError("bad navpoint count")
    nodes, ids = [], {}
    for i in range(n):
        pid, typ, x, y, z = struct.unpack_from("<ii3f", d, p)
        if not _finite((x, y, z)):
            raise ValueError("bad navpoint position")
        ids[pid] = i
        nodes.append([round(x, 1), round(y, 1), round(z, 1), typ])
        p += 32
    npaths, = struct.unpack_from("<i", d, p)
    p += 4
    if not 0 <= npaths <= MAX_NODES * 16:
        raise ValueError("bad path count")
    links = []
    for _ in range(npaths):
        if p + 48 > len(d):
            raise ValueError("path truncated")
        private_id, data_id, start, end = struct.unpack_from("<4i", d, p)
        p += 48
        if data_id == private_id:                    # the path stores its own lists
            for size in (20, 16, 16):                # waypoints, hidden attacks, platforms
                c, = struct.unpack_from("<i", d, p)
                p += 4
                if not 0 <= c <= 100000 or p + size * c > len(d):
                    raise ValueError("bad path list")
                p += size * c
        if start in ids and end in ids:
            links.append([ids[start], ids[end]])
    return {"format": "parabot", "nodes": nodes, "links": links}


LOADERS = {"jkbotti": load_jkbotti, "foxbot": load_foxbot, "parabot": load_parabot}
LABELS = {"jkbotti": "jk_botti waypoints", "foxbot": "FoXBot waypoints", "parabot": "Parabot navpoints"}
EXT = {"jkbotti": ".wpt", "foxbot": ".fwp", "parabot": ".pnf"}


def find_file(directory, mapname, fmt):
    """<dir>/<map><ext>, matched case-insensitively (GoldSrc map names often differ in case)."""
    if not directory or not os.path.isdir(directory):
        return None
    want = (mapname + EXT[fmt]).lower()
    for f in os.listdir(directory):
        if f.lower() == want:
            return os.path.join(directory, f)
    return None
