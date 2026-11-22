"""Quake .pak archives (read-only): "PACK", directory offset, directory length; 64-byte entries
(56-byte name, int32 offset, int32 length)."""
import os
import struct

MAX_ENTRIES = 65536


def entries(path):
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"PACK":
            return {}
        off, ln = struct.unpack_from("<ii", head, 4)
        size = os.path.getsize(path)
        if off < 12 or ln < 0 or off + ln > size or ln // 64 > MAX_ENTRIES:
            return {}
        f.seek(off)
        d = f.read(ln)
    out = {}
    for i in range(len(d) // 64):
        name = d[i * 64:i * 64 + 56].split(b"\0")[0].decode("latin-1").replace("\\", "/").lower()
        fo, fl = struct.unpack_from("<ii", d, i * 64 + 56)
        if 0 <= fo and 0 <= fl and fo + fl <= size:
            out[name] = (fo, fl)
    return out


def paks_in(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, f) for f in os.listdir(directory) if f.lower().endswith(".pak"))


def find(directory, relpath):
    """(pak path, offset, length) of relpath inside the directory's .pak files; later paks win."""
    hit = None
    for p in paks_in(directory):
        e = entries(p).get(relpath.lower())
        if e:
            hit = (p, e[0], e[1])
    return hit


def extract(hit, dst, max_size=256 * 1024 * 1024):
    pak, off, ln = hit
    if ln > max_size:
        raise ValueError("pak entry too large")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(pak, "rb") as f:
        f.seek(off)
        data = f.read(ln)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    return dst
