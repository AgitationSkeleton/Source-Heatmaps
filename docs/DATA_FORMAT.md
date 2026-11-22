# Data formats

All files are static and public under `data/` on the site. Coordinates are Source world units, Z up.

## Units and indices

| Field | Meaning |
|---|---|
| cell (`x`,`y`,`z` in cells.bin) | `floor(world / 32)`. A 32-unit cell's world centre is `x*32+16, y*32+16`; `z*32` is the floor of the cell the player's feet were in. |
| edge cell (edges.bin) | `floor(world / 64)`, centre `x*64+32, …` |
| `n` presence | samples. The plugin samples once per `sm_heatmap_interval` (default 1 s), so `n` ≈ seconds spent in the cell. |
| `n` deaths | deaths in the cell |
| `n` edges | moves from cell A to cell B between two consecutive samples of the same life |
| `team` | the game's team index (TF: 2 red, 3 blue; TF2C adds 4 green, 5 yellow; OF 4 = Mercenary FFA; FF 2 blue, 3 red, 4 yellow, 5 green; HL2DM FFA = 0). Names and colours are in `meta.json`. |
| `bot` | 0 human, 1 bot |
| `kind` | 0 grounded presence, 1 death, 3 airborne presence (edges are their own file) |

### Canonical classes (`cls`)

| id | class | id | class |
|---|---|---|---|
| 0 | none (no class system) | 7 | medic |
| 1 | scout | 8 | sniper |
| 2 | soldier | 9 | spy |
| 3 | pyro | 10 | civilian |
| 4 | demoman | 11 | mercenary |
| 5 | heavy (FF hwguy) | 12 | zombie |
| 6 | engineer | 13 | juggernaut |

Special labelling done by the plugin:
- **TF2 with tf2cc** (class 10 slot): the tf2cc role decides Civilian or Mercenary.
- **TF2C with tf2cc**: the tf2cc Merc model marks Mercenary; real Soldiers stay Soldier.
- **TF2C DM/infection VScript bundle**: the Merc model or class Soldier marks Mercenary; the zombie model or class Sniper marks Zombie.
- **Open Fortress**: raw 10 is Mercenary, 11 Civilian, 12 Juggernaut.
- **Team Fortress Classic** (`pev_playerclass`): 1 scout, 2 sniper, 3 soldier, 4 demoman, 5 medic, 6 HWGuy (heavy), 7 pyro, 8 spy, 9 engineer, 11 civilian.

Team indices on GoldSrc: TFC uses 1 blue, 2 red, 3 yellow, 4 green. HLDM, DMC and Ricochet are stored as team 0 unless `mp_teamplay` is on.

## `data/manifest.json`

```json
{"generated": 1795363327,
 "classes": [{"id": 1, "key": "scout", "label": "Scout"}, ...],
 "games": {"tf": {"name": "Team Fortress 2", "engine": "source", "icons": {},
                  "maps": [{"map": "ctf_2fort", "samples": 12345, "lastSeen": 1795363000, "geo": true, "nav": true}]}}}
```

## `data/<game>/<map>/meta.json`

Contains:
- counts (`cells`, `edges`, `samples.human`/`bot`/`deaths`)
- `teams` (`{"2": {"name": "Red", "color": "#d8403f"}}`)
- `classes` present
- `bounds` (`[[minx,miny,minz],[maxx,maxy,maxz]]` of the brushwork)
- `has.geo` / `has.nav` / `has.analysis`

## `cells.bin` (little-endian)

```
"HMC1"  u32 count
u32 n[count]
i16 x[count]  i16 y[count]  i16 z[count]
u8 kind[count]  u8 team[count]  u8 cls[count]  u8 bot[count]
```

## `edges.bin`

Movement edges, strongest first. The number of edges is capped per map (default 60,000).

```
"HME1"  u32 count
u32 n[count]
i16 x[count] i16 y[count] i16 z[count] i16 x2[count] i16 y2[count] i16 z2[count]
u8 team[count] u8 cls[count] u8 bot[count]
```

## `geo.bin`

The map's low-poly brushwork:
- World faces plus static brush entities (func_brush, func_wall).
- Displacements included, at no more than 5×5 vertices each.
- Tool, sky and trigger faces removed, and the 3D skybox removed.
- Triangles are counter-clockwise around their outward normal.

```
"HMG1"  u32 vertexCount  u32 indexCount  u32 indexSize (2 or 4)
i16 position[vertexCount*3]   (rounded world units)
(pad to 4 bytes)
u16|u32 index[indexCount]
```

## `nav.json`

Present when the server has a `.nav` for the map.

```json
{"version": 16, "subversion": 2,
 "areas": [[id, nwX, nwY, nwZ, seX, seY, seZ, neZ, swZ, attributeFlags], ...],
 "conn":  [[connected area ids of area 0], [... area 1], ...]}
```

The corners are:
- NW = (nwX, nwY, nwZ)
- NE = (seX, nwY, neZ)
- SE = (seX, seY, seZ)
- SW = (nwX, seY, swZ)

## `analysis.json` (for nav and bot work)

Present when the map has a nav mesh. Only human data is used, because bots only walk where the mesh already lets them.

- **`gaps`**: `[x, y, z, humanSamples]`, sorted by samples. These are 32-unit cells where humans stood on the ground but no nav area lies within 40 units vertically (8 units of horizontal slack). They are candidates for missing nav areas.
- **`missingLinks`**: `[fromAreaId, toAreaId, humanMoves, fromPos, toPos]`. Humans moved from area A to area B within one sample (about 1 s), but B is not within two connections of A. They are candidates for missing connections, jump or drop links, or ladders.

Both lists are heuristics: rocket jumps, teleporters and knockback show up too. Weigh them by count, and check them against the brushwork in the viewer.

## Reading the files (Python)

```python
import struct, numpy as np, urllib.request
b = urllib.request.urlopen("https://<site>/data/tf/ctf_2fort/cells.bin").read()
assert b[:4] == b"HMC1"; n, = struct.unpack_from("<I", b, 4); o = 8
cnt = np.frombuffer(b, "<u4", n, o); o += 4 * n
x, y, z = (np.frombuffer(b, "<i2", n, o + 2 * n * i) for i in range(3)); o += 6 * n
kind, team, cls, bot = (np.frombuffer(b, "u1", n, o + n * i) for i in range(4))
ground_humans = (kind == 0) & (bot == 0)
```
