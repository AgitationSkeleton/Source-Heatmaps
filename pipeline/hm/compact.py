"""Fold validated staging rows (hm_ingest / hm_ingest_meta) into the aggregate tables.

Game servers can only INSERT staging rows, so everything in staging is untrusted. Rows are
discarded when they fail validation: unknown game, a map name that isn't a plain lower-case
name, a map that has no map file in the game's content folders, or out-of-range values.
Staging is processed in id-range chunks so a flood can't make one huge transaction.
"""
import os

import pymysql

from .common import find_map_files, load_secrets, valid_map
from .geo import LOADERS

CHUNK = 200_000
MAX_ROWS_PER_RUN = 3_000_000     # a flood is folded over several runs instead of one huge one

# cells are 32 units, edges 64; Source maps are within +-16384 (+-32768 for extended-bounds mods).
# BINARY makes the name check case-sensitive; LOCATE/LIKE reject a trailing newline or dot.
VALID = ("game IN ({games}) AND BINARY map REGEXP '^[a-z0-9_.+-]{{1,96}}$' AND LOCATE(CHAR(10), map) = 0 "
         "AND map NOT LIKE '%%..%%' AND map NOT LIKE '%%.' AND map NOT LIKE '.%%' "
         "AND kind IN (0,1,2,3) AND team < 16 AND cls <= 13 AND bot <= 1 AND n BETWEEN 1 AND 1000000 "
         "AND x BETWEEN -1100 AND 1100 AND y BETWEEN -1100 AND 1100 AND z BETWEEN -1100 AND 1100 "
         "AND x2 BETWEEN -1100 AND 1100 AND y2 BETWEEN -1100 AND 1100 AND z2 BETWEEN -1100 AND 1100")


def connect(cfg):
    s = load_secrets(cfg)
    return pymysql.connect(host=s["host"], port=int(s.get("port", 3306)), user=s["export"]["user"],
                           password=s["export"]["pass"], database=s.get("database", "heatmap"),
                           charset="utf8mb4", autocommit=False)


def readable_games(cfg, log):
    """Games whose map folders this account can list. Rows of other configured games stay in
    staging (a permissions problem must never look like 'every map is fake')."""
    ok = []
    for key, g in cfg["games"].items():
        dirs = [os.path.join(d, "maps") for d in g.get("content", [])]
        if any(os.path.isdir(d) and _can_list(d) for d in dirs):
            ok.append(key)
        else:
            log("compact: can't read any maps folder for %s; leaving its rows in staging" % key)
    return ok


def _can_list(d):
    try:
        os.listdir(d)
        return True
    except OSError:
        return False


class _MapCheck:
    """Caches whether a (game, map) has a map file on disk."""

    def __init__(self, cfg):
        self.cfg, self.cache = cfg, {}

    def ok(self, game, mapname):
        k = (game, mapname)
        if k not in self.cache:
            g = self.cfg["games"].get(game)
            loader = LOADERS.get((g or {}).get("engine", "source"))
            self.cache[k] = bool(g and loader and valid_map(mapname) and find_map_files(g, mapname, loader)[0])
        return self.cache[k]


