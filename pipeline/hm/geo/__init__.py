"""Map loaders by engine. Each engine provides:

  map_ext / nav_ext   file extensions looked up in the game's maps folders (nav_ext may be None)
  geometry(path)      -> {"vertices": float32 (N,3), "indices": uint32 (M,3)}  world-space, Z up,
                         triangles counter-clockwise around their outward normal
  nav(path)           -> {"version", "subversion", "areas": [...]} (see nav_source.load) or None

To add an engine (e.g. GoldSrc BSP v30, Quake/FTE BSP29/BSP2), write a module with those two
functions and register it here; the exporter, the site and the data formats stay the same.
"""
from . import bsp_goldsrc, bsp_source, nav_source, qnav

LOADERS = {
    "source": {"map_ext": ".bsp", "nav_ext": ".nav", "geometry": bsp_source.load, "nav": nav_source.load},
    # GoldSrc (BSP v30; Quake v29 too): no nav meshes
    "goldsrc": {"map_ext": ".bsp", "nav_ext": None, "geometry": bsp_goldsrc.load, "nav": None},
    # Quake / QuakeWorld (FTEQW): BSP29 or BSP2, maps loose or inside .pak files; fteqw-sv NextBot
    # nav meshes are <gamedir>/nav/<map>.qnav
    "quake": {"map_ext": ".bsp", "nav_ext": ".qnav", "nav_subdir": "nav", "paks": True,
              "geometry": bsp_goldsrc.load, "nav": qnav.load},
}
