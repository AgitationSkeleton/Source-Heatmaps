"""Source engine BSP (VBSP v19-v21) -> low-poly world mesh.

Keeps the world model plus static brush entities (func_brush, func_wall, func_detail leftovers),
drops tool/sky/trigger faces, includes displacements at reduced resolution and removes the 3D
skybox (the BSP area that contains sky_camera) when it is a separate area.
"""
import lzma
import re
import struct

import numpy as np

LUMP_ENTITIES, LUMP_PLANES, LUMP_TEXDATA, LUMP_VERTEXES = 0, 1, 2, 3
LUMP_NODES, LUMP_TEXINFO, LUMP_FACES, LUMP_LEAFS = 5, 6, 7, 10
LUMP_EDGES, LUMP_SURFEDGES, LUMP_MODELS, LUMP_LEAFFACES = 12, 13, 14, 16
LUMP_DISPINFO, LUMP_DISP_VERTS, LUMP_TEXDATA_STRING_DATA, LUMP_TEXDATA_STRING_TABLE = 26, 33, 43, 44

SURF_SKY2D, SURF_SKY, SURF_TRIGGER, SURF_NODRAW, SURF_HINT, SURF_SKIP = 0x2, 0x4, 0x40, 0x80, 0x100, 0x200
SKIP_FLAGS = SURF_SKY2D | SURF_SKY | SURF_TRIGGER | SURF_NODRAW | SURF_HINT | SURF_SKIP
TOOL_RE = re.compile(r"^tools/tools(nodraw|clip|playerclip|npcclip|skybox|skybox2d|trigger|hint|skip|invisible|"
                     r"invisibleladder|areaportal|occluder|fog|blocklight|blockbullets|blocklos|climb|nodraw_roof)", re.I)
# limits against malformed or hostile map files
MAX_FILE = 768 * 1024 * 1024
MAX_LUMP = 256 * 1024 * 1024
MAX_FACES = 1_000_000
MAX_MODELS = 4096
MAX_TRIS = 4_000_000
# Brush entities that are NOT drawn: triggers and invisible gameplay volumes. Every other brush
# entity (doors, breakables, func_illusionary grates, rotating/moving parts, ...) is drawn at its
# placed position; tool-textured faces are dropped separately.
HIDDEN_BRUSH_ENTS = {
    "func_respawnroom", "func_respawnroomvisualizer", "func_regenerate", "func_nobuild", "func_ladder",
    "func_clip_vphysics", "func_capturezone", "func_smokevolume", "func_dustmotes", "func_dustcloud",
    "func_precipitation", "func_areaportal", "func_areaportalwindow", "func_occluder", "func_bomb_target",
    "func_buyzone", "func_hostage_rescue", "func_vehicleclip", "func_clip", "func_forcefield",
    "func_upgradestation", "func_flagdetectionzone", "func_nogrenades", "func_suggested_build",
    "func_croc", "func_passtime_goal", "func_vip_safety_zone", "func_escapezone", "func_vip_safetyzone",
    "i_t_g", "info_tfgoal", "func_tfgoal", "info_areadef", "func_monsterclip", "func_hostage_rescue",
}


def drawn_brush_entity(classname):
    c = (classname or "").lower()
    return not (c.startswith(("trigger_", "func_nav_", "func_nav")) or c in HIDDEN_BRUSH_ENTS)


class BSP:
    def __init__(self, path):
        import os
        if os.path.getsize(path) > MAX_FILE:
            raise ValueError("map file too large")
        self.data = open(path, "rb").read()
        ident, self.version = struct.unpack_from("<4si", self.data, 0)
        if ident != b"VBSP":
            raise ValueError("not a Source BSP (%r)" % ident)
        self.lumps = [struct.unpack_from("<iii4s", self.data, 8 + i * 16) for i in range(64)]

    def lump(self, i):
        ofs, ln, ver, fourcc = self.lumps[i]
        raw = self.data[ofs:ofs + ln]
        if raw[:4] == b"LZMA":
            usize, csize = struct.unpack_from("<II", raw, 4)
            if usize > MAX_LUMP:
                raise ValueError("compressed lump too large")
            props = raw[12:17]
            alone = props + struct.pack("<Q", usize) + raw[17:17 + csize]
            raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(alone, max_length=usize)
        return raw

    def lump_version(self, i):
        return self.lumps[i][2]


