# Extending to other engines

GoldSrc is supported: see `plugin/amxmodx/` for the collector and `pipeline/hm/geo/bsp_goldsrc.py`
for the map loader (BSP v30; Quake v29 too). The sections below show how that was done, for
adding another engine such as FTEQW / QuakeWorld.

The storage and the website don't depend on the engine. To add an engine you need two things:

1. **Something that writes staging rows.**
2. **A map loader** for the exporter.

## 1. Writing staging rows

Any collector may INSERT into `heatmap.hm_ingest` with the `hm_ingest` account, using the same conventions as the SourceMod plugin:

| column | value |
|---|---|
| `port` | server port (informational) |
| `game` | game key, `[a-z0-9_]{1,32}`, and present in the exporter's `config.json` `games` (other keys are discarded) |
| `map` | lower-case map name, `[a-z0-9_.+-]{1,96}` |
| `kind` | 0 grounded presence, 1 death, 2 movement edge, 3 airborne presence |
| `team` | team index 0-15 |
| `cls` | canonical class 0-13 (see DATA_FORMAT.md) |
| `bot` | 0 or 1 |
| `x,y,z` | `floor(world/32)` for kinds 0, 1, 3; `floor(world/64)` for edges |
| `x2,y2,z2` | edge end cell (kind 2), else 0 |
| `n` | count in this flush, 1 to 1,000,000 |

Aggregate in memory and flush multi-row INSERTs about once a minute. Don't write one row per sample.

Team names go to `hm_ingest_meta` with `kind=1`, `k` = team index and `v` = name, once per map.

- **GoldSrc (AMX Mod X):**
  - Use `sqlx` with `SQL_ThreadQuery`.
  - Positions come from `pev(id, pev_origin, …)` and teams from `get_user_team`.
  - For TFC classes, read `pev_playerclass`; its order is the same as Source TF, with civilian at 11 in TFC. Map it with a table.
- **FTEQW / QuakeC:**
  - QuakeC has no SQL. Write a small log line per flush (`localcmd`/`fputs`, or `bprint` to a log channel).
  - Have a tiny sidecar insert it with `hm_ingest`.
  - Keep the sidecar on the server machine, and give it only that account.

### Engines without database access: drop files

An engine can write plain-text drop files instead of talking to MySQL. List the folder in the game's
`drop_dirs`; on each run the exporter reads every `*.hm` file there, validates it, stages the rows
and deletes the file. This is how QuakeWorld (fteqw-sv) feeds the site.

```
HM1 game=<game key> map=<map> port=<port>
T <team index> <team name>                       (optional, one per team)
<kind> <team> <class> <bot> <x> <y> <z> <x2> <y2> <z2> <count>
```

- Columns mean the same as the staging table above (cells `floor(world/32)`, edges `floor(world/64)`).
- `game` must equal the config key, or the game's `drop_game`.
- A game's `class_map` turns raw class numbers into canonical ids. For example, QuakeWorld TF `playerclass` maps 6 (HWGuy) to heavy and 11 to civilian.
- Write each file atomically: write `.tmp`, then rename.

## 2. A map loader

Add a module under `pipeline/hm/geo/` and register it in `pipeline/hm/geo/__init__.py`:

```python
LOADERS["goldsrc"] = {"map_ext": ".bsp", "nav_ext": None, "geometry": bsp_goldsrc.load, "nav": None}
```

`geometry(path)` returns `{"vertices": float32 (N,3), "indices": uint32 (M,3)}`:
- in world units, Z up;
- with triangles counter-clockwise around their outward normal;
- with sky and clip faces dropped.

For GoldSrc BSP v30 and Quake BSP29 (or BSP2/2PSB in FTE), the face, edge and surfedge lumps work the same way as in Source. Model 0 is the world, and the texture name in the miptex lump tells you `sky`, `clip`, `aaatrigger` and so on.

Then add the game to `config.json` with `"engine": "goldsrc"`. Map lookup, export and the site need no changes. Without a nav format, the site simply hides the nav and analysis layers for those maps.
