"""Shared definitions: canonical classes, team colours, config and name validation."""
import json
import os
import re

CLASSES = [
    # id, key, label  (ids match the plugin's cls column)
    (1, "scout", "Scout"), (2, "soldier", "Soldier"), (3, "pyro", "Pyro"), (4, "demoman", "Demoman"),
    (5, "heavy", "Heavy"), (6, "engineer", "Engineer"), (7, "medic", "Medic"), (8, "sniper", "Sniper"),
    (9, "spy", "Spy"), (10, "civilian", "Civilian"), (11, "mercenary", "Mercenary"), (12, "zombie", "Zombie"),
    (13, "juggernaut", "Juggernaut"),
]
CLASS_KEYS = {i: k for i, k, _ in CLASSES}

COLORS = {"red": "#d8403f", "blue": "#3f7fdc", "green": "#36ad4f", "yellow": "#e3b524", "purple": "#a052e0", "grey": "#9aa0a6"}

# team names reported by the game -> colour slot
TEAM_NAME_COLORS = {
    "red": "red", "blue": "blue", "blu": "blue", "green": "green", "grn": "green", "yellow": "yellow", "ylw": "yellow",
    "mercenary": "purple", "mercenaries": "purple", "merc": "purple", "ffa": "purple", "unassigned": "purple",
    "terrorist": "red", "terrorists": "red", "counter-terrorist": "blue", "counterterrorist": "blue", "ct": "blue",
    "combine": "blue", "rebels": "red", "rebel": "red",
}

MAP_RE = re.compile(r"[a-z0-9_.+\-]{1,96}")
GAME_RE = re.compile(r"[a-z0-9_]{1,32}")
# Windows device names can't be used as folder names
RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}


def valid_map(name):
    """A plain lower-case map name that is also a safe folder name on Windows and POSIX."""
    return (isinstance(name, str) and bool(MAP_RE.fullmatch(name)) and ".." not in name
            and not name.startswith(".") and not name.endswith(".") and name.split(".")[0] not in RESERVED)


def team_color(name, override=None):
    if override:
        return COLORS.get(override, override)
    slot = TEAM_NAME_COLORS.get((name or "").strip().lower())
    return COLORS.get(slot or "grey")


def load_config(path):
    cfg = json.load(open(path, encoding="utf-8"))
    base = os.path.dirname(os.path.abspath(path))
    cfg.setdefault("site_src", os.path.join(os.path.dirname(base), "site"))
    for k in ("secrets", "site_src", "site_dir", "cache_dir"):
        cfg[k] = os.path.normpath(os.path.join(base, cfg[k]))
    for key in cfg["games"]:
        if not GAME_RE.fullmatch(key):
            raise ValueError("bad game key %r" % key)
    return cfg


def load_secrets(cfg):
    return json.load(open(cfg["secrets"], encoding="utf-8"))


def find_map_files(game_cfg, mapname, loader=None):
    """(map, nav) paths for a map from the game's content directories (first hit wins)."""
    map_ext = (loader or {}).get("map_ext", ".bsp")
    nav_ext = (loader or {}).get("nav_ext", ".nav")
    bsp = nav = None
    for d in game_cfg.get("content", []):
        dirs = [os.path.join(d, "maps"), os.path.join(d, "download", "maps")]
        custom = os.path.join(d, "custom")
        if os.path.isdir(custom):
            dirs += [os.path.join(custom, c, "maps") for c in sorted(os.listdir(custom))]
        for md in dirs:
            if bsp is None and os.path.isfile(os.path.join(md, mapname + map_ext)):
                bsp = os.path.join(md, mapname + map_ext)
            if nav is None and nav_ext and os.path.isfile(os.path.join(md, mapname + nav_ext)):
                nav = os.path.join(md, mapname + nav_ext)
        if bsp and nav:
            break
    return bsp, nav
