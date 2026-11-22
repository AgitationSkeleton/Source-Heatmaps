"""Build the class icons for the site from the game files you own (nothing is downloaded).

config.json "icons" maps an output name (e.g. "scout", "pf2/civilian") to a list of sources tried in
order: {"file": "path/to/x.vtf"} (or .png) or {"vpk": "path/to/pak_dir.vpk", "path": "materials/..."}.
The zombie icon is generated from the juggernaut icon (sniper if missing) with a green tint, unless
"zombie" is configured.
"""
import io
import os

from PIL import Image, ImageOps

from .geo import vpk as vpkmod
from .geo import vtf


def _load(src):
    if "file" in src:
        p = src["file"]
        if not os.path.isfile(p):
            return None
        data = open(p, "rb").read()
    else:
        if not os.path.isfile(src.get("vpk", "")):
            return None
        data = vpkmod.VPK(src["vpk"]).read(src["path"])
        if data is None:
            return None
    if data[:4] == b"VTF\0":
        return vtf.decode(data)
    return Image.open(io.BytesIO(data)).convert("RGBA")


def build(cfg, log=print):
    out_dir = os.path.join(cfg["site_dir"], "icons")
    made = {}
    for name, sources in cfg.get("icons", {}).items():
        if name.startswith("_") or not isinstance(sources, list):
            continue
        img = None
        for s in sources:
            try:
                img = _load(s)
            except Exception as e:
                log("icon %s: %s" % (name, e))
            if img is not None:
                break
        if img is None:
            log("icon %s: no source found" % name)
            continue
        img = img.resize((64, 64), Image.LANCZOS) if img.size != (64, 64) else img
        dst = os.path.join(out_dir, name + ".png")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        img.save(dst, optimize=True)
        made[name] = img
    base = made.get("juggernaut") or made.get("sniper")
    if "zombie" not in made and base is not None:
        s = base
        r, g, b, a = s.split()
        gray = ImageOps.grayscale(Image.merge("RGB", (r, g, b)))
        tinted = ImageOps.colorize(gray, black="#0b2a10", white="#9cf07a").convert("RGBA")
        tinted.putalpha(a)
        tinted.save(os.path.join(out_dir, "zombie.png"), optimize=True)
        made["zombie"] = tinted
    log("icons: %d written to %s" % (len(made), out_dir))
    return sorted(made)
