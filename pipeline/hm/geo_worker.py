"""Parse one map file in an isolated child process (memory-capped, killed on timeout by the caller).

usage: python -I geo_worker.py geo|nav <engine> <source file> <output file>
Prints one JSON line with the result summary. Map files come from game installs and can be
downloaded from anywhere, so they are parsed here rather than in the exporter process.
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

MEMORY_LIMIT = 2 * 1024 * 1024 * 1024


def limit_memory():
    if os.name != "nt":
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
        except Exception:
            pass
        return
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.GetCurrentProcess.restype = wintypes.HANDLE
    job = k.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x100 | 0x2000          # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.ProcessMemoryLimit = MEMORY_LIMIT
    if job and k.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        k.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k.GetCurrentProcess()))


def main():
    kind, engine, src, dst = sys.argv[1:5]
    limit_memory()
    from hm.geo import LOADERS
    import numpy as np
    loader = LOADERS[engine]
    tmp = dst + ".tmp"
    if kind == "geo":
        r = loader["geometry"](src)
        V, I = r["vertices"], r["indices"]
        Vq = np.clip(np.round(V), -32768, 32767).astype("<i2")
        isz = 2 if len(V) < 65536 else 4
        pos = Vq.tobytes()
        with open(tmp, "wb") as f:
            f.write(b"HMG1" + struct.pack("<III", len(V), I.size, isz))
            f.write(pos + (b"\0\0" if len(pos) % 4 else b""))
            f.write(I.astype("<u2" if isz == 2 else "<u4").tobytes())
        os.replace(tmp, dst)
        bounds = [V.min(0).round().tolist(), V.max(0).round().tolist()] if len(V) else None
        print(json.dumps({"bounds": bounds, "tris": int(len(I))}, allow_nan=False))
    else:
        n = loader["nav"](src)
        areas = n["areas"]
        out = {
            "version": n["version"], "subversion": n["subversion"],
            "areas": [[a["id"], round(a["nw"][0], 1), round(a["nw"][1], 1), round(a["nw"][2], 1),
                       round(a["se"][0], 1), round(a["se"][1], 1), round(a["se"][2], 1),
                       round(a["nez"], 1), round(a["swz"], 1), a["attr"]] for a in areas],
            "conn": [sorted(set(a["conn"])) for a in areas],
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"), allow_nan=False)
        os.replace(tmp, dst)
        print(json.dumps({"areas": len(areas)}))


if __name__ == "__main__":
    main()
