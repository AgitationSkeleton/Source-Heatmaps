# Security model

Source Heatmaps is meant to be exposed publicly while staying read-only. The only writers are your game servers, and even they can only append to a staging table.

## Trust boundaries

| Component | Trust | What it can do |
|---|---|---|
| Website visitors | untrusted | GET/HEAD static files. Nothing else exists to call. |
| Game servers / plugin (`hm_ingest`) | semi-trusted | Column-level `INSERT` into `hm_ingest` and `hm_ingest_meta`. Can't read, update or delete, and can't set `id` or `received`. |
| Players | untrusted | Only influence positions, teams and classes. Map and team names go through `Database.Escape`. |
| Map files (BSP/NAV from the game install, often downloaded) | untrusted input | Parsed only in a child process with a 2 GB memory cap and a 180 s timeout, with size, count and decompression limits. |
| Exporter (`hm_export`) | trusted, low-privilege | `SELECT/INSERT/UPDATE/DELETE` on `heatmap.*` only. Runs as a low-privilege account, not as root or SYSTEM. |

## Defences, by attack

- **XSS through data:**
  - Staging rows are validated before they're folded. Map names must be lower-case `[a-z0-9_.+-]` and compared as binary. Rejected: trailing dots or newlines, `..`, Windows device names, unknown games, out-of-range values, and maps that don't exist in the game's content folders.
  - JSON is written ASCII-escaped.
  - The page builds the DOM with `textContent` and `new Option` only (no `innerHTML`) and re-validates names, colours and icon paths with strict patterns.
  - The CSP is `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'`, with no inline code.
- **Path traversal or writes outside `data/`:** map and game names are validated as safe folder names before any path is built.
- **Web server:**
  - A separate, static-only Apache instance with no PHP, CGI or directory listings, `AllowMethods GET HEAD`, and dotfiles and backup or temp extensions denied.
  - Request body limited to 1 KB, header lines to 4 KB.
  - Header rules match case-insensitively (Windows paths are case-insensitive).
  - Rotated logs, 5 × 50 MB each.
  - Loopback-only listener. Publish it through a reverse proxy or tunnel; don't port-forward it.
- **Disk use:** everything the system writes is bounded.
  - Database aggregates: at most 300k cells and 100k movement edges per map (the weakest rows go first), plus a global budget of 20M rows. Over budget, single-sample rows are pruned.
  - Site data: per-map export caps, and only maps that exist on disk.
  - Staging: emptied every run.
  - Logs: web logs 5 × 50 MB each; pipeline log rotates at 5 MB, 3 kept.
  - Drop folders: at most 5,000 pending files.
- **Staging floods:** compaction runs in 200k-row chunks. Rows for maps without a map file are discarded. Exports cap cells at 400k per map and edges at 60k. Run one ingest account per server group if you want to be able to cut off a single source.
- **Hostile map files:**
  - LZMA lumps are decompressed with `max_length` and a 256 MB cap.
  - Displacement power must be 2 to 4.
  - Each brush model is drawn once.
  - Caps on faces, triangles, file size, nav area count and size, and nav index entries.
  - Parsing happens in the worker process with a memory limit and timeout. Failures are cached until the file changes.
  - A lock left by a killed run is cleared when its process is gone.
- **Secrets:** the DB passwords live in one secrets file and in `databases.cfg`. Restrict both to administrators and the account that needs them. Nothing secret is ever written under the web root.

## File-system permissions (Windows)

Break inheritance on the install folder: drive roots often grant `Authenticated Users: Modify`, which would let any local account edit code that the exporter runs.

| Folder | Administrators, SYSTEM | Exporter account (e.g. NETWORK SERVICE) | Web account (e.g. LocalService) |
|---|---|---|---|
| install root | Full | RX (this folder) | RX (this folder) |
| `app` (exporter code) | Full | RX | none |
| `www` (web root) | Full | Modify | RX |
| `conf` (web config) | Full | none | RX |
| `logs` (web logs) | Full | none | Modify |
| `cache`, `pipelinelogs` | Full | Modify | none |
| `secrets/db.json` | Full | Read | none |
| the game's `maps` folders | n/a | RX | none |

The web account also needs RX on Apache's `bin` and `modules` folders. Don't grant it Apache's `conf`, which may hold TLS keys for other sites.

## Behind Cloudflare

- Proxy the hostname, and use **Full (strict)** SSL with a tunnel or origin certificate.
- Enable **Always Use HTTPS** and HSTS.
- Add a cache rule that caches `/vendor/*` and `/icons/*` and ignores query strings, plus a rate-limiting rule on the hostname.
- Restrict allowed methods to GET/HEAD with a WAF custom rule. The origin already does this; the edge rule saves origin work.

## Known limits

- The plugin trusts its own server: a malicious SourceMod plugin on the same server could insert fake rows, within the validation above.
- Heatmap data is public by design. It shows where players go; it holds no player names or Steam IDs.
