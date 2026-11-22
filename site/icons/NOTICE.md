# Class icons

These are the leaderboard class icons from the games themselves, converted from VTF to PNG by
`pipeline/hm/icons.py`. They are **not** covered by this repository's GPL licence; they belong to
their respective owners and are included only so the viewer can label classes out of the box.

| File | Source |
|---|---|
| scout, soldier, pyro, demoman, heavy, engineer, medic, sniper, spy | Team Fortress 2 leaderboard icons (Valve Corporation), as shipped with Team Fortress 2 Classic beta 4 |
| civilian | Team Fortress 2 Classified (`tf2c_assets` VPK) |
| pf2/civilian | Pre-Fortress 2 (`pf2_textures` VPK); used for the Pre-Fortress 2 game only |
| mercenary, juggernaut | Open Fortress |
| zombie | Open Fortress's juggernaut icon tinted green by `icons.py` |

To rebuild them from your own installs instead, set the `icons` section of `pipeline/config.json` and
run `python -I pipeline/run.py icons`. If you are a rights holder and want an icon removed, open an issue.
