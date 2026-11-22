"""Ingest heatmap drop files written by engines that can't reach MySQL themselves (fteqw-sv).

A game's config may list "drop_dirs"; each *.hm file there is plain text:

    HM1 game=<gamedir> map=<map> port=<port>
    T <team index> <team name>                      (optional)
    <kind> <team> <class> <bot> <x> <y> <z> <x2> <y2> <z2> <count>

with the same meaning as the SourceMod plugin's staging rows (cells are floor(world/32), edges
floor(world/64)). The file's game must be the config key (or its "drop_game"); a game's
"class_map" turns raw classes (e.g. TF playerclass) into canonical ids. Rows go into hm_ingest
like any server's, so compaction applies the usual validation; each file is deleted afterwards.
Files are untrusted: sizes, counts and every value are checked.
"""
import os
import re

from .common import valid_map

MAX_FILE = 8 * 1024 * 1024
MAX_ROWS = 200000
MAX_FILES_PER_RUN = 2000
HEAD_RE = re.compile(r"HM1 game=([a-z0-9_]{1,32}) map=(\S{1,96}) port=(\d{1,5})")
TEAM_RE = re.compile(r"T (\d{1,2}) ([A-Za-z0-9 _#()./+-]{1,64})")
INSERT = "INSERT INTO hm_ingest (port,game,map,kind,team,cls,bot,x,y,z,x2,y2,z2,n) VALUES "


def _parse(path, game_key, expect_game, class_map, log):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("file too large")
    with open(path, encoding="latin-1") as f:
        lines = f.read().splitlines()
    if not lines:
        raise ValueError("empty file")
    m = HEAD_RE.fullmatch(lines[0].strip())
    if not m:
        raise ValueError("bad header")
    game, mapname, port = m.group(1), m.group(2).lower(), int(m.group(3))
    if game != expect_game or not valid_map(mapname) or port > 65535:
        raise ValueError("header doesn't match this drop folder (game %s, map %r)" % (game, mapname))
    rows, teams = [], []
    for ln in lines[1:]:
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("T "):
            t = TEAM_RE.fullmatch(ln)
            if t:
                teams.append((int(t.group(1)), t.group(2)))
            continue
        parts = ln.split()
        if len(parts) != 11 or not all(re.fullmatch(r"-?\d{1,7}", p) for p in parts):
            raise ValueError("bad row")
        kind, team, cls, bot, x, y, z, x2, y2, z2, n = (int(p) for p in parts)
        if kind not in (0, 1, 2, 3) or not 0 <= team < 16 or bot not in (0, 1) or not 1 <= n <= 1000000:
            raise ValueError("value out of range")
        cls = int(class_map.get(str(cls), cls)) if class_map else cls
        if not 0 <= cls <= 13:
            cls = 0
        rows.append((port, game_key, mapname, kind, team, cls, bot, x, y, z, x2, y2, z2, n))
        if len(rows) > MAX_ROWS:
            raise ValueError("too many rows")
    return mapname, port, rows, teams


def collect(cfg, conn, log=print):
    total = files = 0
    for key, g in cfg["games"].items():
        for d in g.get("drop_dirs", []):
            if not os.path.isdir(d):
                continue
            names = sorted(f for f in os.listdir(d) if f.lower().endswith(".hm"))[:MAX_FILES_PER_RUN]
            for name in names:
                path = os.path.join(d, name)
                try:
                    mapname, port, rows, teams = _parse(path, key, g.get("drop_game", key), g.get("class_map"), log)
                    with conn.cursor() as c:
                        for i in range(0, len(rows), 500):
                            part = rows[i:i + 500]
                            c.execute(INSERT + ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(part)),
                                      [v for r in part for v in r])
                        for t, tname in teams:
                            c.execute("INSERT INTO hm_ingest_meta (port,game,map,kind,k,v) VALUES (%s,%s,%s,1,%s,%s)",
                                      (port, key, mapname, t, tname))
                    conn.commit()
                    total += len(rows)
                    files += 1
                except Exception as e:
                    conn.rollback()
                    log("collect: rejected %s (%s)" % (name, str(e)[:120]))
                try:
                    os.remove(path)
                except OSError as e:
                    log("collect: couldn't remove %s: %s" % (name, e))
    if files:
        log("collect: %d drop file(s), %d row(s) staged" % (files, total))
    return total