def parse_entities(text):
    ents = []
    for block in re.findall(r"\{([^{}]*)\}", text):
        ents.append({k.lower(): v for k, v in re.findall(r'"([^"]*)"\s*"([^"]*)"', block)})
    return ents


def _vec(s):
    try:
        v = [float(x) for x in s.split()[:3]]
    except ValueError:
        return [0.0, 0.0, 0.0]
    # reject nan/inf and absurd values (a hostile entity lump could carry them)
    if len(v) != 3 or not all(-1e6 < x < 1e6 for x in v):
        return [0.0, 0.0, 0.0]
    return v


def load(path, disp_step=None):
    b = BSP(path)
    ents_text = b.lump(LUMP_ENTITIES).decode("utf-8", "replace")
    ents = parse_entities(ents_text)

    verts = np.frombuffer(b.lump(LUMP_VERTEXES), dtype="<f4").reshape(-1, 3)
    edges = np.frombuffer(b.lump(LUMP_EDGES), dtype="<u2").reshape(-1, 2)
    surfedges = np.frombuffer(b.lump(LUMP_SURFEDGES), dtype="<i4")
    faces_raw = b.lump(LUMP_FACES)
    nfaces = len(faces_raw) // 56
    if nfaces > MAX_FACES:
        raise ValueError("too many faces")
    texinfo_raw = b.lump(LUMP_TEXINFO)
    texdata_raw = b.lump(LUMP_TEXDATA)
    sdata = b.lump(LUMP_TEXDATA_STRING_DATA)
    stable = np.frombuffer(b.lump(LUMP_TEXDATA_STRING_TABLE), dtype="<i4")
    models_raw = b.lump(LUMP_MODELS)
    planes = np.frombuffer(b.lump(LUMP_PLANES), dtype="<f4").reshape(-1, 5)

    def face_normal(fo):
        pn, side = struct.unpack_from("<HB", faces_raw, fo)
        nrm = planes[pn][:3].astype(np.float64) if pn < len(planes) else np.zeros(3)
        return -nrm if side else nrm

    def texname(ti):
        if ti < 0 or ti * 72 + 72 > len(texinfo_raw):
            return "", 0
        flags, td = struct.unpack_from("<ii", texinfo_raw, ti * 72 + 64)
        name = ""
        if 0 <= td and td * 32 + 32 <= len(texdata_raw):
            sid, = struct.unpack_from("<i", texdata_raw, td * 32 + 12)
            if 0 <= sid < len(stable):
                o = stable[sid]
                e = sdata.find(b"\0", o)
                name = sdata[o:e].decode("utf-8", "replace")
        return name, flags

    tex_cache = {}

    def face_ok(ti):
        r = tex_cache.get(ti)
        if r is None:
            name, flags = texname(ti)
            r = not (flags & SKIP_FLAGS) and not TOOL_RE.match(name.replace("\\", "/"))
            tex_cache[ti] = r
        return r

    # ---- which faces belong to the 3D skybox ----
    sky_box = None
    try:
        sky_box = _skybox_bbox(b, ents)
    except Exception:
        sky_box = None

    def in_sky(pts):
        if sky_box is None:
            return False
        c = pts.mean(0)
        return bool(np.all(c >= sky_box[0]) and np.all(c <= sky_box[1]))

    # ---- models to include: world + static brush entities ----
    model_list = [(0, (0.0, 0.0, 0.0))]
    seen_models = {0}
    for e in ents:
        m = e.get("model", "")
        if drawn_brush_entity(e.get("classname", "")) and m.startswith("*") and len(model_list) < MAX_MODELS:
            try:
                mi = int(m[1:])
            except ValueError:
                continue
            if mi in seen_models:      # each brush model once (a hostile map could repeat one forever)
                continue
            seen_models.add(mi)
            model_list.append((mi, tuple(_vec(e.get("origin", "0 0 0")))))

    out_v = []
    out_i = []
    nv = 0
    disp_faces = []
    for mi, origin in model_list:
        if mi * 48 + 48 > len(models_raw):
            continue
        first, num = struct.unpack_from("<ii", models_raw, mi * 48 + 40)
        ox, oy, oz = origin
        for fi in range(first, min(first + num, nfaces)):
            fo = fi * 56
            firstedge, numedges, texi, dispi = struct.unpack_from("<ihhh", faces_raw, fo + 4)
            if numedges < 3 or numedges > 512 or firstedge < 0 or not face_ok(texi):
                continue
            if dispi >= 0:
                disp_faces.append((fi, dispi))
                continue
            se = surfedges[firstedge:firstedge + numedges]
            idx = np.where(se >= 0, edges[np.abs(se), 0], edges[np.abs(se), 1])
            pts = verts[idx]
            if mi:
                pts = pts + np.array([ox, oy, oz], dtype=np.float32)
            if in_sky(pts):
                continue
            # orient counter-clockwise around the face normal (three.js front faces)
            if np.dot(np.cross(pts[1] - pts[0], pts[2] - pts[0]), face_normal(fo)) < 0:
                pts = pts[::-1]
            out_v.append(pts)
            k = len(pts)
            tri = np.empty((k - 2, 3), dtype=np.uint32)
            tri[:, 0] = nv
            tri[:, 1] = nv + np.arange(1, k - 1)
            tri[:, 2] = nv + np.arange(2, k)
            out_i.append(tri)
            nv += k

    # ---- displacements ----
    if disp_faces:
        dinfo = b.lump(LUMP_DISPINFO)
        dverts = np.frombuffer(b.lump(LUMP_DISP_VERTS), dtype="<f4").reshape(-1, 5)
        for fi, di in disp_faces:
            if di * 176 + 176 > len(dinfo):
                continue
            sx, sy, sz, vstart, tstart, power = struct.unpack_from("<fffiii", dinfo, di * 176)
            if power not in (2, 3, 4):
                continue
            fo = fi * 56
            firstedge, numedges = struct.unpack_from("<ih", faces_raw, fo + 4)
            if numedges != 4:
                continue
            se = surfedges[firstedge:firstedge + 4]
            idx = np.where(se >= 0, edges[np.abs(se), 0], edges[np.abs(se), 1])
            corners = verts[idx].astype(np.float64)
            if in_sky(corners):
                continue
            start = np.array([sx, sy, sz])
            s = int(np.argmin(((corners - start) ** 2).sum(1)))
            c = np.roll(corners, -s, axis=0)
            n = (1 << power) + 1
            if vstart < 0 or vstart + n * n > len(dverts):
                continue
            dv = dverts[vstart:vstart + n * n]
            step = disp_step or max(1, (n - 1) // 4)   # at most 5x5 samples per displacement
            ids = list(range(0, n, step))
            if ids[-1] != n - 1:
                ids.append(n - 1)
            m = len(ids)
            grid = np.empty((m, m, 3))
            for a, yy in enumerate(ids):
                ty = yy / (n - 1)
                left = c[0] + (c[1] - c[0]) * ty
                right = c[3] + (c[2] - c[3]) * ty
                for bb, xx in enumerate(ids):
                    tx = xx / (n - 1)
                    base = left + (right - left) * tx
                    r = dv[yy * n + xx]
                    grid[a, bb] = base + r[:3] * r[3]
            out_v.append(grid.reshape(-1, 3).astype(np.float32))
            flip = np.dot(np.cross(c[1] - c[0], c[3] - c[0]), face_normal(fo)) < 0
            tris = []
            for a in range(m - 1):
                for bb in range(m - 1):
                    p0 = nv + a * m + bb
                    p1, p2, p3 = p0 + 1, p0 + m, p0 + m + 1
                    if flip:
                        tris += [(p0, p1, p2), (p1, p3, p2)]
                    else:
                        tris += [(p0, p2, p1), (p1, p2, p3)]
            out_i.append(np.array(tris, dtype=np.uint32))
            nv += m * m

    if sum(len(i) for i in out_i) > MAX_TRIS:
        raise ValueError("too many triangles")
    if out_v:
        V = np.concatenate(out_v).astype(np.float32)
        I = np.concatenate(out_i).astype(np.uint32)
    else:
        V = np.zeros((0, 3), np.float32)
        I = np.zeros((0, 3), np.uint32)
    # drop triangles that touch non-finite or out-of-range vertices
    if len(V):
        bad = ~np.all(np.isfinite(V) & (np.abs(V) < 65536), axis=1)
        if bad.any():
            I = I[~np.any(bad[I], axis=1)]
            V = np.where(bad[:, None], 0.0, V).astype(np.float32)
    V, I = _weld(V, I)
    return {"vertices": V, "indices": I, "entities": ents, "version": b.version}


def _weld(V, I, q=0.5):
    if len(V) == 0:
        return V, I
    key = np.round(V / q).astype(np.int64)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    newV = np.zeros((len(uniq), 3), np.float32)
    newV[inv] = V
    I2 = inv[I]
    good = (I2[:, 0] != I2[:, 1]) & (I2[:, 1] != I2[:, 2]) & (I2[:, 0] != I2[:, 2])
    return newV, I2[good].astype(np.uint32)


def _skybox_bbox(b, ents):
    """Bounding box of the BSP area holding sky_camera, or None when it can't be separated."""
    cam = next((e for e in ents if e.get("classname") == "sky_camera"), None)
    if cam is None:
        return None
    planes = np.frombuffer(b.lump(LUMP_PLANES), dtype="<f4").reshape(-1, 5)
    nodes = b.lump(LUMP_NODES)
    leafs = b.lump(LUMP_LEAFS)
    lsize = 56 if b.lump_version(LUMP_LEAFS) == 0 else 32
    if len(leafs) % lsize:
        lsize = 32 if len(leafs) % 32 == 0 else 56
    head, = struct.unpack_from("<i", b.lump(LUMP_MODELS), 36)

    def leaf_of(p):
        n, guard = head, 0
        while n >= 0 and guard < 100000:
            pl, c0, c1 = struct.unpack_from("<iii", nodes, n * 32)
            nx, ny, nz, d = planes[pl][:4]
            n = c0 if nx * p[0] + ny * p[1] + nz * p[2] - d >= 0 else c1
            guard += 1
        return -1 - n

    def area(li):
        f, = struct.unpack_from("<h", leafs, li * lsize + 6)
        return f & 0x1FF

    sky = area(leaf_of(_vec(cam.get("origin", "0 0 0"))))
    mins, maxs = [], []
    for li in range(len(leafs) // lsize):
        contents, = struct.unpack_from("<i", leafs, li * lsize)
        if contents & 1 or area(li) != sky:
            continue
        mins.append(struct.unpack_from("<3h", leafs, li * lsize + 8))
        maxs.append(struct.unpack_from("<3h", leafs, li * lsize + 14))
    if not mins:
        return None
    lo = np.min(np.array(mins), 0) - 1.0
    hi = np.max(np.array(maxs), 0) + 1.0
    # never cut away playable space: no spawn point may sit inside the box
    for e in ents:
        if e.get("classname", "").startswith(("info_player_", "info_ff_teamspawn")):
            p = np.array(_vec(e.get("origin", "0 0 0")))
            if np.all(p >= lo) and np.all(p <= hi):
                return None
    return lo, hi
