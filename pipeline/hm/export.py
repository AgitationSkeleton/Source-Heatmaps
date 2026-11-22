"""Export aggregates to the static site: data/manifest.json and data/<game>/<map>/*.

Per map:
  meta.json     counts, teams, classes, bounds, which layers exist
  cells.bin     presence/death cells (see docs/DATA_FORMAT.md)
  edges.bin     movement edges (strongest first, capped)
  geo.bin       low-poly world brushwork from the BSP (cached by BSP size+mtime)
  nav.json      nav areas and connections, when the map has a .nav
  analysis.json nav gaps (human ground presence with no nav area under it) and missing links
                (human moves between nav areas that the mesh does not connect); for bot/nav work
"""
import json
import math
import os
import struct
import subprocess
import sys
import time

import numpy as np

from .common import CLASSES, CLASS_KEYS, find_map_files, team_color, valid_map
from .geo import LOADERS

CELL, EDGE_CELL = 32, 64
MAX_CELLS = 400_000          # per map, strongest first
GEO_VERSION = 3
NAV_VERSION = 1


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))
    os.replace(tmp, path)


def _json(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------- geometry / nav (cached)

def _sig(path):
    st = os.stat(path)
    return "%d-%d" % (st.st_size, int(st.st_mtime))


WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_worker.py")
WORKER_TIMEOUT = 180


def _run_worker(kind, engine, src, dst, log, label):
    """Parse a map file in a memory-capped child process; None on failure or timeout."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        p = subprocess.run([sys.executable, "-I", WORKER, kind, engine, src, dst],
                           capture_output=True, text=True, timeout=WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("%s %s: timed out after %d s, skipped" % (kind, label, WORKER_TIMEOUT))
        return None
    if p.returncode != 0:
        err = (p.stderr.strip().splitlines() or ["exit %d" % p.returncode])[-1]
        log("%s %s: %s" % (kind, label, err[:200]))
        return None
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _cached(cache_f, sig, dst):
    try:
        c = json.load(open(cache_f))
    except (OSError, ValueError):
        return None
    if c.get("sig") != sig:
        return None
    if c.get("failed"):
        return c                       # known-bad file: don't retry until it changes
    return c if os.path.isfile(dst) else None


def _save_cache(cache_f, data):
    os.makedirs(os.path.dirname(cache_f), exist_ok=True)
    json.dump(data, open(cache_f, "w"))


def build_geo(cfg, game, mapname, bsp, out_dir, log, engine):
    cache_f = os.path.join(cfg["cache_dir"], game, mapname + ".geo.json")
    dst = os.path.join(out_dir, "geo.bin")
    sig = "%s|%d" % (_sig(bsp), GEO_VERSION)
    c = _cached(cache_f, sig, dst)
    if c is not None:
        return None if c.get("failed") else c["bounds"]
    t = time.time()
    r = _run_worker("geo", engine, bsp, dst, log, "%s/%s" % (game, mapname))
    if r is None:
        _save_cache(cache_f, {"sig": sig, "failed": True})
        return None
    _save_cache(cache_f, {"sig": sig, "bounds": r["bounds"], "tris": r["tris"]})
    log("geo %s/%s: %d tris (%.1fs)" % (game, mapname, r["tris"], time.time() - t))
    return r["bounds"]


def build_nav(cfg, game, mapname, nav, out_dir, log, engine):
    cache_f = os.path.join(cfg["cache_dir"], game, mapname + ".nav.json")
    dst = os.path.join(out_dir, "nav.json")
    sig = "%s|%d" % (_sig(nav), NAV_VERSION)
    c = _cached(cache_f, sig, dst)
    if c is None:
        r = _run_worker("nav", engine, nav, dst, log, "%s/%s" % (game, mapname))
        if r is None:
            _save_cache(cache_f, {"sig": sig, "failed": True})
            return None
        _save_cache(cache_f, {"sig": sig, "areas": r["areas"]})
        log("nav %s/%s: %d areas" % (game, mapname, r["areas"]))
    elif c.get("failed"):
        return None
    try:
        return json.load(open(dst))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- analysis

class _NavIndex:
    """Grid index of nav areas for point lookups."""

    MAX_AREA_SPAN = 8192.0
    MAX_ENTRIES = 4_000_000

    def __init__(self, nav, g=128.0):
        self.g = g
        self.areas = nav["areas"]
        self.conn = {a[0]: set(c) for a, c in zip(nav["areas"], nav["conn"])}
        self.grid = {}
        entries = 0
        for i, a in enumerate(self.areas):
            x0, x1 = min(a[1], a[4]), max(a[1], a[4])
            y0, y1 = min(a[2], a[5]), max(a[2], a[5])
            if x1 - x0 > self.MAX_AREA_SPAN or y1 - y0 > self.MAX_AREA_SPAN:
                continue          # implausibly large area: skip rather than flood the index
            entries += (int((x1 - x0) // g) + 2) * (int((y1 - y0) // g) + 2)
            if entries > self.MAX_ENTRIES:
                break
            for gx in range(int(math.floor(x0 / g)), int(math.floor(x1 / g)) + 1):
                for gy in range(int(math.floor(y0 / g)), int(math.floor(y1 / g)) + 1):
                    self.grid.setdefault((gx, gy), []).append(i)

    @staticmethod
    def _z(a, x, y):
        x0, y0, z_nw, x1, y1, z_se, z_ne, z_sw = a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8]
        u = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
        v = 0.0 if y1 == y0 else (y - y0) / (y1 - y0)
        top = z_nw + (z_ne - z_nw) * u
        bot = z_sw + (z_se - z_sw) * u
        return top + (bot - top) * v

    def find(self, x, y, z, below=24.0, above=72.0, pad=0.0):
        best, bd = None, None
        for i in self.grid.get((int(math.floor(x / self.g)), int(math.floor(y / self.g))), ()):
            a = self.areas[i]
            if not (min(a[1], a[4]) - pad <= x <= max(a[1], a[4]) + pad and min(a[2], a[5]) - pad <= y <= max(a[2], a[5]) + pad):
                continue
            az = self._z(a, x, y)
            dz = z - az
            if -below <= dz <= above:
                if bd is None or abs(dz) < bd:
                    best, bd = a[0], abs(dz)
        return best


def analyse(nav, cells, edges):
    idx = _NavIndex(nav)
    # nav gaps: human grounded presence (all teams/classes merged) with no area under it
    m = (cells["kind"] == 0) & (cells["bot"] == 0)
    agg = {}
    for x, y, z, n in zip(*(cells[k][m].tolist() for k in ("x", "y", "z", "n"))):
        k = (int(x), int(y), int(z))
        agg[k] = agg.get(k, 0) + int(n)
    gaps = []
    for (x, y, z), n in agg.items():
        wx, wy, wz = x * CELL + CELL / 2, y * CELL + CELL / 2, z * CELL + 1
        if idx.find(wx, wy, wz, below=40, above=40, pad=8) is None:
            gaps.append([int(wx), int(wy), int(z * CELL), n])
    gaps.sort(key=lambda g: -g[3])

    # missing links: human moves between two different areas that are not connected (1 or 2 hops)
    links = {}
    em = edges["bot"] == 0
    for x, y, z, x2, y2, z2, n in zip(*(edges[k][em].tolist() for k in ("x", "y", "z", "x2", "y2", "z2", "n"))):
        a = idx.find(x * EDGE_CELL + 32, y * EDGE_CELL + 32, z * EDGE_CELL + 32, below=72, above=72)
        b = idx.find(x2 * EDGE_CELL + 32, y2 * EDGE_CELL + 32, z2 * EDGE_CELL + 32, below=72, above=72)
        if a is None or b is None or a == b:
            continue
        ca = idx.conn.get(a, ())
        if b in ca or any(b in idx.conn.get(m2, ()) for m2 in ca):
            continue
        k = (a, b)
        if k not in links:
            links[k] = [0, (x * EDGE_CELL + 32, y * EDGE_CELL + 32, z * EDGE_CELL + 32),
                        (x2 * EDGE_CELL + 32, y2 * EDGE_CELL + 32, z2 * EDGE_CELL + 32)]
        links[k][0] += int(n)
    ml = sorted(([a, b, v[0], list(v[1]), list(v[2])] for (a, b), v in links.items()), key=lambda r: -r[2])
    return {"gaps": gaps[:5000], "missingLinks": ml[:2000],
            "notes": {"gaps": "[x, y, z, humanSamples] world units, 32-unit cells with human ground presence and no nav area within 40u vertically",
                      "missingLinks": "[fromAreaId, toAreaId, humanMoves, fromPos, toPos]: humans moved A->B within one sample (~1 s) but B is not within 2 connections of A"}}


# ---------------------------------------------------------------- export

def _fetch_cells(c, game, mapname):
    c.execute("SELECT kind,team,cls,bot,x,y,z,n FROM hm_cell WHERE game=%s AND map=%s ORDER BY n DESC LIMIT %s",
              (game, mapname, MAX_CELLS))
    rows = c.fetchall()
    a = np.array(rows, dtype=np.int64).reshape(-1, 8)
    return {"kind": a[:, 0], "team": a[:, 1], "cls": a[:, 2], "bot": a[:, 3], "x": a[:, 4], "y": a[:, 5], "z": a[:, 6], "n": a[:, 7]}


def _fetch_edges(c, game, mapname, limit):
    c.execute("SELECT team,cls,bot,x,y,z,x2,y2,z2,n FROM hm_edge WHERE game=%s AND map=%s ORDER BY n DESC LIMIT %s",
              (game, mapname, int(limit)))
    a = np.array(c.fetchall(), dtype=np.int64).reshape(-1, 10)
    return {"team": a[:, 0], "cls": a[:, 1], "bot": a[:, 2], "x": a[:, 3], "y": a[:, 4], "z": a[:, 5],
            "x2": a[:, 6], "y2": a[:, 7], "z2": a[:, 8], "n": a[:, 9]}


def _cells_bin(d):
    n = len(d["n"])
    parts = [b"HMC1", struct.pack("<I", n),
             np.minimum(d["n"], 0xFFFFFFFF).astype("<u4").tobytes(),
             d["x"].astype("<i2").tobytes(), d["y"].astype("<i2").tobytes(), d["z"].astype("<i2").tobytes(),
             d["kind"].astype("u1").tobytes(), d["team"].astype("u1").tobytes(), d["cls"].astype("u1").tobytes(),
             d["bot"].astype("u1").tobytes()]
    return b"".join(parts)


def _edges_bin(d):
    n = len(d["n"])
    parts = [b"HME1", struct.pack("<I", n), np.minimum(d["n"], 0xFFFFFFFF).astype("<u4").tobytes()]
    for k in ("x", "y", "z", "x2", "y2", "z2"):
        parts.append(d[k].astype("<i2").tobytes())
    for k in ("team", "cls", "bot"):
        parts.append(d[k].astype("u1").tobytes())
    return b"".join(parts)


def export_map(cfg, conn, game, mapname, teams, log):
    gcfg = cfg["games"][game]
    out_dir = os.path.join(cfg["site_dir"], "data", game, mapname)
    with conn.cursor() as c:
        cells = _fetch_cells(c, game, mapname)
        edges = _fetch_edges(c, game, mapname, cfg.get("edge_limit", 60000))
    _atomic_write(os.path.join(out_dir, "cells.bin"), _cells_bin(cells))
    _atomic_write(os.path.join(out_dir, "edges.bin"), _edges_bin(edges))

    loader = LOADERS.get(gcfg.get("engine", "source"))
    bsp, navf = find_map_files(gcfg, mapname, loader) if loader else (None, None)
    if not bsp:
        return None                    # no map file on disk: not a real map of this game
    bounds = None
    if bsp:
        try:
            bounds = build_geo(cfg, game, mapname, bsp, out_dir, log, gcfg.get("engine", "source"))
        except Exception as e:
            log("geo %s/%s failed: %s" % (game, mapname, e))
    nav = build_nav(cfg, game, mapname, navf, out_dir, log, gcfg.get("engine", "source")) if navf and loader.get("nav") else None
    has_analysis = False
    if nav:
        try:
            _atomic_write(os.path.join(out_dir, "analysis.json"), _json(analyse(nav, cells, edges)))
            has_analysis = True
        except Exception as e:
            log("analysis %s/%s failed: %s" % (game, mapname, e))

    present_cls = sorted(set(int(v) for v in np.unique(cells["cls"])))
    present_team = sorted(set(int(v) for v in np.unique(cells["team"])) | set(int(v) for v in np.unique(edges["team"])))
    meta = {
        "game": game, "map": mapname, "cell": CELL, "edgeCell": EDGE_CELL,
        "updated": int(time.time()),
        "cells": int(len(cells["n"])), "edges": int(len(edges["n"])),
        "samples": {"human": int(cells["n"][(cells["kind"] != 1) & (cells["bot"] == 0)].sum()),
                    "bot": int(cells["n"][(cells["kind"] != 1) & (cells["bot"] == 1)].sum()),
                    "deaths": int(cells["n"][cells["kind"] == 1].sum())},
        "teams": {str(t): teams.get(t, {"name": "Team %d" % t,
                                        "color": team_color("", gcfg.get("team_colors", {}).get(str(t)))}) for t in present_team},
        "classes": [CLASS_KEYS[c] for c in present_cls if c in CLASS_KEYS],
        "bounds": bounds,
        "has": {"geo": bool(bounds), "nav": bool(nav), "analysis": has_analysis},
    }
    _atomic_write(os.path.join(out_dir, "meta.json"), _json(meta))
    return meta


def export(cfg, conn, full=False, log=print):
    games_out = {}
    with conn.cursor() as c:
        c.execute("SELECT game,team,name FROM hm_team")
        team_rows = c.fetchall()
        c.execute("SELECT game,map,samples,UNIX_TIMESTAMP(last_seen),dirty FROM hm_map ORDER BY game,map")
        maps = c.fetchall()
    teams_by_game = {}
    for g, t, name in team_rows:
        ov = cfg["games"].get(g, {}).get("team_colors", {}).get(str(t))
        # some mods report localisation tokens (#FF_TEAM_BLUE): show the last word
        nice = name.lstrip("#").split("_")[-1].title() if name.startswith("#") else name
        teams_by_game.setdefault(g, {})[int(t)] = {"name": nice, "color": team_color(nice, ov)}

    done = 0
    for game, mapname, samples, last_seen, dirty in maps:
        if game not in cfg["games"] or not valid_map(mapname):
            continue
        meta_f = os.path.join(cfg["site_dir"], "data", game, mapname, "meta.json")
        if dirty or full or not os.path.isfile(meta_f):
            try:
                meta = export_map(cfg, conn, game, mapname, teams_by_game.get(game, {}), log)
                if meta is None:
                    with conn.cursor() as c:
                        c.execute("UPDATE hm_map SET dirty=0 WHERE game=%s AND map=%s", (game, mapname))
                    conn.commit()
                    continue
                with conn.cursor() as c:
                    c.execute("UPDATE hm_map SET dirty=0 WHERE game=%s AND map=%s", (game, mapname))
                conn.commit()
                done += 1
            except Exception as e:
                log("export %s/%s failed: %s" % (game, mapname, e))
                continue
        else:
            try:
                meta = json.load(open(meta_f))
            except (OSError, ValueError):
                continue
        g = games_out.setdefault(game, {"name": cfg["games"][game]["name"], "engine": cfg["games"][game].get("engine", "source"),
                                       "icons": cfg["games"][game].get("icons", {}), "maps": []})
        g["maps"].append({"map": mapname, "samples": int(samples), "lastSeen": int(last_seen or 0),
                          "geo": meta["has"]["geo"], "nav": meta["has"]["nav"]})

    manifest = {
        "generated": int(time.time()),
        "classes": [{"id": i, "key": k, "label": lab} for i, k, lab in CLASSES],
        "games": games_out,
    }
    _atomic_write(os.path.join(cfg["site_dir"], "data", "manifest.json"), _json(manifest))
    log("export: %d map(s) rebuilt, %d game(s) in manifest" % (done, len(games_out)))
    return done
