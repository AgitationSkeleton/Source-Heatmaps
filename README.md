# Source Heatmaps

Collects where players and bots go on Source and GoldSrc servers, by team and class, and shows it as heatmaps on the map's own brushwork in a static, read-only website. The data is also published in simple formats for nav mesh and bot work: nav gaps and missing nav links found from human movement.

- **SourceMod plugin** (SM 1.11+, no extensions):
  - Samples every living player and bot once a second and records deaths and movement between samples.
  - Buckets everything in memory and flushes once a minute to MySQL/MariaDB.
  - Its database account only needs INSERT on two staging tables.
- **Exporter** (Python 3.10+):
  - Validates the staging rows and folds them into aggregates.
  - Reads each map's BSP and `.nav` from your game install, building a low-poly mesh of the brushwork and the nav areas.
  - Writes static files for the site.
- **Website**: plain HTML, CSS and JS with three.js vendored locally. It has:
  - game and map pickers;
  - presence, deaths and airborne layers;
  - human and bot, team, and class filters, with class icons taken from your own game files;
  - movement lines in team colours;
  - nav areas, area connections, nav gaps and missing links, from the map's `.nav` or from bot waypoint graphs (jk_botti, Parabot, FoXBot);
  - a height cut for stacked floors;
  - a mobile layout.

  The site has no server-side code and nothing on it accepts writes.

Tested with Team Fortress 2, Team Fortress 2 Classified, Open Fortress, Pre-Fortress 2, Fortress Forever, TF2 Gold Rush, Counter-Strike: Source and Half-Life 2: Deathmatch (SourceMod), with Team Fortress Classic, Half-Life Deathmatch, Deathmatch Classic and Ricochet (AMX Mod X), and with QuakeWorld and QuakeWorld Team Fortress (FTEQW, via drop files; BSP29/BSP2 maps, loose or in .pak files, and fteqw-sv `.qnav` nav meshes).

```
game servers --INSERT only--> MySQL staging --exporter (validate, fold)--> aggregates
                                                     |
                     game install (BSP, NAV, icons) -+--> static files --> any web server
```

## Requirements

- SourceMod 1.11 or newer on each server, with the bundled MySQL driver.
- MySQL 5.7+ or MariaDB 10.3+ reachable from the game servers. Localhost is best.
- Python 3.10+ with `pip install -r pipeline/requirements.txt` (pymysql, numpy, Pillow) on a machine that can read the game install folders. Usually that's the game server box.
- Any static web server. `webserver/httpd-heatmaps.conf` is a hardened Apache config for a dedicated instance.

## Setup

### 1. Database

```
python -I sql/setup_db.py --mysql /path/to/mysql --secrets /secure/place/db.json
```

