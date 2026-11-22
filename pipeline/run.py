"""Source Heatmaps pipeline.

usage: python -I run.py [all|compact|export|icons] [--full] [--config config.json]

  compact  fold validated staging rows into the aggregates
  export   write changed maps (or every map with --full) to the static site
  icons    build class icons from the game files listed in config.json
  all      compact + export (default; what the scheduled task runs)

Runs at below-normal priority, one instance at a time, and logs to ../logs/pipeline.log.
"""
import argparse
import datetime
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hm import common  # noqa: E402


def lower_priority():
    if os.name == "nt":
        import ctypes
        BELOW_NORMAL = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    else:
        try:
            os.nice(10)
        except OSError:
            pass


STATIC = ("index.html", "app.js", "style.css", "vendor/three.module.min.js", "vendor/OrbitControls.js", "vendor/three.LICENSE")


def sync_static(cfg, log):
    """Copy the site's static files into site_dir when site_src is a different folder."""
    src, dst = cfg["site_src"], cfg["site_dir"]
    if os.path.normcase(src) == os.path.normcase(dst):
        return
    n = 0
    for rel in STATIC:
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        if not os.path.isfile(s):
            continue
        if os.path.isfile(d) and open(s, "rb").read() == open(d, "rb").read():
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(s, d + ".tmp")
        os.replace(d + ".tmp", d)
        n += 1
    # bundled class icons: copied only where site_dir has none (icons built by `run.py icons` win)
    isrc = os.path.join(src, "icons")
    for root, _, files in os.walk(isrc):
        for f in files:
            if not f.endswith(".png"):
                continue
            s = os.path.join(root, f)
            d = os.path.join(dst, "icons", os.path.relpath(s, isrc))
            if not os.path.exists(d):
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copyfile(s, d)
                n += 1
    if n:
        log("site: %d static file(s) updated" % n)


class Lock:
    def __init__(self, path, stale=2 * 3600):
        self.path, self.stale = path, stale

    @staticmethod
    def _alive(pid):
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259          # STILL_ACTIVE
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def __enter__(self):
        # a lock left by a killed run (task time limit, crash) is cleared when its process is gone
        try:
            pid = int(open(self.path).read().strip() or 0)
            if not pid or not self._alive(pid) or time.time() - os.path.getmtime(self.path) > self.stale:
                os.remove(self.path)
        except (OSError, ValueError):
            pass
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit("another pipeline run is in progress (%s)" % self.path)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="all", choices=["all", "compact", "export", "icons"])
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    a = ap.parse_args()
    lower_priority()
    cfg = common.load_config(a.config)
    logdir = cfg.get("log_dir") or os.path.join(os.path.dirname(cfg["site_dir"]), "logs")
    os.makedirs(logdir, exist_ok=True)
    os.makedirs(cfg["cache_dir"], exist_ok=True)
    logf = open(os.path.join(logdir, "pipeline.log"), "a", encoding="utf-8")

    def log(msg):
        line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line)
        logf.write(line + "\n")
        logf.flush()

    with Lock(os.path.join(cfg["cache_dir"], "pipeline.lock")):
        sync_static(cfg, log)
        if a.cmd == "icons":
            from hm import icons
            icons.build(cfg, log)
            return
        from hm import compact, export
        conn = compact.connect(cfg)
        try:
            if a.cmd in ("all", "compact"):
                compact.compact(cfg, conn, log)
            if a.cmd in ("all", "export"):
                export.export(cfg, conn, full=a.full, log=log)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
