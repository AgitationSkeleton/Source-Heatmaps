"""Minimal reader for Valve VPK v1/v2 directory archives (read-only)."""
import os
import struct


class VPK:
    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.base = dir_path[:-len("_dir.vpk")] if dir_path.lower().endswith("_dir.vpk") else None
        self.entries = {}
        with open(dir_path, "rb") as f:
            sig, ver, tree = struct.unpack("<III", f.read(12))
            if sig != 0x55AA1234 or ver not in (1, 2):
                raise ValueError("not a VPK: %s" % dir_path)
            hdr = 12 if ver == 1 else 28
            f.seek(hdr)
            data = f.read(tree)
        self.data_start = hdr + tree
        p = 0

        def cstr():
            nonlocal p
            e = data.index(b"\0", p)
            s = data[p:e].decode("utf-8", "replace")
            p = e + 1
            return s

        while True:
            ext = cstr()
            if not ext:
                break
            while True:
                path = cstr()
                if not path:
                    break
                while True:
                    name = cstr()
                    if not name:
                        break
                    crc, pre, arc, off, ln, term = struct.unpack_from("<IHHIIH", data, p)
                    p += 18
                    preload = data[p:p + pre]
                    p += pre
                    full = ((path.strip() + "/") if path.strip() and path != " " else "") + name + ("." + ext if ext != " " else "")
                    self.entries[full.lower()] = (arc, off, ln, preload)

    def read(self, path):
        e = self.entries.get(path.lower().replace("\\", "/"))
        if e is None:
            return None
        arc, off, ln, preload = e
        if ln == 0:
            return preload
        if arc == 0x7FFF:
            fn, off = self.dir_path, self.data_start + off
        else:
            fn = "%s_%03d.vpk" % (self.base, arc)
        with open(fn, "rb") as f:
            f.seek(off)
            return preload + f.read(ln)

    def names(self):
        return self.entries.keys()


def open_all(dirs):
    """All *_dir.vpk archives under the given game directories (non-recursive + custom/*)."""
    out = []
    for d in dirs:
        cands = []
        for sub in (d, os.path.join(d, "custom")):
            if not os.path.isdir(sub):
                continue
            for n in sorted(os.listdir(sub)):
                fp = os.path.join(sub, n)
                if n.lower().endswith("_dir.vpk") and os.path.isfile(fp):
                    cands.append(fp)
                elif sub.endswith("custom") and n.lower().endswith(".vpk") and os.path.isfile(fp) and not n.lower()[-8:-4].isdigit():
                    cands.append(fp)
        for fp in cands:
            try:
                out.append(VPK(fp))
            except Exception:
                pass
    return out