The script:
- asks for an admin password (it isn't stored);
- creates the `heatmap` database from `sql/schema.sql`;
- creates two accounts with random passwords, written only to the secrets file.

The accounts are:
- `hm_ingest`: column-level `INSERT` on `heatmap.hm_ingest` and `heatmap.hm_ingest_meta`, nothing else. It can't set `id` or `received`. Setups made with an older `setup_db.py` can switch with `sql/tighten_ingest.py`, which keeps the same password.
- `hm_export`: `SELECT, INSERT, UPDATE, DELETE` on `heatmap.*`, nothing else.

Both are created for `localhost` and `127.0.0.1`. If your game servers are on another machine, add `--ingest-host 192.0.2.15`, firewall port 3306 to those hosts only, and prefer TLS or a private network.

### 2. Plugin

1. Copy `plugin/plugins/source_heatmaps.smx` to `addons/sourcemod/plugins/`. Build it yourself from `plugin/scripting/` if you prefer.
2. Add this section to `addons/sourcemod/configs/databases.cfg`, using the `hm_ingest` password from the secrets file:

   ```
   "heatmap"
   {
       "driver"    "mysql"
       "host"      "127.0.0.1"
       "database"  "heatmap"
       "user"      "hm_ingest"
       "pass"      "<hm_ingest password>"
       "port"      "3306"
   }
   ```
3. Load it with `sm plugins load source_heatmaps`, or let it load at the next map change. If the section was added while the server was running, the plugin runs `sm_reload_databases` once by itself.

Cvars (written to `cfg/sourcemod/source_heatmaps.cfg`):

| Cvar | Default | |
|---|---|---|
| `sm_heatmap_enable` | 1 | collect data |
| `sm_heatmap_interval` | 1.0 | seconds between samples |
| `sm_heatmap_flush` | 60 | seconds between database flushes |
| `sm_heatmap_bots` | 1 | record bots too (stored as bots) |
| `sm_heatmap_game` | "" | game key (empty = game folder, e.g. `tf`, `tf2classified`, `open_fortress`) |
| `sm_heatmap_classmap` | "" | raw→canonical class overrides, e.g. `"10:11,11:10"` |
| `sm_heatmap_dbconfig` | heatmap | databases.cfg section |
| `sm_heatmap_maxbuckets` | 40000 | flush early at this many buckets; never buffers more |

Admin commands (RCON flag):
- `sm_heatmap_status` shows the connection state and how each player is labelled: team, raw class and canonical class.
- `sm_heatmap_flushnow` flushes the buffer immediately.

### 2b. GoldSrc servers (AMX Mod X)

1. Copy `plugin/plugins_amxx/source_heatmaps.amxx` to `addons/amxmodx/plugins/`. Build it yourself from `plugin/amxmodx/source_heatmaps.sma` with `amxxpc` if you prefer; the plugin targets AMX Mod X 1.8.2+.
2. Add `source_heatmaps.amxx` to `addons/amxmodx/configs/plugins.ini`.
3. Create `addons/amxmodx/configs/source_heatmaps.cfg`:

   ```
   amx_heatmap_sql_host "127.0.0.1"
   amx_heatmap_sql_user "hm_ingest"
   amx_heatmap_sql_pass "<hm_ingest password>"
   amx_heatmap_sql_db   "heatmap"
   ```
4. The plugin starts at the next map change. Check it with `amx_heatmap_status` (RCON access).

Other cvars: `amx_heatmap_enable`, `amx_heatmap_interval` (1.0), `amx_heatmap_flush` (60), `amx_heatmap_bots` (1), `amx_heatmap_game` (empty means the mod folder, e.g. `tfc`, `valve`), `amx_heatmap_maxbuckets` (20000).

- Positions are the player's feet (origin plus hull mins), the same as on Source.
- TFC classes map to the same class ids (HWGuy is heavy, civilian is civilian).
- HLDM, DMC and Ricochet without `mp_teamplay` are stored as free-for-all (team 0).
- In the exporter's `config.json`, give these games `"engine": "goldsrc"`. GoldSrc has no nav meshes, so the nav and analysis layers are hidden for them.

### 3. Exporter

1. Copy `pipeline/config.example.json` to `pipeline/config.json` and set:
   - **`secrets`**: the file from step 1.
   - **`site_src` / `site_dir`**: the `site/` folder and the folder your web server serves.
   - **`games`**: one entry per game key the plugin sends. List the install folders (the game folder that holds `maps/`) in `content` so the exporter can find BSP and NAV files. Add `team_colors` to pin team colours by index.
   - **`drop_dirs`**, **`class_map`** (optional, per game): folders where engines without database access write `*.hm` drop files (see docs/EXTENDING.md); used for QuakeWorld.
   - **`waypoints`** (optional, per game): GoldSrc bot waypoint graphs to show and analyse, e.g. `[{"format": "jkbotti", "dir": ".../addons/jk_botti/waypoints"}]`. Formats: `jkbotti`, `parabot`, `foxbot` (see docs/DATA_FORMAT.md).
   - **`icons`** (optional; ready-made icons ship in `site/icons/`): where to read the class icons from your own game files (VTF loose files or inside VPKs). The site shows text-only chips for classes without an icon. Zombie is generated from Juggernaut, or from Sniper if Juggernaut is missing.
2. Build the icons once with `python -I pipeline/run.py icons`.
3. Schedule `python -I pipeline/run.py all` every 5 to 15 minutes (cron or Task Scheduler). It lowers its own priority, runs one instance at a time, rebuilds only maps that changed, and caches map geometry until the BSP changes.

### 4. Website

Serve `site_dir` as static files. With the included Apache config:
- it is a separate instance with no PHP or CGI and no directory listings;
- it allows GET/HEAD only and listens on loopback;
- it sends a strict CSP, `nosniff`, `frame-ancestors 'none'` and related headers.

Put a reverse proxy or a Cloudflare Tunnel in front of it. See [docs/SECURITY.md](docs/SECURITY.md).

```
httpd -t -f /path/to/httpd-heatmaps.conf
httpd -k install -n SourceHeatmapsWeb -f /path/to/httpd-heatmaps.conf     (Windows service)
```

Run it as a low-privilege account (on Windows, `NT AUTHORITY\LocalService`). Grant that account read access to the site folder and the Apache `bin`/`modules` folders, and write access to its log folder only.

## Using the data for nav and bot work

Everything under `data/` is plain files that tools can fetch. See [docs/DATA_FORMAT.md](docs/DATA_FORMAT.md):
- `analysis.json` lists **nav gaps**: places humans stand where the nav mesh has no area.
- It also lists **missing links**: human moves between nav areas that the mesh doesn't connect within two hops.
- `cells.bin` and `edges.bin` hold the raw aggregates (by team, class, human or bot), and `nav.json` holds the parsed nav mesh.

## Extending to other engines

The data format has no engine-specific fields, and map loaders are registered per engine (`pipeline/hm/geo/__init__.py`). See [docs/EXTENDING.md](docs/EXTENDING.md) for adding GoldSrc (AMX Mod X) or Quake/FTEQW servers.

## Licence

GPL-3.0-or-later. three.js (`site/vendor/`) is MIT; its licence is in `site/vendor/three.LICENSE`. The class icons in `site/icons/` are game assets from TF2, TF2 Classified, Open Fortress and Pre-Fortress 2, included for convenience and not covered by the GPL (see `site/icons/NOTICE.md`). Map geometry is produced from your own game files and is not included.
