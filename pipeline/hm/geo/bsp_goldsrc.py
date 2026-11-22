"""GoldSrc BSP (v30; also Quake v29) -> low-poly world mesh.

Keeps the world model plus static brush entities (func_wall, func_illusionary), drops sky, clip,
trigger and other tool textures. GoldSrc has no 3D skybox and no nav meshes.
"""
import os
import struct

import numpy as np

from .bsp_source import MAX_FACES, MAX_FILE, MAX_MODELS, MAX_TRIS, _vec, _weld, parse_entities

L_ENTITIES, L_PLANES, L_TEXTURES, L_VERTICES = 0, 1, 2, 3
L_TEXINFO, L_FACES, L_EDGES, L_SURFEDGES, L_MODELS = 6, 7, 12, 13, 14

SKIP_TEX = ("sky", "clip", "aaatrigger", "origin", "null", "bevel", "hint", "skip", "trigger", "nodraw")
STATIC_BRUSH_ENTS = {"func_wall", "func_illusionary", "func_wall_toggle", "func_detail"}


def load(path):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError("map file too large")
    data = open(path, "rb").read()
    version, = struct.unpack_from("<i", data, 0)
    if version not in (29, 30):
        raise ValueError("not a GoldSrc/Quake BSP (version %d)" % version)
    lumps = [struct.unpack_from("<ii", data, 4 + i * 8) for i in range(15)]

    def lump(i):
        ofs, ln = lumps[i]
        if ofs < 0 or ln < 0 or ofs + ln > len(data):
            raise ValueError("lump %d out of range" % i)
        return data[ofs:ofs + ln]

    ents = parse_entities(lump(L_ENTITIES).decode("latin-1", "replace"))
    verts = np.frombuffer(lump(L_VERTICES), dtype="<f4").reshape(-1, 3)
    edges = np.frombuffer(lump(L_EDGES), dtype="<u2").reshape(-1, 2)
    surfedges = np.frombuffer(lump(L_SURFEDGES), dtype="<i4")
    planes = np.frombuffer(lump(L_PLANES), dtype="<f4").reshape(-1, 5)
    faces_raw = lump(L_FACES)
    nfaces = len(faces_raw) // 20
    if nfaces > MAX_FACES:
        raise ValueError("too many faces")
    texinfo = lump(L_TEXINFO)
    tex = lump(L_TEXTURES)
    models = lump(L_MODELS)

    # texture names from the miptex lump
    names = []
    if len(tex) >= 4:
        n, = struct.unpack_from("<i", tex, 0)
        for i in range(max(0, min(n, 65536))):
            if 4 + i * 4 + 4 > len(tex):
                break
            o, = struct.unpack_from("<i", tex, 4 + i * 4)
            if 0 <= o and o + 16 <= len(tex):
                names.append(tex[o:o + 16].split(b"\0")[0].decode("latin-1").lower())
            else:
                names.append("")

    ok_cache = {}

    def face_ok(ti):
        r = ok_cache.get(ti)
        if r is None:
            r = False
            if 0 <= ti and ti * 40 + 40 <= len(texinfo):
                mip, flags = struct.unpack_from("<ii", texinfo, ti * 40 + 32)
                name = names[mip] if 0 <= mip < len(names) else ""
                r = not (flags & 1) and not name.startswith(SKIP_TEX)   # TEX_SPECIAL = sky/water warp
                if name.startswith(("!", "*")):    # water/slime/lava: keep as a surface
                    r = True
            ok_cache[ti] = r
        return r

    model_list = [(0, (0.0, 0.0, 0.0))]
    seen = {0}
    for e in ents:
        m = e.get("model", "")
        if e.get("classname", "") in STATIC_BRUSH_ENTS and m.startswith("*") and len(model_list) < MAX_MODELS:
            try:
                mi = int(m[1:])
            except ValueError:
                continue
            if mi not in seen:
                seen.add(mi)
                model_list.append((mi, tuple(_vec(e.get("origin", "0 0 0")))))

    out_v, out_i, nv = [], [], 0
    msize = 64
    for mi, origin in model_list:
        if mi * msize + msize > len(models):
            continue
        first, num = struct.unpack_from("<ii", models, mi * msize + 56)
        for fi in range(max(0, first), min(first + num, nfaces)):
            fo = fi * 20
            pn, side, firstedge, numedges, ti = struct.unpack_from("<HHiHH", faces_raw, fo)
            if numedges < 3 or numedges > 512 or firstedge < 0 or firstedge + numedges > len(surfedges) or not face_ok(ti):
                continue
            se = surfedges[firstedge:firstedge + numedges]
            if np.any(np.abs(se) >= len(edges)):
                continue
            idx = np.where(se >= 0, edges[np.abs(se), 0], edges[np.abs(se), 1])
            if np.any(idx >= len(verts)):
                continue
            pts = verts[idx]
            if mi:
                pts = pts + np.array(origin, dtype=np.float32)
            nrm = planes[pn][:3].astype(np.float64) if pn < len(planes) else np.zeros(3)
            if side:
                nrm = -nrm
            if np.dot(np.cross(pts[1] - pts[0], pts[2] - pts[0]), nrm) < 0:
                pts = pts[::-1]
            out_v.append(pts)
            k = len(pts)
            tri = np.empty((k - 2, 3), dtype=np.uint32)
            tri[:, 0] = nv
            tri[:, 1] = nv + np.arange(1, k - 1)
            tri[:, 2] = nv + np.arange(2, k)
            out_i.append(tri)
            nv += k
    if sum(len(i) for i in out_i) > MAX_TRIS:
        raise ValueError("too many triangles")
    if out_v:
        V = np.concatenate(out_v).astype(np.float32)
        I = np.concatenate(out_i).astype(np.uint32)
    else:
        V, I = np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32)
    if len(V):
        bad = ~np.all(np.isfinite(V) & (np.abs(V) < 65536), axis=1)
        if bad.any():
            I = I[~np.any(bad[I], axis=1)]
            V = np.where(bad[:, None], 0.0, V).astype(np.float32)
    V, I = _weld(V, I)
    return {"vertices": V, "indices": I, "entities": ents, "version": version}