def _fold_chunk(c, conn, lo, hi, games, valid, check, log, held):
    """Fold staging ids in (lo, hi]. Returns (good, discarded). Rows of games in `held` are kept."""
    rng = [lo, hi]
    hold = (" AND game NOT IN (%s)" % ",".join(["%s"] * len(held))) if held else ""
    # drop rows for maps that don't exist in the game's content (fake or mistyped map names)
    c.execute("SELECT DISTINCT game, map FROM hm_ingest WHERE id > %s AND id <= %s", rng)
    bad_maps = [(g, m) for g, m in c.fetchall() if g not in held and not check.ok(g, m)]
    if bad_maps:
        log("compact: %d unknown map(s), e.g. %s" % (len(bad_maps), ", ".join("%s/%s" % gm for gm in bad_maps[:5])))
    dropped = 0
    by_game = {}
    for g, m in bad_maps:
        by_game.setdefault(g, []).append(m)
    for g, maps in by_game.items():          # only the unknown maps go, in batches; real rows stay
        for i in range(0, len(maps), 500):
            part = maps[i:i + 500]
            dropped += c.execute("DELETE FROM hm_ingest WHERE id > %s AND id <= %s AND game = %s AND map IN (" +
                                 ",".join(["%s"] * len(part)) + ")", rng + [g] + part)
    c.execute("SELECT COUNT(*) FROM hm_ingest WHERE id > %s AND id <= %s AND " + valid, rng + games)
    good, = c.fetchone()
    c.execute("SELECT COUNT(*) FROM hm_ingest WHERE id > %s AND id <= %s" + hold, rng + held)
    total, = c.fetchone()
    if total and not good:
        log("compact: chunk (%s, %s] had %d rows and none valid" % (lo, hi, total))
    c.execute(
        "INSERT INTO hm_cell (game,map,kind,team,cls,bot,x,y,z,n) "
        "SELECT game,map,kind,team,cls,bot,x,y,z,SUM(n) FROM hm_ingest "
        "WHERE id > %s AND id <= %s AND kind <> 2 AND " + valid + " GROUP BY game,map,kind,team,cls,bot,x,y,z "
        "ON DUPLICATE KEY UPDATE n = hm_cell.n + VALUES(n)", rng + games)
    c.execute(
        "INSERT INTO hm_edge (game,map,team,cls,bot,x,y,z,x2,y2,z2,n) "
        "SELECT game,map,team,cls,bot,x,y,z,x2,y2,z2,SUM(n) FROM hm_ingest "
        "WHERE id > %s AND id <= %s AND kind = 2 AND " + valid + " GROUP BY game,map,team,cls,bot,x,y,z,x2,y2,z2 "
        "ON DUPLICATE KEY UPDATE n = hm_edge.n + VALUES(n)", rng + games)
    # timestamps are clamped to now: staging rows could carry any value
    c.execute(
        "INSERT INTO hm_map (game,map,samples,dirty,first_seen,last_seen) "
        "SELECT game,map,SUM(CASE WHEN kind IN (0,3) THEN n ELSE 0 END),1,LEAST(MIN(received),NOW()),LEAST(MAX(received),NOW()) "
        "FROM hm_ingest WHERE id > %s AND id <= %s AND " + valid + " GROUP BY game,map "
        "ON DUPLICATE KEY UPDATE samples = hm_map.samples + VALUES(samples), dirty = 1, "
        "last_seen = LEAST(GREATEST(hm_map.last_seen, VALUES(last_seen)), NOW())", rng + games)
    c.execute("DELETE FROM hm_ingest WHERE id > %s AND id <= %s" + hold, rng + held)
    conn.commit()
    return good, dropped + (total - good)


def compact(cfg, conn, log=print):
    games = readable_games(cfg, log)
    held = [g for g in cfg["games"] if g not in games]
    if not games:
        return 0
    gl = ",".join(["%s"] * len(games))
    valid = VALID.format(games=gl)
    check = _MapCheck(cfg)
    good_all = bad_all = 0
    with conn.cursor() as c:
        c.execute("SELECT COALESCE(MIN(id),1)-1, COALESCE(MAX(id),0) FROM hm_ingest")
        lo, maxid = c.fetchone()
        done = 0
        while lo < maxid and done < MAX_ROWS_PER_RUN:
            c.execute("SELECT id FROM hm_ingest WHERE id > %s ORDER BY id LIMIT 1 OFFSET %s", (lo, CHUNK - 1))
            r = c.fetchone()
            hi = min(maxid, r[0]) if r else maxid
            good, bad = _fold_chunk(c, conn, lo, hi, games, valid, check, log, held)
            good_all += good
            bad_all += bad
            done += good + bad
            lo = hi
        if lo < maxid:
            log("compact: run limit reached; the rest of staging is folded next run")
        if bad_all:
            log("compact: discarded %d invalid staging row(s)" % bad_all)
        if good_all:
            log("compact: folded %d staging row(s)" % good_all)

        c.execute("SELECT COALESCE(MAX(id),0) FROM hm_ingest_meta")
        maxmeta, = c.fetchone()
        if maxmeta:
            c.execute(
                "INSERT INTO hm_team (game,team,name) "
                "SELECT game,k,SUBSTRING(MAX(v),1,64) FROM hm_ingest_meta "
                "WHERE id <= %s AND kind = 1 AND k BETWEEN 0 AND 15 AND game IN (" + gl + ") "
                "AND BINARY v REGEXP '^[A-Za-z0-9 _#()./+-]{1,64}$' AND LOCATE(CHAR(10), v) = 0 GROUP BY game,k "
                "ON DUPLICATE KEY UPDATE name = VALUES(name)", [maxmeta] + games)
            c.execute("DELETE FROM hm_ingest_meta WHERE id <= %s", [maxmeta])
            conn.commit()
    return good_all
