#!/usr/bin/env python3
"""
iwmap2map.py - CoD4 (IW3) Radiant source map ("iwmap 4") -> SOF2 / idTech3 .map

Formats follow the KisakCOD Radiant reconstruction (src/radiant/brush.cpp,
pmesh.cpp, texturevecs.cpp), not guesswork:

  brush face   ( p1 ) ( p2 ) ( p3 ) MAT sizeX sizeY shiftX shiftY rot cross
               LMMAT lmSX lmSY lmShX lmShY lmRot lmCross [smoothing X]
      Face_MoveTexture: Q3's base-axis table and rotation, but in WORLD units:
          st = (xyz . axis(rot) - shift) / size        (normalized st)
      -> Q3 standard texdef: scale = size / texW, shift = -shift * texW / size
  patch        mesh|curve { [contents ..;] [toolFlags ..;] MAT LMMAT [smoothing]
               W H size subdiv  then W columns "( v x y z [c b g r a] t s t ls lt [f e] )" }
      t values are UV * 1024. 'curve' = bezier control grid (always odd sizes,
      maps 1:1 to patchDef2). 'mesh' = LINEAR terrain grid; expanded exactly to
      bezier by inserting midpoints ((2W-1) x (2H-1)), split at 31 per side.
  misc_prefab  inlined recursively with origin/angles/modelscale applied to
               brush planes, texture projections (re-solved per face), patch
               vertices and nested entities.

Texture projections are carried as a world-space affine and solved back into a
Q3 standard texdef on the final face. That is exact for any rotation that keeps
the face texture un-sheared (all yaw-only prefab rotations, which is nearly all
of them); faces where a pitch/roll rotation produces a projection the legacy
format cannot express get the least-error fit and are counted in the report.

Usage:
  python iwmap2map.py mp_shipment.map out.map --prefabs <dir containing prefabs/>
                      --assets <unzipped cod4_assets>  [--manifest out.json]

The manifest (JSON) lists every material/variant the .map uses with its image,
size, blend/alphatest/surface type, plus models and sun settings - input for
the shader/model steps.
"""
import argparse
import json
import math
import os
import re
import struct
import sys
from collections import Counter, OrderedDict, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cod4_names import map_tag, world_shader  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from material_iw3 import read_material
except ImportError:  # pragma: no cover
    read_material = None

CONTENTS_DETAIL = 0x8000000
# Same budget and reasoning as vmf2map.py's PROP_VERTEX_BUDGET: brushwork,
# lightmaps and vis are ~19 MB on their own; 150k baked prop vertices (80 bytes
# each in RBSP, ~12 MB) keeps the .bsp near a size SOF2 is known to load.
# mp_shipment at full LOD0 on every placement made a ~100 MB .bsp and SOF2
# died in Z_Malloc loading it.
# Baked prop vertices (80 bytes each in the .bsp). SOF2 loads the whole .bsp
# in ONE allocation in a 32-bit process: mp_shipment at 57.4 MB loads,
# mp_backlot at 71.7 MB failed ("Z_Malloc(): Failed to malloc 71688368").
# CoD brushwork is far heavier than the CS:S maps the old 150k was set for
# (backlot: ~50 MB of world, vis and light before any prop), and play-area
# props are model_static now (not in the .bsp), so 60k (~5 MB) is the share.
PROP_VERTEX_BUDGET = 60000
# CoD units -> SOF2 units. Same 1.25 the CS:S pipeline's GUI uses for Source
# (Source player 72 units, CoD4 70 - so CoD's ideal is ~1.28; 1.25 keeps both
# pipelines on one scale). Applied as the root transform, so brushes, patches,
# texture projections (texture world size scales too), models, MD3 offsets
# and entity positions all scale together.
DEFAULT_SCALE = 1.25
SKY_MARGIN = 1024.0         # sky box distance beyond the outermost geometry
MAX_PATCH = 31               # q3map2 MAX_PATCH_SIZE 32 -> odd sizes up to 31
# A solid patch also has to fit SOF2's per-patch collision arrays. The engine
# builds one or two facets per grid cell and a deduplicated plane per facet
# face and border (cm_patch.c CM_PatchCollideFromGrid), then aborts the map
# load with MAX_PATCH_PLANES or MAX_FACETS. Our control grids come from an
# already-tessellated CoD mesh, so they are dense enough that the engine never
# subdivides them further - the cell count is exactly (w-1)*(h-1) and entirely
# ours to choose. Measured on mp_derail's own patches, worst case is about 1.7
# planes and 1.7 facets per cell, so 256 cells keeps both near 440 - well
# inside the 1024 that a 27x29 snow_soft2_clip patch (1030 planes) broke.
MAX_PATCH_CELLS = 256
TEX_PREFIX = "cod4/"         # .map names (no "textures/"), shader = textures/cod4/...
MODEL_PREFIX = "models/cod4/"
SKIP_SHADER = "cod4/skip"         # textures/cod4/skip, written by cod4mat2shader.py
MIN_CLIP_THICK = 2.0       # world units, thinnest prop clip box
# The player box in the idTech3 family is mins (-15 -15 -24), so a spawn
# origin needs at least 24 units of clearance or the box starts inside the
# floor. 16 was not enough - spawns clipped through on mp_derail. 32 gives
# the 24 plus margin for a floor that is not perfectly flat under the point;
# the player just drops the remainder.
SPAWN_LIFT = 32.0
# props the budget may thin out (when every LOD is used): vegetation only
THIN_RE = re.compile(r"^(?!.*tree).*(grass|shrub|bush|weed|leaves|leaf_|plant|flower|ivy|vine|clump|fern|hedge)", re.I)

# ---------------------------------------------------------------------------
# tool materials -> SOF2 tool shaders (same confirmed targets vmf2map.py uses)
# value None = face/brush carries no meaning in SOF2: drop
# ---------------------------------------------------------------------------
class _ToolMap(dict):
    """CoD tool materials -> SOF2 tool shader (None = drop the face/brush).
    Exact names first, then the CoD naming families: CoD4 has surface-type
    variants of every clip (clip_nosight_dirt, clip_metal_..., clip_weap_...),
    and mp_strike's clip_nosight_dirt showed up in game as the yellow/red
    "clip nosight" editor texture."""
    PATTERNS = [
        (re.compile(r"^clip_nosight"), None),          # blocks AI sight only: nothing in SOF2
        (re.compile(r"^clip_(ai|vehicle|missile)"), None),
        (re.compile(r"^clip_weap"), "tools/_shoot"),
        (re.compile(r"^clip_player"), "tools/_blockplayer"),
        (re.compile(r"^clip(_|$)"), "tools/_clip"),
        (re.compile(r"^mantle_"), None),
        (re.compile(r"^(nodraw|caulk)_"), "tools/_caulk"),
        (re.compile(r"^(volume|lightgrid|shadowcaster|cinematic|hdrportal)"), None),
    ]
    _MISS = object()

    def _find(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        for rx, val in self.PATTERNS:
            if isinstance(key, str) and rx.search(key):
                return val
        return self._MISS

    def __contains__(self, key):
        return self._find(key) is not self._MISS

    def __getitem__(self, key):
        v = self._find(key)
        if v is self._MISS:
            raise KeyError(key)
        return v

    def get(self, key, default=None):
        v = self._find(key)
        return default if v is self._MISS else v


TOOL_MAP = _ToolMap({
    "caulk": "tools/_caulk",
    "clip": "tools/_clip",
    "clip_player": "tools/_blockplayer",
    "clip_metal": "tools/_clip",
    "clip_wood": "tools/_clip",
    "clip_water": "tools/_clip",
    "clip_foliage": "tools/_clip",
    "clip_snow": "tools/_clip",
    # CoD weapon clip blocks bullets/grenades. SOF2's confirmed _shoot is
    # playerclip+monsterclip+shotclip - closest available, also blocks players.
    "clip_weap": "tools/_shoot",
    "clip_weap_metal": "tools/_shoot",
    "trigger": "tools/_trigger",
    "origin": "tools/_origin",
    "hint": "tools/_hint",
    "ladder": "tools/_ladder",
    "nodraw": "tools/_caulk",
    "nodraw_decal": "tools/_nodraw",      # non-solid invisible decal backing
    # CoD portals cut the level into cells for CoD's own occlusion; they sit
    # in doorways and windows, exactly where a q3map2 hint helps vis. As
    # areaportals (no door to close them) they did nothing but warn
    # "areaportal brush with > 1 areaportal faces" / "touches > 2 areas".
    "portal": "tools/_hint",
    "portal_nodraw": SKIP_SHADER,
    "clip_nosight": None, "clip_nosight_metal": None, "clip_nosight_nothing": None,
    "clip_nosight_rock": None, "clip_ai": None, "clip_vehicle": None,
    "mantle_on": None, "mantle_over": None, "lightgrid_volume": None,
    "shadowcaster": None, "skip": None, "volume": None, "cinematic": None,
    "lightmap_gray": None,
})

# ---------------------------------------------------------------------------
# entity classes
# ---------------------------------------------------------------------------
MODEL_CLASSES = {"misc_model", "script_model", "dyn_model"}
BRUSH_GROUP_CLASSES = {"func_group", "script_brushmodel", "dyn_brushmodel"}
CLASS_MAP = {
    "mp_dm_spawn": "info_player_deathmatch",
    "mp_tdm_spawn_allies_start": "info_player_start_red",
    "mp_tdm_spawn_axis_start": "info_player_start_blue",
    "mp_global_intermission": "info_player_intermission",
}
# gametype-specific duplicates of the sets above: dropped so SOF2 does not
# get 6 overlapping spawn sets. Override with --class-map.
DROP_CLASSES = {
    "mp_tdm_spawn", "mp_dom_spawn", "mp_dom_spawn_allies_start", "mp_dom_spawn_axis_start",
    "mp_sab_spawn_allies_start", "mp_sab_spawn_axis_start", "mp_sab_spawn_allies",
    "mp_sab_spawn_axis", "mp_ctf_spawn_allies_start", "mp_ctf_spawn_axis_start",
    "mp_ctf_spawn_allies", "mp_ctf_spawn_axis", "mp_sd_spawn_attacker", "mp_sd_spawn_defender",
    "info_player_start", "script_origin", "script_struct", "reflection_probe", "info_null",
    "info_volume", "trigger_multiple", "trigger_radius", "trigger_use", "trigger_use_touch",
    "trigger_damage", "misc_turret",
}


# ===========================================================================
# math
# ===========================================================================
def v_sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def v_add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def v_scale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def v_dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def v_cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def v_len(a): return math.sqrt(v_dot(a, a))


def v_norm(a):
    l = v_len(a)
    return (a[0] / l, a[1] / l, a[2] / l) if l > 1e-12 else (0.0, 0.0, 0.0)


def angles_matrix(pitch, yaw, roll):
    """id AngleVectors: columns forward, left, up (world = M * local).
    Same convention q3map2 uses for misc_model and CoD for prefab angles."""
    sp, cp = math.sin(math.radians(pitch)), math.cos(math.radians(pitch))
    sy, cy = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
    sr, cr = math.sin(math.radians(roll)), math.cos(math.radians(roll))
    return ((cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy),
            (cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy),
            (-sp, sr * cp, cr * cp))


def matrix_angles(m):
    fx, fy, fz = m[0][0], m[1][0], m[2][0]
    lz, uz = m[2][1], m[2][2]
    h = math.hypot(fx, fy)
    pitch = math.degrees(math.atan2(-fz, h))
    if h > 1e-6:
        yaw = math.degrees(math.atan2(fy, fx))
        roll = math.degrees(math.atan2(lz, uz))
    else:   # gimbal: fold roll into yaw
        yaw = math.degrees(math.atan2(-m[0][1], m[1][1]))
        roll = 0.0
    return pitch, yaw, roll


def m_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def m_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


IDENT = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class Xform:
    """p_world = scale * R * p_local + origin"""
    __slots__ = ("R", "s", "o")

    def __init__(self, R=IDENT, s=1.0, o=(0.0, 0.0, 0.0)):
        self.R, self.s, self.o = R, s, o

    def point(self, p):
        return v_add(v_scale(m_vec(self.R, p), self.s), self.o)

    def then(self, parent):
        """self applied first, then parent."""
        return Xform(m_mul(parent.R, self.R), parent.s * self.s, parent.point(self.o))

    def is_identity(self):
        return self.R == IDENT and self.s == 1.0 and self.o == (0.0, 0.0, 0.0)


def parse_vec(s, default=(0.0, 0.0, 0.0)):
    try:
        v = tuple(float(x) for x in s.split())
        return v if len(v) == 3 else default
    except (ValueError, AttributeError):
        return default


def entity_xform(keys):
    o = parse_vec(keys.get("origin", "0 0 0"))
    if "angles" in keys:
        p, y, r = parse_vec(keys["angles"])
    elif "angle" in keys:
        p, y, r = 0.0, float(keys["angle"]), 0.0
    else:
        p = y = r = 0.0
    s = float(keys.get("modelscale", "1") or 1)
    return Xform(angles_matrix(p, y, r), s, o)


# ===========================================================================
# texture projection
# ===========================================================================
# Ed_baseaxis (texturevecs.cpp) == Q3 baseaxis: per dominant normal axis
# (+z,-z,+x,-x,+y,-y; strict '>' so ties keep the lower index) -> xv, yv
BASEAXIS = (((1, 0, 0), (0, -1, 0)), ((1, 0, 0), (0, -1, 0)),
            ((0, 1, 0), (0, 0, -1)), ((0, 1, 0), (0, 0, -1)),
            ((1, 0, 0), (0, 0, -1)), ((1, 0, 0), (0, 0, -1)))


def base_axis(n):
    best, idx = 0.0, 0
    for i, c in enumerate((n[2], -n[2], n[0], -n[0], n[1], -n[1])):
        if c > best:
            best, idx = c, i
    return BASEAXIS[idx]


def sincos_deg(d):
    d = d % 360.0
    for ang, sc in ((0.0, (0.0, 1.0)), (90.0, (1.0, 0.0)), (180.0, (0.0, -1.0)), (270.0, (-1.0, 0.0))):
        if d == ang:
            return sc
    r = math.radians(d)
    return math.sin(r), math.cos(r)


def axis_index(v):
    return 0 if v[0] else (1 if v[1] else 2)


def cod_face_affine(normal, size, shift, rot, cross):
    """Face_MoveTexture: returns ((A_s, c_s), (A_t, c_t)) with st = p.A + c."""
    sx = size[0] or 128.0
    sy = size[1] or 128.0
    xv, yv = base_axis(normal)
    s, t = axis_index(xv), axis_index(yv)
    r = 3 - s - t
    sn, cs = sincos_deg(rot)
    a = [0.0] * 8
    v10 = xv[s] / sx
    a[s] = cs * v10
    a[t] = v10 * sn
    a[r] = 0.0
    v12 = yv[t] / sy
    a[s + 4] = -sn * v12
    a[t + 4] = v12 * cs
    a[r + 4] = 0.0
    a[0] += a[4] * cross
    a[1] += a[5] * cross
    a[2] += a[6] * cross
    a[3] = -shift[0] / sx
    a[7] = -shift[1] / sy
    return ((a[0], a[1], a[2]), a[3]), ((a[4], a[5], a[6]), a[7])


def transform_affine(aff, xf):
    """st = p_local.A + c, p_world = s R p_local + o  ->  world affine."""
    out = []
    for A, c in aff:
        A2 = v_scale(m_vec(xf.R, A), 1.0 / xf.s)
        out.append((A2, c - v_dot(A2, xf.o)))
    return tuple(out)


def solve_q3_texdef(normal, plane_d, aff, tex_w, tex_h):
    """Find Q3 (shiftS, shiftT, rot, scaleS, scaleT) reproducing `aff` on the
    plane n.p = d. Returns (texdef tuple, residual in texels)."""
    xv, yv = base_axis(normal)
    s, t = axis_index(xv), axis_index(yv)
    r = 3 - s - t
    k = xv[s] * yv[t]
    nr = normal[r]
    # on the plane: p[r] = (d - n[s] p[s] - n[t] p[t]) / n[r];  U = xv[s] p[s], V = yv[t] p[t]
    rows = []
    for (A, c), size in ((aff[0], tex_w), (aff[1], tex_h)):
        if abs(nr) > 1e-9:
            ks = A[s] - A[r] * normal[s] / nr
            kt = A[t] - A[r] * normal[t] / nr
            kc = c + A[r] * plane_d / nr
        else:
            ks, kt, kc = A[s], A[t], c
        # in pixels, as functions of U, V
        rows.append((ks * xv[s] * size, kt * yv[t] * size, kc * size))
    (al, be, ga), (de, ep, ze) = rows
    # Q3: s = (cos U + k sin V)/sx + ox ;  t = (-k sin U + cos V)/sy + oy
    cands = []
    for th in (math.atan2(k * be, al), math.atan2(-k * de, ep)):
        for add in (0.0, math.pi):
            cands.append(th + add)
    best = None
    for th in cands:
        cs, sn = math.cos(th), math.sin(th)
        inv_sx = al * cs + k * be * sn
        inv_sy = -k * de * sn + ep * cs
        if abs(inv_sx) < 1e-12 or abs(inv_sy) < 1e-12:
            continue
        err = (abs(cs * inv_sx - al) + abs(k * sn * inv_sx - be) +
               abs(-k * sn * inv_sy - de) + abs(cs * inv_sy - ep))
        # prefer positive scales on ties
        key = (round(err, 9), inv_sx < 0, inv_sy < 0)
        if best is None or key < best[0]:
            best = (key, th, 1.0 / inv_sx, 1.0 / inv_sy, err)
    if best is None:
        return (0.0, 0.0, 0.0, 0.25, 0.25), 1e9
    _, th, sx, sy, err = best
    rot = math.degrees(th) % 360.0
    if abs(rot - round(rot)) < 1e-4:
        rot = float(round(rot)) % 360.0
    ox = ga % tex_w if tex_w else ga
    oy = ze % tex_h if tex_h else ze
    # residual expressed in texels across a 1024-unit face
    return (ox, oy, rot, sx, sy), err * 1024.0


# ===========================================================================
# parsing
# ===========================================================================
class Face:
    __slots__ = ("pts", "mat", "size", "shift", "rot", "cross")


class Brush:
    __slots__ = ("faces", "contents", "src")

    def __init__(self):
        self.faces, self.contents, self.src = [], set(), ""


class Patch:
    __slots__ = ("kind", "mat", "contents", "w", "h", "ctrl", "src")

    def __init__(self):
        self.kind, self.mat, self.contents, self.w, self.h, self.ctrl, self.src = None, "", set(), 0, 0, [], ""


class Entity:
    __slots__ = ("keys", "brushes", "patches")

    def __init__(self):
        self.keys, self.brushes, self.patches = OrderedDict(), [], []


_FACE_RE = re.compile(r'^\(\s*([^)]+)\)\s*\(\s*([^)]+)\)\s*\(\s*([^)]+)\)\s*(.*)$')
_KV_RE = re.compile(r'^"([^"]*)"\s+"([^"]*)"\s*$')


def _contents_line(s, target):
    # "contents detail;" / "contents nonColliding;" / "toolFlags splitGeo;" / "toolFlags;"
    body = s.split(None, 1)
    if len(body) == 2:
        for w in body[1].replace(";", " ").split():
            target.add(w.lower())


def parse_iwmap(text, src=""):
    lines = text.splitlines()
    n = len(lines)
    i = 0
    ents = []

    def nxt():
        nonlocal i
        while i < n:
            s = lines[i].strip()
            i += 1
            if s and not s.startswith("//"):
                return s
        return None

    while True:
        s = nxt()
        if s is None:
            break
        if s != "{":
            continue          # header "iwmap 4", layer table lines
        ent = Entity()
        while True:
            s = nxt()
            if s is None or s == "}":
                break
            m = _KV_RE.match(s)
            if m:
                ent.keys[m.group(1)] = m.group(2)
                continue
            if s == "{":
                parse_brush_or_patch(nxt, ent, src)
                continue
            # entity-level "layer ..." etc: ignore
        ents.append(ent)
    return ents


def parse_brush_or_patch(nxt, ent, src):
    contents = set()
    faces = []
    while True:
        s = nxt()
        if s is None:
            return
        if s == "}":
            break
        if s in ("mesh", "curve"):
            p = parse_patch(nxt, s, src)
            if p:
                p.contents |= contents
                ent.patches.append(p)
            # brush close
            while True:
                t = nxt()
                if t is None or t == "}":
                    return
        if s.startswith("contents") or s.startswith("toolFlags"):
            _contents_line(s, contents)
            continue
        if s.startswith("layer ") or s.startswith("smoothing"):
            continue
        if s.startswith("physics_"):
            # physics_box / physics_cylinder: editor-only primitives, skip block
            depth = 0
            while True:
                t = nxt()
                if t is None:
                    return
                if t == "{":
                    depth += 1
                elif t == "}":
                    if depth == 0:
                        return
                    depth -= 1
        m = _FACE_RE.match(s)
        if m:
            f = Face()
            f.pts = tuple(tuple(float(x) for x in m.group(g).split()) for g in (1, 2, 3))
            tok = m.group(4).split()
            f.mat = tok[0].lower()
            f.size = (float(tok[1]), float(tok[2]))
            f.shift = (float(tok[3]), float(tok[4]))
            f.rot = float(tok[5])
            f.cross = float(tok[6]) if len(tok) > 6 else 0.0
            faces.append(f)
    if faces:
        b = Brush()
        b.faces, b.contents, b.src = faces, contents, src
        ent.brushes.append(b)


def parse_patch(nxt, kind, src):
    if nxt() != "{":
        return None
    p = Patch()
    p.kind, p.src = kind, src
    s = nxt()
    while s.startswith("contents") or s.startswith("toolFlags") or s.startswith("layer "):
        if not s.startswith("layer "):
            _contents_line(s, p.contents)
        s = nxt()
    p.mat = s.split()[0].lower()
    nxt()                                   # lightmap material
    s = nxt()
    if s.startswith("smoothing"):
        s = nxt()
    dims = s.split()
    p.w, p.h = int(dims[0]), int(dims[1])
    for _ in range(p.w):
        s = nxt()                           # "("
        col = []
        for _ in range(p.h):
            tok = nxt().split()
            # v x y z [c b g r a] t s t ls lt [f e]
            xyz = (float(tok[1]), float(tok[2]), float(tok[3]))
            j = 4
            rgba = None
            if tok[j] == "c":
                b_, g_, r_, a_ = (int(x) for x in tok[j + 1:j + 5])
                rgba = (r_, g_, b_, a_)
                j += 5
            st = (float(tok[j + 1]) / 1024.0, float(tok[j + 2]) / 1024.0)
            col.append((xyz, st, rgba))
        nxt()                               # ")"
        p.ctrl.append(col)
    nxt()                                   # mesh/curve "}"
    return p


# ===========================================================================
# assets: material info + image sizes
# ===========================================================================
class Assets:
    def __init__(self, root):
        self.root = root
        self.mat_cache = {}
        self.img_cache = {}
        self.mdir = os.path.join(root, "materials") if root else None
        self.idir = os.path.join(root, "images") if root else None
        self._mindex = {}
        if self.mdir and os.path.isdir(self.mdir):
            for f in os.listdir(self.mdir):
                self._mindex[f.lower()] = os.path.join(self.mdir, f)
                if "@" in f:
                    self._mindex.setdefault(f.split("@", 1)[1].lower(), os.path.join(self.mdir, f))

    def material(self, name):
        if name in self.mat_cache:
            return self.mat_cache[name]
        info = None
        path = self._mindex.get(name.lower())
        if path and read_material:
            try:
                info = read_material(open(path, "rb").read())
            except Exception as e:     # corrupt / unknown version
                info = {"error": str(e)}
        self.mat_cache[name] = info
        return info

    def image_size(self, image):
        if image in self.img_cache:
            return self.img_cache[image]
        size = None
        if self.idir:
            p = os.path.join(self.idir, image + ".iwi")
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    hd = fh.read(28)
                if hd[:3] == b"IWi":
                    w, h = struct.unpack_from("<2h", hd, 6)
                    size = (w, h)
        self.img_cache[image] = size
        return size

    def color_map(self, name):
        info = self.material(name)
        if not info or "textures" not in info:
            return None
        for tx in info["textures"]:
            if tx["slot"].lower() == "colormap" and tx["image"]:
                return tx["image"]
        for tx in info["textures"]:
            if tx["image"]:
                return tx["image"]
        return None

    def tex_size(self, name):
        img = self.color_map(name)
        size = self.image_size(img) if img else None
        return size


# ===========================================================================
# conversion
# ===========================================================================
def safe_name(mat):
    return re.sub(r"[^a-z0-9_/\-]", "_", mat.lower())


class Converter:
    def __init__(self, prefab_root, assets, class_map=None, keep_models=None, drop_models=None,
                 model_offsets=None, md3_dir=None, prop_budget=PROP_VERTEX_BUDGET,
                 scale=DEFAULT_SCALE, collision=None, use_model_static=True):
        self.scale = scale
        self.use_model_static = use_model_static
        self.static_cache = {}
        self.static_lod_files = 0
        self.auto_detail = True
        self.fog = None                 # None = read CoD's setExpFog, False = no fog
        self.vision = None              # CoD's colour grading, read at write time
        self.vision_path = ""
        self.vision_names = []
        self.light_stats = []           # (radius, intensity) per light entity
        self.clutter_radius = self.CLUTTER_RADIUS
        self.fog_max = self.FOG_MAX_OPAQUE
        self.model_radius = {}
        self.collision = collision or {}
        self.model_offsets = model_offsets or {}
        self.md3_dir = md3_dir
        self.map_name = "map"
        self.map_tag = map_tag(self.map_name)
        self.prop_budget = prop_budget
        self.budget_report = []
        self.prefab_root = prefab_root
        self.assets = assets
        self.class_map = dict(CLASS_MAP)
        if class_map:
            self.class_map.update(class_map)
        self.drop_models = [re.compile(x, re.I) for x in (drop_models or [])]
        self.prefab_index = {}
        if prefab_root:
            for d, _, fs in os.walk(os.path.join(prefab_root, "prefabs")):
                for f in fs:
                    full = os.path.join(d, f)
                    rel = os.path.relpath(full, prefab_root).replace("\\", "/").lower()
                    self.prefab_index[rel] = full
        self.prefab_cache = {}
        self.stats = Counter()
        self.warn = Counter()
        self.missing_prefabs = set()
        self.mat_use = Counter()       # (material, variant) -> faces/patches
        self.patch_recs = []           # every world patch, for find_layers / find_overlays
        self.vis_faces = []            # visible brush faces (mat, normal, dist, bounds, brush id)
        self.overlay_mats = set()      # materials of flat patches lying on a wall -> polygonOffset
        self.layer_sort = {}           # material -> CoD layer depth (blend layers)
        self.blend_dir = None          # where the blend-layer .lwo goes (models/cod4/blend)
        self.blend_entity = None
        self.piece_group = 0
        self._cur_patch = None
        self.models = Counter()
        self.class_drop = Counter()
        self.class_out = Counter()
        self.tex_resid = []
        self.world_brushes = []        # list of text blocks
        self.world_patches = []
        self.out_entities = []         # list of text blocks
        self.worldspawn_keys = OrderedDict()
        self.min_vertex_light = self.MIN_VERTEX_LIGHT
        self.occluders = False
        self.sky_brushes = []
        self.solid_brushes = []
        self.far_shaders = set()
        self.pending_clip = []
        self.dropped_props = set()      # (model path, origin text) of props left out        # (planes, (mins, maxs)) of structural opaque brushes          # (text, (mins, maxs)) of sky-only brushes
        self.bmin = [1e18] * 3         # bounds of real geometry/placements
        self.bmax = [-1e18] * 3

    def grow(self, p):
        for k in range(3):
            if p[k] < self.bmin[k]:
                self.bmin[k] = p[k]
            if p[k] > self.bmax[k]:
                self.bmax[k] = p[k]

    # ---------------------------------------------------------------- prefabs
    def load_prefab(self, rel):
        key = rel.replace("\\", "/").lower()
        if key in self.prefab_cache:
            return self.prefab_cache[key]
        path = self.prefab_index.get(key)
        ents = None
        if path:
            with open(path, "r", errors="ignore") as fh:
                ents = parse_iwmap(fh.read(), rel)
        else:
            self.missing_prefabs.add(rel)
        self.prefab_cache[key] = ents
        return ents

    def walk(self, ents, xf, depth=0, group_ctx=None):
        for ent in ents:
            cls = ent.keys.get("classname", "")
            if cls == "worldspawn":
                if depth == 0:
                    self.worldspawn_keys = ent.keys
                self.emit_geometry(ent, xf, None)
                continue
            if cls == "misc_prefab":
                rel = ent.keys.get("model", "")
                sub = self.load_prefab(rel)
                if sub is None:
                    continue
                self.stats["prefab instances"] += 1
                if depth > 16:
                    self.warn["prefab recursion > 16 levels (cut)"] += 1
                    continue
                self.walk(sub, entity_xform(ent.keys).then(xf), depth + 1)
                continue
            self.convert_entity(ent, xf)

    # ------------------------------------------------------------- geometry
    def material_out(self, mat, nonsolid=False):
        """returns .map texture name or None (drop)."""
        if mat in TOOL_MAP:
            return TOOL_MAP[mat]
        variant = "_nonsolid" if nonsolid else ""
        self.mat_use[(mat, variant)] += 1
        return world_shader(self.map_tag, mat) + variant

    def emit_geometry(self, ent, xf, func_group_lines):
        sink_b = self.world_brushes if func_group_lines is None else func_group_lines
        for b in ent.brushes:
            txt = self.brush_text(b, xf)
            if txt:
                sink_b.append(txt)
        for p in ent.patches:
            self._cur_patch = None
            txts = self.patch_texts(p, xf)
            if self._cur_patch is not None and func_group_lines is None and txts:
                self._cur_patch["idx"] = list(range(len(sink_b), len(sink_b) + len(txts)))
                self.patch_recs.append(self._cur_patch)
            sink_b.extend(txts)

    def brush_text(self, b, xf):
        if all(TOOL_MAP.get(f.mat, "x") is None for f in b.faces):
            self.stats["brushes dropped (tool-only)"] += 1
            return None
        nonsolid = "noncolliding" in b.contents
        detail = "detail" in b.contents or nonsolid
        flags = CONTENTS_DETAIL if detail else 0
        lines = ["{"]
        planes = []
        outs = []
        vis = []
        for f in b.faces:
            # transform plane points; keep winding (uniform positive scale)
            pts = [xf.point(p) for p in f.pts] if not xf.is_identity() else list(f.pts)
            n_src = v_norm(v_cross(v_sub(f.pts[0], f.pts[1]), v_sub(f.pts[2], f.pts[1])))
            n_dst = v_norm(v_cross(v_sub(pts[0], pts[1]), v_sub(pts[2], pts[1])))
            d_dst = v_dot(n_dst, pts[1])
            planes.append((n_dst, d_dst))
            mat = f.mat
            out = TOOL_MAP.get(mat, "keep")
            if out is None:
                # meaningless tool face on a brush that otherwise survives:
                # keep the brush closed with an invisible face
                out = "tools/_nodraw" if nonsolid else "tools/_caulk"
            if out == "keep":
                out = self.material_out(mat, nonsolid)
                size = self.assets.tex_size(mat) or (512, 512)
                if not self.assets.tex_size(mat):
                    self.warn["material without colorMap size (used 512): " + mat] += 1
            else:
                size = (64, 64)
            outs.append(out)
            if not out.startswith("tools/") and out != SKIP_SHADER:
                vis.append((mat, n_dst, d_dst))
            aff = cod_face_affine(n_src, f.size, f.shift, f.rot, f.cross)
            if not xf.is_identity():
                aff = transform_affine(aff, xf)
            td, resid = solve_q3_texdef(n_dst, d_dst, aff, size[0], size[1])
            if resid > 0.5:
                self.stats["faces with sheared texture (best fit)"] += 1
            self.stats["brush faces"] += 1
            lines.append("( {} ) ( {} ) ( {} ) {} {:.6g} {:.6g} {:.6g} {:.8g} {:.8g} %FLAGS% 0 0".format(
                fmt3(pts[0]), fmt3(pts[1]), fmt3(pts[2]), out,
                td[0], td[1], td[2], td[3], td[4]))
        lines.append("}")
        self.stats["brushes"] += 1
        bb = brush_bounds(planes)
        if bb:
            bid = len(self.vis_faces)
            for mat, n, d in vis:
                self.vis_faces.append((mat, n, d, bb, bid))
        if not detail and self.auto_detail and self.small_brush(planes, bb, outs):
            detail = True
            flags = CONTENTS_DETAIL
            self.stats["brushes made detail (small / trim, for vis)"] += 1
        txt = "\n".join(lines).replace("%FLAGS%", str(flags))
        # structural + opaque (textures or caulk): q3map2 makes the space
        # inside it a solid leaf with no vis cluster - a model_static whose
        # origin is in there is never drawn (see apply_prop_budget)
        if bb and not detail and all(o == "tools/_caulk" or not (o.startswith("tools/") or o == SKIP_SHADER) for o in outs):
            self.solid_brushes.append((planes, bb))
        sky_only = all(f.mat.startswith("sky") for f in b.faces)
        if bb:
            if sky_only:
                self.sky_brushes.append((txt, bb))
            elif not all(TOOL_MAP.get(f.mat, "x") is not None and f.mat != "caulk" for f in b.faces):
                self.grow(bb[0])
                self.grow(bb[1])
        return txt

    # CoD has no Quake-style vis, so its mappers never marked anything detail:
    # mp_strike came out with 28,794 vis portals and a full -vis ran for
    # ~3 days. Structural brushes should be the big occluders only (walls,
    # floors, roofs); trim, steps, frames, pipes and other small or bevelled
    # pieces become detail. The map can't leak because of it: fit_sky_hull
    # wraps everything in a sealed sky box.
    DETAIL_MIN_SPAN = 64.0         # 2nd-largest extent below this -> detail
    DETAIL_MIN_SPAN_ANGLED = 192.0 # same for brushes with non-axial planes

    def small_brush(self, planes, bb, outs):
        if not bb or any(o in ("tools/_hint", SKIP_SHADER) for o in outs):
            return False
        ext = sorted(bb[1][k] - bb[0][k] for k in range(3))
        axial = all(max(abs(c) for c in n) > 0.999 for n, _ in planes)
        return ext[1] < (self.DETAIL_MIN_SPAN if axial else self.DETAIL_MIN_SPAN_ANGLED)

    # CoD terrain/ground layering: several patches share one vertex grid
    # (me_asphalt_road1 + ch_asphaltcracks01_dec + me_decal_papers +
    # me_trash01 on mp_strike), each upper layer faded in by its vertex alpha;
    # a lone patch with vertex alpha is a soft-edged decal on the brushes
    # under it. patchDef2 has no vertex colour (q3map2 sets 255), so drawn as
    # patches the layers are opaque and z-fight. They go into one .lwo
    # misc_model instead (RGBA vertex map -> vertex alpha in the .bsp) with
    # <shader>_blend shaders: alphaGen vertex, polygonOffset, sorted by layer.
    # The bottom layer of a stack stays a normal patch.
    LAYER_TESS = 4                 # curve patches: steps per bezier segment

    def find_layers(self):
        groups = defaultdict(list)
        for r in self.patch_recs:
            groups[r["key"]].append(r)
        def blended(mat):
            info = self.assets.material(mat) or {}
            return info.get("srcBlend") not in (None, "GL_ONE") or info.get("dstBlend") not in (None, "GL_ZERO")

        # which material is the ground: one that is never faded in (vertex
        # alpha always 255 where it is stacked) and, per its CoD material,
        # not a blended/decal one
        seen, faded = Counter(), Counter()
        for rs in groups.values():
            if len(rs) > 1:
                for r in rs:
                    seen[r["mat"]] += 1
                    faded[r["mat"]] += r["alpha"] < 255

        def ground_score(r):
            return (blended(r["mat"]), faded[r["mat"]] / max(1, seen[r["mat"]]), r["alpha"] < 255)

        def can_fade(mat):
            # multiply decals (blend ZERO/SRC_COLOR) have no alpha to fade with,
            # water and sky have their own shaders: those stay patches
            info = self.assets.material(mat) or {}
            tech = (info.get("techset") or "").lower()
            return not (info.get("srcBlend") in ("GL_ZERO", "GL_DST_COLOR") or
                        tech.startswith("water") or tech == "sky")

        layers = []
        grounds = []                   # bottom patch of a stack that got layers
        lone_opaque = 0
        for key, rs in groups.items():
            if len(rs) == 1:
                # a lone patch with vertex alpha: a soft-edged decal if its CoD
                # material blends; an opaque material ignores the alpha (it is
                # the ground itself - fading it would open holes)
                if rs[0]["alpha"] < 255:
                    if blended(rs[0]["mat"]) and can_fade(rs[0]["mat"]):
                        layers.append((rs[0], 1))
                    else:
                        lone_opaque += 1
                continue
            base = min(rs, key=ground_score)          # min() keeps file order on ties
            depth = 1
            for r in rs:
                if r is base or r["mat"] == base["mat"] or not can_fade(r["mat"]):
                    continue                          # duplicates of the ground stay patches
                layers.append((r, depth))
                depth += 1
            if depth > 1:
                grounds.append(base)
        if lone_opaque:
            self.stats["patches with vertex alpha on an opaque material (alpha ignored)"] += lone_opaque
        if not layers:
            return
        drop = set()
        tris = defaultdict(list)       # shader -> [((xyz, st, a) x3)]
        depths = defaultdict(Counter)
        for r, depth in layers:
            drop.update(r["idx"])
            r["layer"] = True
            depths[r["mat"]][depth] += 1
            self.mat_use[(r["mat"], "_blend")] += 1
            sh = "textures/" + world_shader(self.map_tag, r["mat"]) + "_blend"
            tris[sh].extend(patch_triangles(r["ctrl"], r["kind"] == "mesh", self.LAYER_TESS))
        for mat, c in depths.items():
            self.layer_sort[mat] = c.most_common(1)[0][0]
        # The ground under the layers goes into the model too, from the same
        # triangles: SOF2 tessellates a patch itself (dropping rows, choosing
        # its own diagonals, by distance), so layers over a patch never sit
        # exactly on it and flicker. In the model every layer lies on the
        # very same triangles. The patch stays for collision only (<shader>_clip:
        # nodraw, same footstep material); the model surface uses <shader>_bsp,
        # a copy of the lightmapped world shader (q3map2 -rename).
        for r in grounds:
            ws = world_shader(self.map_tag, r["mat"])
            tris["textures/" + ws].extend(patch_triangles(r["ctrl"], r["kind"] == "mesh", self.LAYER_TESS))
            self.mat_use[(r["mat"], "_ground")] += 1
            if r["nonsolid"]:
                drop.update(r["idx"])
                continue
            for i in r["idx"]:
                ln = self.world_brushes[i].split("\n")
                ln[3] = ws + "_clip"
                self.world_brushes[i] = "\n".join(ln)
        self.world_brushes = [t for i, t in enumerate(self.world_brushes) if i not in drop]
        ntri = sum(len(t) for t in tris.values())
        self.stats["patches drawn as blend layers (vertex alpha)"] += len(layers)
        self.stats["ground patches under layers moved into the layer model (patch kept for collision)"] += len(grounds)
        if self.blend_dir:
            os.makedirs(self.blend_dir, exist_ok=True)
            name = "%s_layers.lwo" % self.map_tag
            with open(os.path.join(self.blend_dir, name), "wb") as fh:
                fh.write(write_lwo_layers(tris))
            # spawnflags 4: forced meta - q3map2 merges the triangles into
            # surfaces under SOF2's 1000-vertex limit; no collision
            self.blend_entity = '{\n"classname" "misc_model"\n"model" "%sblend/%s"\n"origin" "0 0 0"\n"spawnflags" "4"\n}' % (
                MODEL_PREFIX, name)
        self.budget_report.append("blend layers (CoD vertex-alpha patches): %d patches, %d triangles, %d materials -> %s"
                                  % (len(layers), ntri, len(tris),
                                     "models/cod4/blend/%s_layers.lwo" % self.map_tag if self.blend_dir else "NOT written (no output models folder)"))

    # A flat patch lying in a brush face of another material (signs like
    # me_sign_clothingstore2 on mp_strike: CoD draws it on top, idTech3
    # z-fights) -> its material gets polygonOffset in cod4mat2shader.
    OVERLAY_MIN_COVER = 0.5

    def find_overlays(self):
        groups = defaultdict(list)
        for mat, n, d, bb, bid in self.vis_faces:
            groups[(round(n[0], 3), round(n[1], 3), round(n[2], 3), int(round(d * 8)))].append((mat, bb, bid))
        found = Counter()
        for r in self.patch_recs:
            if r.get("layer") or not r["plane"]:
                continue
            n, d, pb = r["plane"]
            for sg in (1, -1):
                key = (round(sg * n[0], 3), round(sg * n[1], 3), round(sg * n[2], 3), int(round(sg * d * 8)))
                faces = groups.get(key)
                if not faces:
                    continue
                ax = max(range(3), key=lambda k: abs(n[k]))
                u, v = [k for k in range(3) if k != ax]
                area = (pb[1][u] - pb[0][u]) * (pb[1][v] - pb[0][v])
                if area <= 0.01:
                    break
                cov = 0.0
                for mat, bb, _bid in faces:
                    if mat == r["mat"]:
                        continue
                    ow = min(pb[1][u], bb[1][u]) - max(pb[0][u], bb[0][u])
                    oh = min(pb[1][v], bb[1][v]) - max(pb[0][v], bb[0][v])
                    if ow > 0 and oh > 0:
                        cov += ow * oh
                if cov >= self.OVERLAY_MIN_COVER * area:
                    found[r["mat"]] += 1
                    break
        # brush faces the same way: a sign or plate brush set into a wall
        # (me_sign_zayed_street on mp_strike). Only materials that are never
        # the bigger face of such a pair: a wall that overlaps another wall
        # somewhere must not get the offset (decals on it would z-fight)
        small, big = Counter(), Counter()
        for key, fs in groups.items():
            if len(fs) < 2 or len(set(f[0] for f in fs)) < 2:
                continue
            ax = max(range(3), key=lambda k: abs(key[k]))
            u, v = [k for k in range(3) if k != ax]
            rects = sorted(((bb[0][u], bb[1][u], bb[0][v], bb[1][v]), mat, bid)
                           for mat, bb, bid in fs)
            for i, (ra, ma, ba) in enumerate(rects):
                aa = (ra[1] - ra[0]) * (ra[3] - ra[2])
                for rb, mb, bb_ in rects[i + 1:]:
                    if rb[0] >= ra[1]:
                        break
                    if ma == mb or ba == bb_:
                        continue
                    ow = min(ra[1], rb[1]) - max(ra[0], rb[0])
                    oh = min(ra[3], rb[3]) - max(ra[2], rb[2])
                    if ow <= 0 or oh <= 0:
                        continue
                    ab = (rb[1] - rb[0]) * (rb[3] - rb[2])
                    (sa, sm), (la, lm) = sorted(((aa, ma), (ab, mb)))
                    if sa > 0.9 * la or ow * oh < self.OVERLAY_MIN_COVER * sa:
                        continue
                    small[sm] += 1
                    big[lm] += 1
        total = Counter(f[0] for f in self.vis_faces)
        for m, n in small.items():
            # never the base of a pair, and an overlay in a good share of its
            # uses (me_floor_concrete overlapping itself here and there is not)
            if not big[m] and n >= 0.2 * total[m]:
                found[m] += n
        self.vis_faces = []
        self.overlay_mats = set(found)
        if found:
            self.budget_report.append("flat patches / sign brushes lying on a wall (polygonOffset): %d materials - %s"
                                      % (len(found), ", ".join("%s x%d" % kv for kv in found.most_common(10))))

    def patch_texts(self, p, xf):
        mat = p.mat
        tool = TOOL_MAP.get(mat, "keep")
        if tool is None:
            self.stats["patches dropped (tool)"] += 1
            return []
        nonsolid = "noncolliding" in p.contents
        name = tool if tool != "keep" else self.material_out(mat, nonsolid)
        ident = xf.is_identity()
        grid = [[(xf.point(v[0]) if not ident else v[0], v[1]) for v in col] for col in p.ctrl]
        if p.kind == "mesh":
            pieces, nbad = mesh_pieces(simplify_linear_grid(grid))
            if nbad:
                self.stats["mesh cells drawn as triangles (patch would warp the texture)"] += nbad
            elif len(pieces) > 1:
                self.stats["mesh patches split where SOF2 would drop a textured row (stretching)"] += 1
            grids = [linear_to_bezier(g) for g in pieces]
            self.stats["mesh patches"] += 1
        else:
            grids = [grid]
            self.stats["curve patches"] += 1
        if tool == "keep":
            wpts = [[(xf.point(v[0]) if not ident else v[0], v[1], v[2][3] if v[2] else 255) for v in col]
                    for col in p.ctrl]
            flat = [v[0] for col in wpts for v in col]
            self._cur_patch = {
                "mat": mat, "kind": p.kind, "ctrl": wpts, "nonsolid": nonsolid,
                "alpha": min(v[2] for col in wpts for v in col),
                "key": tuple(sorted((round(q[0] * 2), round(q[1] * 2), round(q[2] * 2)) for q in flat)),
                "plane": planar_patch(flat)}
        for g in grids:
            for col in g:
                for v in col:
                    self.grow(v[0])
        out = []
        for chunk in (c for g in grids for c in split_grid(g)):
            w, h = len(chunk), len(chunk[0])
            lines = ["{", "patchDef2", "{", name, "( {} {} 0 0 0 )".format(w, h), "("]
            for col in chunk:
                lines.append("( " + " ".join(
                    "( {} {:.8g} {:.8g} )".format(fmt3(v[0]), v[1][0], v[1][1]) for v in col) + " )")
            lines += [")", "}", "}"]
            out.append("\n".join(lines))
            self.stats["patchDef2 written"] += 1
        return out

    # -------------------------------------------------------------- entities
    def convert_entity(self, ent, xf):
        cls = ent.keys.get("classname", "")
        if cls in MODEL_CLASSES:
            mdl = ent.keys.get("model", "")
            if not mdl or mdl.startswith("*"):
                return
            # some prefabs name models "xmodel/<name>" (mp_derail:
            # me_telegraphpole2, me_electricbox2) - the asset is <name>
            mdl = re.sub(r"^xmodel[/\\]", "", mdl, flags=re.I)
            if any(rx.search(mdl) for rx in self.drop_models):
                self.class_drop["misc_model (dropped by --drop-model)"] += 1
                return
            exf = entity_xform(ent.keys).then(xf)
            pitch, yaw, roll = matrix_angles(exf.R)
            # xmodel2md3 re-centres big models and cuts huge ones into
            # pieces (MD3's +-512 limit); place each piece at its offset
            pieces = self.model_offsets.get(safe_name(mdl), [[safe_name(mdl), [0.0, 0.0, 0.0]]])
            self.piece_group += 1
            for piece, shift in pieces:
                origin = v_add(exf.o, v_scale(m_vec(exf.R, tuple(shift)), exf.s))
                self.grow(origin)
                lines = ['{', '"classname" "misc_model"',
                         '"model" "{}{}.md3"'.format(MODEL_PREFIX, piece),
                         '"origin" "{}"'.format(fmt3(origin)),
                         '"angles" "{:.6g} {:.6g} {:.6g}"'.format(*(round_ang(a) for a in (pitch, yaw, roll)))]
                if abs(exf.s - 1.0) > 1e-6:
                    lines.append('"modelscale" "{:.6g}"'.format(exf.s))
                if len(pieces) > 1:
                    # pieces of one big model (MD3 range): kept or left out together
                    lines.append('"_cod4piece" "{}"'.format(self.piece_group))
                lines.append("}")
                self.out_entities.append("\n".join(lines))
            self.models[safe_name(mdl)] += 1
            self.class_out["misc_model (from %s)" % cls] += 1
            # dyn_model = CoD's client-side physics clutter (bottles, fruit,
            # cardboard): players kick through it, so no clip
            coll = self.collision.get(safe_name(mdl)) if cls != "dyn_model" else None
            if coll:
                keys = set()
                for piece, shift in pieces:
                    po = v_add(exf.o, v_scale(m_vec(exf.R, tuple(shift)), exf.s))
                    keys.add((MODEL_PREFIX + piece + ".md3", fmt3(po)))
                self.pending_clip.append((exf, coll, keys))   # written by emit_prop_clip()
            return
        if cls in BRUSH_GROUP_CLASSES or (cls not in self.class_map and (ent.brushes or ent.patches)
                                          and cls not in ("trigger_hurt",) and not cls.startswith("trigger_")):
            if not (ent.brushes or ent.patches):
                return
            # straight into worldspawn: q3map2 merges func_group into the
            # world anyway, and each one is an entity (MAX_MAP_ENTITIES 4096 -
            # mp_backlot has 1136 of them after prefab expansion)
            before = len(self.world_brushes)
            self.emit_geometry(ent, xf, None)
            if len(self.world_brushes) > before:
                self.class_out["worldspawn brushes (from %s)" % cls] += 1
            return
        if cls == "trigger_hurt":
            blocks = []
            self.emit_geometry(ent, xf, blocks)
            if blocks:
                dmg = ent.keys.get("dmg", "1000")
                self.out_entities.append('{\n"classname" "trigger_hurt"\n"dmg" "%s"\n%s\n}' % (dmg, "\n".join(blocks)))
                self.class_out["trigger_hurt"] += 1
            return
        if cls == "light":
            import cod4_vision
            exf = entity_xform(ent.keys).then(xf)
            radius = float(ent.keys.get("radius", "250") or 250)
            inten = float(ent.keys.get("intensity", "1") or 1)
            col = ent.keys.get("_color", "1 1 1")
            value, fade = cod4_vision.point_light(radius, inten, exf.s)
            # spawnflags 1 is q3map2's linear falloff, which has CoD's shape:
            # it dies at a radius instead of running to infinity. On a Quake 3
            # game that flag also drops the Lambert term, and "_anglescale"
            # "1" puts it back (light.cpp reads it after the spawnflags).
            self.out_entities.append(
                '{\n"classname" "light"\n"origin" "%s"\n"light" "%g"\n"fade" "%g"\n'
                '"spawnflags" "1"\n"_anglescale" "1"\n"_color" "%s"\n}'
                % (fmt3(exf.o), value, fade, col))
            self.class_out["light"] += 1
            self.light_stats.append((radius * exf.s, inten))
            return
        if cls in self.class_map:
            exf = entity_xform(ent.keys).then(xf)
            _, yaw, _ = matrix_angles(exf.R)
            o = exf.o
            if self.class_map[cls].startswith("info_player_"):
                # CoD spawn origins sit on the floor; SOF2 spawns the player
                # box centred on the origin, so without a lift the box's
                # mins[2] of -24 starts below the floor (see SPAWN_LIFT)
                o = (o[0], o[1], o[2] + SPAWN_LIFT)
            self.out_entities.append('{\n"classname" "%s"\n"origin" "%s"\n"angle" "%g"\n}'
                                     % (self.class_map[cls], fmt3(o), round_ang(yaw)))
            self.class_out[self.class_map[cls]] += 1
            return
        self.class_drop[cls] += 1

    # ------------------------------------------------------------------- run
    def convert(self, map_path):
        self.map_name = os.path.splitext(os.path.basename(map_path))[0]
        self.map_tag = map_tag(self.map_name)
        with open(map_path, "r", errors="ignore") as fh:
            ents = parse_iwmap(fh.read(), os.path.basename(map_path))
        self.walk(ents, Xform(s=self.scale))
        self.find_layers()
        self.find_overlays()
        self.fit_sky_hull()
        self.apply_prop_budget()
        self.emit_prop_clip()
        self.merge_baked_props()
        if len(self.out_entities) + 1 > 4096:
            self.budget_report.append("STILL %d entities - q3map2 stops at 4096 (MAX_MAP_ENTITIES)"
                                      % (len(self.out_entities) + 1))
        ws = ['{', '"classname" "worldspawn"']
        ws.append('"message" "{}"'.format(os.path.splitext(os.path.basename(map_path))[0]))
        gs = self.pick_gridsize()
        if gs:
            ws.append('"gridsize" "{} {} {}"'.format(*gs))
        # q3map2 cuts the world into 1024-unit blocks before any brush; on
        # CoD's huge world boxes (mp_strike 31,600 x 25,000) that alone makes
        # thousands of extra leaves/clusters, and vis data grows with the
        # square of the cluster count. 4096 on mp_backlot: 7363 -> 5869
        # clusters, vis data 6.8 -> 4.3 MB. ("0" went too far: "Leaf with too
        # many portals".)
        ws.append('"_blocksize" "4096"')
        # Every alpha-tested and blended world surface is vertex-lit (one
        # stage, rgbGen vertex, no lightmap - sof2_shader DECAL_LIGHTING).
        # q3map2 samples vertex light at a surface's corners, and a decal
        # lying flat on a floor has its corners IN the floor plane, which
        # reads as occluded: mp_derail's me_decal_papers came out as black
        # blobs on the office floor and ch_factory_floorgrate as black bars
        # in the warehouse. _minvertexlight puts a floor under it -
        # light.cpp reads the scalar from worldspawn, multiplies it by
        # _color, and lightmaps_ydnar.cpp applies value_maximize() per
        # channel, so it only lifts what is darker and leaves lit surfaces
        # alone. It reaches the baked terrain too, which is vertex-lit now.
        self.vision = self.cod_vision(map_path)
        if self.min_vertex_light:
            # q3map2 normalises worldspawn "_color" and scales all three
            # min-light floors by it, so this is the colour of a shadow that
            # nothing else reaches. CoD's raw ambient colour is blue on
            # mp_strike ("_color" "0.5 0.5 0.7"); run through the grade's dark
            # tint it comes out warmer, and the floor should agree with the
            # light rather than fight it.
            import cod4_vision
            keys = {k: self.worldspawn_keys.get(k) for k in ("_color",)}
            ws.append('"_color" "%.3g %.3g %.3g"'
                      % tuple(cod4_vision.ambient_colour(keys, self.vision)))
            # The same floor on the lightmaps. q3map2 reads "_minlight" first
            # and lets it set all three floors, then "_minvertexlight" and
            # "_mingridlight" override their own, so writing both is safe and
            # the two agree. It matters more now that the sky light is down to
            # CoD's own sun:ambient ratio (cod4mat2shader SKY_SCALE) and
            # q3map2's radiosity is off: this floor is the last resort for a
            # shadow no light reaches.
            ws.append('"_minlight" "%g"' % self.min_vertex_light)
            ws.append('"_minvertexlight" "%g"' % self.min_vertex_light)
        if self.vision:
            src = getattr(self, "vision_path", "?")
            self.budget_report.append(
                "vision: CoD colour grading from %s - %s (SOF2 has no post-process pass, so "
                "cod4mat2shader folds the tint and desaturation into the sun and ambient "
                "colours; contrast and brightness stay in COD4TOSOF2_GAMMA)" % (
                    src if " " in src else os.path.basename(src),
                    ", ".join("%s %s" % (k.replace("r_filmtweak", "").replace("r_film", ""), v)
                              for k, v in sorted(self.vision.items()))))
        else:
            self.budget_report.append(
                "vision: no grading found (looked for %s) - not in raw\\vision\\, not in the "
                "map's fastfile, and not among the stock grades in cod4_vision.py, so the "
                "baked light stays as CoD's raw worldspawn values. On some maps those are far "
                "cooler than the image CoD shows."
                % ", ".join(getattr(self, "vision_names", None) or [self.map_name]))
        if self.light_stats:
            import cod4_vision
            rad = sorted(r for r, _ in self.light_stats)
            inten = sorted(i for _, i in self.light_stats)
            mid = cod4_vision.point_light(rad[len(rad) // 2], inten[len(inten) // 2])
            self.budget_report.append(
                "lights: %d light entities, CoD radius %g-%g, intensity %g-%g -> q3map2 linear "
                "falloff (spawnflags 1), peak %g luxels per unit of intensity at the light and "
                "nothing past the radius; the middle one is \"light\" \"%g\" \"fade\" \"%g\""
                % (len(self.light_stats), rad[0], rad[-1], inten[0], inten[-1],
                   cod4_vision.SUN_SCALE, mid[0], mid[1]))
        self.fog = self.cod_fog(map_path) if self.fog is None else self.fog
        if self.fog is None:
            seen, names = set(), []
            for p in getattr(self, "fog_searched", []):
                b = os.path.basename(p)
                if b not in seen:                   # the asset copy and the
                    seen.add(b)                     # raw copy share a name
                    names.append(b)
            self.budget_report.append(
                "fog: none - CoD does not set one for this map (no usable setExpFog in %s), so "
                "there is none here either; --fog start,half,r,g,b adds one"
                % (", ".join(names) or
                   "raw\\maps\\createart\\%s_art.gsc (not found)" % self.map_name))
            # No fog does not mean no far plane: CoD's own distancecull is
            # the distance the author chose and it still applies.
            own = self.worldspawn_keys.get("distancecull")
            try:
                if own and float(own) > 0:
                    ws.append('"distancecull" "%d"' % round(float(own) * self.scale))
                    self.budget_report.append(
                        'distancecull: %d, from the source map\'s own "distancecull" %g'
                        % (round(float(own) * self.scale), float(own)))
            except ValueError:
                pass
        if self.fog:
            # SOF2 level-wide fog ("fog" shader key, q3map2 writes it as a
            # global fog) and SOF2's "distancecull": vis drops everything past
            # it (q3map2 reads it as _farplanedist) and the view ends there
            ws.append('"fog" "textures/%s"' % world_shader(self.map_tag, "fog"))
            cull, cull_src = self.fog["cull"], "the fog distance"
            # CoD's own far plane, when the map set one, beats anything
            # derived from the fog - it is the distance the author chose.
            own = self.worldspawn_keys.get("distancecull")
            try:
                if own and float(own) > 0:
                    cull = int(round(float(own) * self.scale))
                    cull_src = 'the source map\'s own "distancecull" %g' % float(own)
            except ValueError:
                pass
            ws.append('"distancecull" "%d"' % cull)
            self.budget_report.append(
                "fog: CoD setExpFog(start %g, halfway %g, colour %.2f %.2f %.2f) from %s -> SOF2 fog "
                "opaque at %d units%s, distancecull %d from %s" % (
                    self.fog["start"], self.fog["half"], *self.fog["rgb"], self.fog["src"],
                    self.fog["opaque"],
                    " (CoD's own: %d, capped by --fog-max)" % self.fog["cod_opaque"]
                    if self.fog.get("cod_opaque", 0) > self.fog["opaque"] else "", cull, cull_src))
            # A far plane past the map's own diagonal culls nothing, so vis
            # has to carry the whole map: slower to compile and more drawn.
            # That is CoD's own number, so it stands - but say what it costs.
            span = max(1.0, math.sqrt(sum((self.bmax[k] - self.bmin[k]) ** 2 for k in range(2))))
            if cull >= span:
                self.budget_report.append(
                    "distancecull %d is past this map's %d-unit diagonal, so it culls nothing - "
                    "CoD's own fog is that thin. --fog-max pulls it in if vis needs the help"
                    % (cull, span))
        kept = []
        far = []
        area = self.play_area()
        far_lim = self.FAR_LIGHTMAP_MARGIN * self.scale
        for txt in self.world_brushes:
            if area and txt.count("(") >= 9 and not ("tools/" in txt and "cod4/" not in txt):
                pts = [tuple(float(x) for x in m.groups()) for m in _PT_RE.finditer(txt)]
                if "patchDef" in txt:
                    pts = pts[1:]                  # first "( w h 0 0 0 )" is the size line
                if pts:
                    lo, hi = area
                    bl = [min(p[k] for p in pts) for k in range(3)]
                    bh = [max(p[k] for p in pts) for k in range(3)]
                    # gap between the boxes on the worst axis
                    gap = max(max(lo[k] - bh[k], bl[k] - hi[k]) for k in range(3))
                    if gap > far_lim and "patchDef" in txt:
                        far.append(self.to_far(txt))
                        continue
                    if gap > far_lim:
                        fixed, removed = fix_brush(txt)
                        if removed:
                            self.stats["faces removed (BobToolz fix brushes)"] += removed
                        if fixed and not brush_problem(fixed):
                            far.append(self.to_far(fixed))
                        else:
                            self.warn["brush dropped before compile: invalid"] += 1
                        continue
            if "patchDef" not in txt:
                txt, removed = fix_brush(txt)
                if removed:
                    self.stats["faces removed (BobToolz fix brushes)"] += removed
                if txt is None:
                    self.warn["brush dropped before compile: no solid left after fixing"] += 1
                    continue
            why = None if "patchDef" in txt else brush_problem(txt)
            if why:
                self.warn["brush dropped before compile: " + why] += 1
            else:
                kept.append(txt)
        self.world_brushes = kept
        extra = []
        if far:
            # scenery outside the play area: 4x coarser lightmaps (q3map2
            # _lightmapscale on a func_group, merged into the world as usual)
            extra = ['{\n"classname" "func_group"\n%s\n}' % "\n".join(far)]
            self.budget_report.append("scenery outside the play area (> %g units): %d brushes/patches "
                                      "vertex-lit (%d _far shaders, no lightmaps)"
                                      % (far_lim, len(far), len(self.far_shaders)))
        if self.blend_entity:
            self.out_entities.append(self.blend_entity)
        out = ["\n".join(ws)] + self.world_brushes + ["}"] + extra + self.out_entities
        return "\n".join(out) + "\n"

    # CoD fog: setExpFog(startDist, halfwayDist, r, g, b, transitionTime) in
    # raw/maps/createart/<map>_art.gsc (or the map's own .gsc). Exponential,
    # 50% at start + halfway. Quake 3 / SOF2 fog is linear to opaque, so
    # opaque = 2 x (start + halfway) gives the same 50% point.
    FOG_CULL_FACTOR = 1.0          # distancecull = this x the opaque distance
    # 0-255, the same scale as _minlight. Enough to keep a shadowed cut-out
    # readable without lifting anything already lit; 0 turns it off.
    MIN_VERTEX_LIGHT = 24.0
    # CoD's fog distance is used as it is. This used to be capped at 8000,
    # because CoD fog is often a thin haze (mp_strike's is opaque only at
    # 17,500 SOF2 units) that culls almost nothing, and a nearer fog lets vis
    # drop the far side of the map. But that is a performance choice made on
    # the map author's behalf: it changes how far you can see and how thick
    # the air looks, which is most of what a map's distance reads like.
    # --fog-max puts the cap back for anyone who wants that trade.
    FOG_MAX_OPAQUE = 0

    def gsc_paths(self, map_path):
        """The map's art and gameplay scripts, wherever they can be found."""
        name = self.map_name
        cands = []
        if self.assets and getattr(self.assets, "root", None):
            # copied out of the map's fastfile by cod4_collect_assets.py
            ar = os.path.join(self.assets.root, "raw", "maps")
            cands += [os.path.join(ar, "createart", name + "_art.gsc"),
                      os.path.join(ar, "mp", name + ".gsc")]
        roots = [os.path.dirname(os.path.abspath(map_path))]
        if self.prefab_root:
            roots.insert(0, os.path.abspath(self.prefab_root))
        for r in roots:
            for up in (os.path.dirname(r), os.path.dirname(os.path.dirname(r))):
                cands += [os.path.join(up, "raw", "maps", "createart", name + "_art.gsc"),
                          os.path.join(up, "raw", "maps", "mp", name + ".gsc")]
        out = []
        for p in cands:
            if p not in out and os.path.exists(p):
                out.append(p)
        return out

    def cod_vision(self, map_path):
        """CoD's colour grading for this map. The map's art script names the
        vision it wants - "strike" and "mp_strike" are both real files and are
        not the same grade - so the name comes from VisionSetNaked() first and
        falls back to the map's own name. Then the .vision file itself if one
        is on disk, and otherwise cod4_vision's table of the stock grades,
        which is the usual answer because CoD4 ships them inside its shared
        fastfiles rather than under raw/."""
        import cod4_vision
        names = []
        for p in self.gsc_paths(map_path):
            try:
                names += cod4_vision.names_in_script(open(p, errors="ignore").read())
            except OSError:
                continue
        for n in [self.map_name] + names:
            if n.lower() not in [x.lower() for x in names]:
                names.append(n)
        self.vision_names = list(names)
        roots = [os.path.dirname(os.path.abspath(map_path))]
        if self.prefab_root:
            roots.insert(0, os.path.abspath(self.prefab_root))
        for n in names:
            cands = []
            if self.assets and getattr(self.assets, "root", None):
                cands.append(os.path.join(self.assets.root, "raw", "vision", n + ".vision"))
            for r in roots:
                for up in (os.path.dirname(r), os.path.dirname(os.path.dirname(r))):
                    cands.append(os.path.join(up, "raw", "vision", n + ".vision"))
            for path in cands:
                if not os.path.exists(path):
                    continue
                try:
                    out = cod4_vision.parse(open(path, errors="ignore").read())
                except OSError:
                    continue
                if out:
                    self.vision_path = path
                    return out
        for n in names:
            out = cod4_vision.stock(n)
            if out:
                self.vision_path = "the stock grade for vision %s" % n
                return out
        return None

    def cod_fog(self, map_path):
        name = self.map_name
        roots = [os.path.dirname(os.path.abspath(map_path))]
        if self.prefab_root:
            roots.insert(0, os.path.abspath(self.prefab_root))
        cands = []
        if self.assets and getattr(self.assets, "root", None):
            # copied out of the map's fastfile by cod4_collect_assets.py
            ar = os.path.join(self.assets.root, "raw", "maps")
            cands += [os.path.join(ar, "createart", name + "_art.gsc"), os.path.join(ar, "mp", name + ".gsc")]
        for r in roots:
            for up in (os.path.dirname(r), os.path.dirname(os.path.dirname(r))):
                cands += [os.path.join(up, "raw", "maps", "createart", name + "_art.gsc"),
                          os.path.join(up, "raw", "maps", "mp", name + ".gsc")]
        self.fog_searched = []
        for path in cands:
            if not os.path.exists(path) or path in self.fog_searched:
                continue
            self.fog_searched.append(path)
            text = open(path, errors="ignore").read()
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"//[^\n]*", "", text)
            # CoD4 art files usually set the fog through dvars:
            #   setdvar("scr_fog_exp_halfplane", "3759"); ...
            #   setExpFog(getdvarfloat("scr_fog_nearplane"), getdvarfloat("scr_fog_exp_halfplane"), ...);
            dvars = {k.lower(): v for k, v in re.findall(
                r'\bsetdvar\s*\(\s*"([^"]+)"\s*,\s*"?([^"\)]*)"?\s*\)', text, re.I)}
            variables = {k.lower(): v for k, v in re.findall(
                r"\b([A-Za-z_][\w.]*)\s*=\s*([0-9.+\-*/() ]+)\s*;", text)}

            def value(a):
                a = a.strip()
                m = re.fullmatch(r'getdvar(?:float|int)?\s*\(\s*"([^"]+)"\s*\)', a, re.I)
                if m:
                    a = dvars.get(m.group(1).lower(), "")
                elif a.lower() in variables:
                    a = variables[a.lower()]
                a = a.strip()
                if not a or not re.fullmatch(r"[0-9.+\-*/() ]+", a):
                    return None
                try:
                    return float(eval(a, {"__builtins__": {}}))
                except Exception:
                    return None

            for m in re.finditer(r"\bsetExpFog\s*\((.*?)\)\s*;", text, re.I | re.S):
                args = re.split(r",(?![^()]*\))", m.group(1))[:5]
                vals = [value(a) for a in args]
                if len(vals) == 5 and None not in vals and vals[1] > 0:
                    return self.make_fog(vals, os.path.basename(path))
            # no usable setExpFog call, but the fog dvars themselves are set
            keys = ("scr_fog_nearplane", "scr_fog_exp_halfplane", "scr_fog_red", "scr_fog_green", "scr_fog_blue")
            if all(k in dvars for k in keys[1:]):
                vals = [value(dvars.get(k, "0")) for k in keys]
                if None not in vals and vals[1] > 0:
                    return self.make_fog(vals, os.path.basename(path) + " (scr_fog dvars)")
        return None

    def make_fog(self, vals, src):
        start, half, r, g, b = vals
        cod = 2.0 * (start + half) * self.scale
        opaque = min(cod, self.fog_max) if self.fog_max else cod
        return {"start": start, "half": half, "rgb": (r, g, b), "src": src, "cod_opaque": int(round(cod)),
                "opaque": int(round(opaque)), "cull": int(round(opaque * self.FOG_CULL_FACTOR))}

    PLAY_MARGIN = 384.0       # CoD units around the spawn points = where players can stand

    def play_area(self):
        """Bounds of every spawn point (output units) + a margin: the space
        players actually move in. None if the map has no spawns."""
        pts = []
        for txt in self.out_entities:
            if '"classname" "info_player_' in txt:
                m = re.search(r'"origin" "([^"]+)"', txt)
                if m:
                    pts.append([float(v) for v in m.group(1).split()])
        if not pts:
            return None
        mg = self.PLAY_MARGIN * self.scale
        lo = [min(p[k] for p in pts) - mg for k in range(3)]
        hi = [max(p[k] for p in pts) + mg for k in range(3)]
        lo[2] = min(p[2] for p in pts) - 128 * self.scale
        hi[2] = max(p[2] for p in pts) + 256 * self.scale
        return lo, hi

    # idTech3 entity slots (GENTITYNUM_BITS 10): 64 are players', and the
    # game needs free ones while it runs (dropped weapons, grenades, temp
    # events) - same numbers as vmf2map.py, where 1056 model_statics on
    # de_overpass_csgo died with "G_Spawn: no free entities"
    MAX_GENTITIES = 1024
    SOF2_MAX_CLIENTS = 64
    RUNTIME_ENTITY_RESERVE = 200
    SLOTLESS = {"worldspawn", "misc_model", "func_group", "light", "_skybox", "info_null"}

    # Every model_static is an entity the server sends in each snapshot, and
    # idTech3 sends at most MAX_SNAPSHOT_ENTITIES (256) per client - players,
    # items and effects included. mp_strike had ~728 model_statics and in game
    # props like the cars simply did not show (the snapshot was full).
    MAX_STATIC_ENTITIES = 160

    def static_entity_cap(self):
        live = 0
        statics = sum(1 for t in self.out_entities if '"classname" "model_static"' in t)
        for txt in self.out_entities:
            m = re.search(r'"classname" "([^"]+)"', txt)
            if m and m.group(1) not in self.SLOTLESS:
                live += 1
        return max(0, min(self.MAX_GENTITIES - self.SOF2_MAX_CLIENTS - self.RUNTIME_ENTITY_RESERVE - live,
                          self.MAX_STATIC_ENTITIES - statics))

    SOLID_GRID = 256.0

    def seg_blocked(self, a, b):
        """a structural wall/floor between two prop origins (both lifted 16
        units off the floor). A model_static is drawn while the vis cluster
        of its ORIGIN is visible, so one combined model must not span rooms:
        props behind a wall would vanish and reappear with the other room."""
        if not hasattr(self, "_solid_index"):
            idx = defaultdict(list)
            g = self.SOLID_GRID
            for k, (planes, (mn, mx)) in enumerate(self.solid_brushes):
                for x in range(int(math.floor(mn[0] / g)), int(math.floor(mx[0] / g)) + 1):
                    for y in range(int(math.floor(mn[1] / g)), int(math.floor(mx[1] / g)) + 1):
                        idx[(x, y)].append(k)
            self._solid_index = idx
        a = (a[0], a[1], a[2] + 16)
        b = (b[0], b[1], b[2] + 16)
        g = self.SOLID_GRID
        lo = [min(a[k], b[k]) for k in range(3)]
        hi = [max(a[k], b[k]) for k in range(3)]
        seen = set()
        dirv = v_sub(b, a)
        for x in range(int(math.floor(lo[0] / g)), int(math.floor(hi[0] / g)) + 1):
            for y in range(int(math.floor(lo[1] / g)), int(math.floor(hi[1] / g)) + 1):
                for k in self._solid_index.get((x, y), ()):
                    if k in seen:
                        continue
                    seen.add(k)
                    planes, (mn, mx) = self.solid_brushes[k]
                    if any(mx[c] < lo[c] or mn[c] > hi[c] for c in range(3)):
                        continue
                    # clip the segment to the convex brush (shrunk 1 unit)
                    t0, t1 = 0.0, 1.0
                    for nn, d in planes:
                        dist = d - 1.0 - v_dot(nn, a)
                        den = v_dot(nn, dirv)
                        if abs(den) < 1e-9:
                            if dist < 0:
                                t0, t1 = 1.0, 0.0
                                break
                            continue
                        t = dist / den
                        if den > 0:
                            t1 = min(t1, t)
                        else:
                            t0 = max(t0, t)
                        if t0 >= t1:
                            break
                    if t1 - t0 > 1e-6:
                        return True
        return False

    def point_in_solid(self, p):
        for planes, (mn, mx) in self.solid_brushes:
            if all(mn[k] - 1 <= p[k] <= mx[k] + 1 for k in range(3)) and \
                    all(v_dot(n, p) < d - 1.0 for n, d in planes):
                return True
        return False

    STATIC_CELL = 512.0            # output units: small props per model_static cell
    # Every distinct model a model_static uses takes a model configstring
    # (MAX_MODELS 256, shared with brush models and whatever the game
    # registers); mp_backlot with 187 cell models + 180 prop models died in
    # game with "G_FindConfigstringIndex: overflow". Keep well under it.
    MAX_STATIC_MODELS = 240
    # Big groups: each model_static is an entity, and with ~650 of them
    # (mp_strike) a player had up to 645 in view - far over the snapshot's 256.
    # Almost every prop can share a group; the md3 range (+-511 around the
    # group centre) is what bounds a group.
    CELL_PROP_RADIUS = 480.0       # placed radius up to which a prop may join a group
    GROUP_EXTENT = 1000.0          # max size of a group's geometry box (md3 range is +-511)
    MD3_MAX_SURFACES = 32          # idTech3 md3 limit
    SURF_MAX_VERTS = 999           # SOF2 SHADER_MAX_VERTEXES 1000 (">=" check)
    SURF_MAX_TRIS = 1990           # SHADER_MAX_INDEXES 6000 = 2000 triangles

    def static_cell(self, key, members, surfs_of, cap):
        """Small props of one cell -> combined model_static .md3 file(s), all
        geometry in world orientation/scale (model_static ignores
        modelscale), origin at the cell geometry's centre so SOF2's PVS test
        (origin cluster) sees it. Returns ([entity text], None) or ([], why)."""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from xmodel2md3 import write_md3
        def gather(level):
            """shader -> [(world verts, tris)] per member, at a LOD level."""
            world = defaultdict(list)
            lo, hi = [1e18] * 3, [-1e18] * 3
            for j, mdl, o, R, sc in members:
                # small props leave the lower-detail versions entirely (CoD
                # fades them out at distance too): a grass clump or bottle is
                # a few pixels by the time a combined model switches
                if level and surfs_of(mdl)[1] * sc < self.LOD_DROP_RADIUS[level]:
                    continue
                src = mdl if level == 0 else self.cod_lod_name(mdl, self.MD3_LOD_SOURCE[level])
                sf = surfs_of(src)[0] or surfs_of(mdl)[0]
                for shader, vs, tris in sf:
                    w = [(v_add(o, v_scale(m_vec(R, p), sc)), m_vec(R, n), uv) for p, n, uv in vs]
                    for p, _, _ in w:
                        for k in range(3):
                            lo[k] = min(lo[k], p[k])
                            hi[k] = max(hi[k], p[k])
                    world[shader].append((w, tris))
            return world, lo, hi

        world, lo, hi = gather(0)
        centre = [(lo[k] + hi[k]) * 0.5 for k in range(3)]
        # origin candidates: the box centre, points above/below it, then the
        # members' own origins nearest the centre - first one in open space
        # (SOF2 draws a model_static only while its origin cluster is
        # visible) that keeps all geometry inside the md3 range
        # members' own origins first (raised off the floor): they are in the
        # room the props are in; the box centre may be behind a wall
        cands = sorted(([m[2][0], m[2][1], m[2][2] + 16] for m in members),
                       key=lambda p: sum((p[k] - centre[k]) ** 2 for k in range(3)))[:16]
        cands += [centre] + [[centre[0], centre[1], centre[2] + dz] for dz in (64, -64, 128, -128, 256)]
        org = None
        for c in cands:
            o = tuple(round(v * 64) / 64.0 for v in c)
            if any(max(hi[k] - o[k], o[k] - lo[k]) > 511.0 for k in range(3)):
                continue
            if not self.point_in_solid(o):
                org = o
                break
        if org is None:
            return [], ("cell too large for one md3"
                        if any(hi[k] - lo[k] > 1022.0 for k in range(3)) else "cell centre inside a wall")
        def make_chunks(world):
            chunks = []                           # (shader, verts, tris), each within the limits
            for shader, parts in world.items():
                cv, ct = [], []
                for w, tris in parts:
                    if cv and (len(cv) + len(w) > self.SURF_MAX_VERTS or len(ct) + len(tris) > self.SURF_MAX_TRIS):
                        chunks.append((shader, cv, ct))
                        cv, ct = [], []
                    if len(w) > self.SURF_MAX_VERTS or len(tris) > self.SURF_MAX_TRIS:
                        return None
                    base = len(cv)
                    cv.extend((v_sub(p, org), n, uv) for p, n, uv in w)
                    ct.extend((a + base, b + base, c + base) for a, b, c in tris)
                if cv:
                    chunks.append((shader, cv, ct))
            return chunks

        chunks = make_chunks(world)
        if chunks is None:
            return [], "a member surface exceeds the per-surface limits"
        files = [chunks[i:i + self.MD3_MAX_SURFACES] for i in range(0, len(chunks), self.MD3_MAX_SURFACES)]
        if len(files) > cap:
            return [], "entity slots ran out"
        # lower-detail versions (<name>_1/_2.md3): the same shaders in the
        # same file as the full model, so each entity's LODs show what it does
        file_of = {}
        for fi, group in enumerate(files):
            for shader, _, _ in group:
                file_of.setdefault(shader, fi)
        lod_files = {}
        for q in self.MD3_LOD_LEVELS:
            lw, llo, lhi = gather(q)
            lch = make_chunks(lw) if not lw or all(max(lhi[k] - org[k], org[k] - llo[k]) <= 511.0
                                                   for k in range(3)) else None
            per = [[] for _ in files]
            for ch in lch or []:
                per[file_of.get(ch[0], 0)].append(ch)   # a CoD LOD may use another material
            if lch is None or any(len(g) > self.MD3_MAX_SURFACES for g in per):
                per = files                       # keep full detail for this level
            lod_files[q] = per
        name = getattr(self, "map_name", "map")
        ents = []
        for fi, group in enumerate(files):
            mname = "cells/%s_%d_%d_%d_%d" % (name, key[0], key[1], key[2], fi)
            write_md3(os.path.join(self.md3_dir, mname + ".md3"), mname.rsplit("/", 1)[-1], group, (0.0, 0.0, 0.0))
            for q, per in lod_files.items():
                # nothing left at this distance: one zero-area triangle (an
                # .md3 needs a surface; this draws nothing)
                empty = [(group[0][0], [((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0))] * 3, [(0, 1, 2)])]
                write_md3(os.path.join(self.md3_dir, "%s_%d.md3" % (mname, q)), mname.rsplit("/", 1)[-1],
                          per[fi] or empty, (0.0, 0.0, 0.0))
                self.static_lod_files += 1
            ents.append('{\n"classname" "model_static"\n"model" "%s%s.md3"\n"origin" "%s"\n}'
                        % (MODEL_PREFIX, mname, fmt3(org)))
        return ents, None

    # SOF2 (like Quake 3) loads "<model>_1.md3" / "<model>_2.md3" next to an
    # .md3 and switches to them by on-screen size - found in game with
    # lod_test.py. Props were 2/3 of mp_strike's frame time at full detail.
    # Every model_static gets both files (a copy of the full model where CoD
    # has no lower LOD), so a stale _1/_2 from an older conversion can never
    # be picked up instead.
    MD3_LOD_LEVELS = (1, 2)        # Quake 3 LOD levels written (_1, _2)
    # which CoD LOD each one uses (or the lowest the model has): one step
    # lower than the level number - SOF2 switches by the whole combined
    # model's size, so later than CoD would; this makes up for it
    MD3_LOD_SOURCE = {1: 2, 2: 3}
    # leave out props smaller than this (placed radius, output units)
    CLUTTER_RADIUS = 15.0
    # props smaller than this (radius, output units) are left out of a
    # combined model's _1 / _2 version
    LOD_DROP_RADIUS = {1: 24.0, 2: 64.0}

    def cod_lod_name(self, mdl, level):
        """CoD LOD md3 for a level, falling back to the nearest higher detail."""
        for k in range(level, 0, -1):
            n = "%s_lod%d" % (mdl, k)
            if os.path.exists(os.path.join(self.md3_dir, n + ".md3")):
                return n
        return mdl

    def write_static_lods(self, mdl, name, sc, centre):
        for q in self.MD3_LOD_LEVELS:
            dst = os.path.join(self.md3_dir, "%s_%d.md3" % (name, q))
            src = self.cod_lod_name(mdl, self.MD3_LOD_SOURCE[q])
            if md3_write_static(os.path.join(self.md3_dir, src + ".md3"), dst, sc, centre) is None:
                md3_write_static(os.path.join(self.md3_dir, mdl + ".md3"), dst, sc, centre)
            self.static_lod_files += 1

    def static_entity(self, txt, mdl):
        """misc_model entity text -> (model_static entity text, None) or (None, reason)."""
        kv = dict(re.findall(r'"([^"\n]*)" "([^"\n]*)"', txt))
        try:
            o = parse_vec(kv["origin"])
            ang = parse_vec(kv.get("angles", "0 0 0"))
            sc = float(kv.get("modelscale", "1") or 1)
        except (KeyError, ValueError):
            return None, "no origin"
        tag = "" if abs(sc - self.scale) < 1e-4 else "_s%d" % round(sc * 1000)
        name = "%s_static%s" % (mdl, tag)
        key = (mdl, tag)
        if key not in self.static_cache:
            self.static_cache[key] = md3_write_static(
                os.path.join(self.md3_dir, mdl + ".md3"), os.path.join(self.md3_dir, name + ".md3"), sc)
            c = self.static_cache[key]
            if c is not None:
                self.write_static_lods(mdl, name, sc, c)
        c = self.static_cache[key]
        if c is None:
            return None, "too big for one md3 at this scale"
        R = angles_matrix(*ang)
        no = v_add(o, v_scale(m_vec(R, c), sc))
        if self.point_in_solid(no):
            return None, "centre inside a wall"
        return ('{\n"classname" "model_static"\n"model" "%s%s.md3"\n"origin" "%s"\n"angles" "%s"\n}'
                % (MODEL_PREFIX, name, fmt3(no), kv.get("angles", "0 0 0")), None)

    # q3map2 MAX_MAP_ENTITIES is 4096 and it stops with "numEntities ==
    # MAX_MAP_ENTITIES" (mp_backlot: 4334 prop placements after prefab
    # expansion - 923 shrub clumps, 587 grass clumps ...). Above this many
    # entities, small baked props are merged per cell into one .md3 each.
    ENTITY_SOFT_LIMIT = 3000
    MERGE_CELL = 512.0             # output units
    MERGE_MAX_RADIUS = 192.0       # only props this small (placed size) are merged

    # Light grid: one sample per gridsize cell over the whole world box (sky
    # hull included). mp_backlot at q3map2's default 64 64 128 had 1,043,928
    # points - several MB of the .bsp, which SOF2 must allocate in one block.
    # The grid only lights players and model_static props, so take the finest
    # standard spacing that stays under GRID_MAX_POINTS.
    GRID_MAX_POINTS = 400000
    FAR_LIGHTMAP_MARGIN = 1024.0   # CoD units beyond the play area
    FAR_LIGHTMAP_SCALE = 4.0
    GRID_STEPS = ((64, 64, 128), (80, 80, 128), (96, 96, 160), (128, 128, 192), (160, 160, 256), (192, 192, 256),
                  (256, 256, 320), (320, 320, 384), (384, 384, 512))

    def pick_gridsize(self):
        if self.bmin[0] > self.bmax[0]:
            return None
        lo = [self.bmin[k] - SKY_MARGIN for k in range(3)]
        hi = [self.bmax[k] + SKY_MARGIN for k in range(3)]
        for g in self.GRID_STEPS:
            n = 1
            for k in range(3):
                n *= int(math.ceil(hi[k] / g[k]) - math.floor(lo[k] / g[k]) + 1)
            if n <= self.GRID_MAX_POINTS:
                break
        self.budget_report.append("light grid: gridsize %d %d %d -> about %d points" % (g + (n,)))
        return g

    def merge_baked_props(self):
        """Every baked misc_model is copied into the .bsp per placement
        anyway, so N small props in one cell can be ONE misc_model of a
        combined .md3 with the same triangles in the same places: identical
        result, one entity instead of N. Vertex-lit _bsp shaders work the
        same (q3map2 -rename renames the merged model's surfaces too)."""
        if not self.md3_dir or len(self.out_entities) + 1 <= self.ENTITY_SOFT_LIMIT:
            return
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from xmodel2md3 import write_md3
        rx = re.compile(r'"model" "models/cod4/([^"]+)\.md3"')
        cache = {}
        cells = defaultdict(list)
        for i, txt in enumerate(self.out_entities):
            if '"classname" "misc_model"' not in txt:
                continue
            m = rx.search(txt)
            if not m:
                continue
            mdl = m.group(1)
            if mdl not in cache:
                surfs = read_md3_surfaces(os.path.join(self.md3_dir, mdl + ".md3"))
                rad = max((math.sqrt(v_dot(p, p)) for _, vs, _ in surfs or [] for p, _, _ in vs), default=0.0)
                cache[mdl] = (surfs, rad)
            surfs, rad = cache[mdl]
            kv = dict(re.findall(r'"([^"\n]*)" "([^"\n]*)"', txt))
            try:
                o = parse_vec(kv["origin"])
                ang = parse_vec(kv.get("angles", "0 0 0"))
                sc = float(kv.get("modelscale", "1") or 1)
            except (KeyError, ValueError):
                continue
            if not surfs or rad * sc > self.MERGE_MAX_RADIUS:
                continue
            key = tuple(int(math.floor(o[k] / self.MERGE_CELL)) for k in range(3))
            cells[key].append((i, mdl, o, angles_matrix(*ang), sc))
        name = getattr(self, "map_name", "map")
        drop = set()
        new_ents = []
        merged = 0
        for key, items in sorted(cells.items()):
            if len(items) < 2:
                continue
            centre = tuple((key[k] + 0.5) * self.MERGE_CELL for k in range(3))
            by_shader = defaultdict(lambda: ([], []))
            used = []
            for i, mdl, o, R, sc in items:
                pts = []
                for shader, vs, tris in cache[mdl][0]:
                    w = [(v_sub(v_add(o, v_scale(m_vec(R, p), sc)), centre), m_vec(R, n), uv) for p, n, uv in vs]
                    pts.append((shader, w, tris))
                if any(abs(c) > 511.0 for _, w, _ in pts for p, _, _ in w for c in p):
                    continue                   # would not fit the cell's md3 range: keep as is
                for shader, w, tris in pts:
                    verts, tl = by_shader[shader]
                    base = len(verts)
                    verts.extend(w)
                    # mirrored placements (negative det) would flip winding;
                    # angles_matrix is a pure rotation, so never here
                    tl.extend((a + base, b + base, c + base) for a, b, c in tris)
                used.append(i)
            if len(used) < 2:
                continue
            mname = "merged/%s_%d_%d_%d" % (name, key[0], key[1], key[2])
            write_md3(os.path.join(self.md3_dir, mname + ".md3"), mname.rsplit("/", 1)[-1],
                      [(sh, vt[0], vt[1]) for sh, vt in by_shader.items()], (0.0, 0.0, 0.0))
            new_ents.append('{\n"classname" "misc_model"\n"model" "%s%s.md3"\n"origin" "%s"\n}'
                            % (MODEL_PREFIX, mname, fmt3(centre)))
            drop.update(used)
            merged += len(used)
        if not drop:
            return
        before = len(self.out_entities)
        self.out_entities = [t for i, t in enumerate(self.out_entities) if i not in drop] + new_ents
        self.budget_report.append(
            "entities: %d -> %d (%d small baked props merged into %d cell models, %g units per cell)"
            % (before + 1, len(self.out_entities) + 1, merged, len(new_ents), self.MERGE_CELL))

    # Occluders. CoD's props are misc_model, and a model never blocks vis -
    # only a structural opaque brush does. On mp_derail that left 186 of 793
    # clusters visible from an average one, with every container, vehicle and
    # wall panel in the map transparent to vis. The prop collision we already
    # emit has the right shape for it: of 13,274 clip brushes, 939 are panels
    # 96+ units across, which are container and vehicle walls. Those get a
    # second brush in caulk - opaque, structural, invisible - shrunk inward so
    # it can never poke through the model it stands in.
    #
    # Deliberately NOT every clip brush: a box in open space splits the BSP
    # and adds portals, so a thousand small ones would cost more than they
    # save. Foliage is excluded outright - a tree trunk's box would occlude
    # through a canopy you can see straight through.
    OCCLUDER_MIN_MID = 96.0        # world units: 2nd-largest dimension, i.e. panel width
    OCCLUDER_MAX_MIN = 64.0        # world units: thicker than this is a blob, not a wall
    OCCLUDER_SHRINK = 4.0          # world units pulled in on every side
    OCCLUDER_SKIP = re.compile(r"foliage|tree|bush|shrub|grass|branch|canopy|"
                               r"wire|fence|rail(ing)?_|chain|cloth|tarp|flag", re.I)
    OCCLUDER_SHADER = "tools/_caulk"

    # A tree's CoD collision includes its canopy: on mp_strike the birch at
    # (3061 -604) has a 278 x 301 x 139 _shoot_wood box 67 units above its
    # origin. CoD can afford that because its canopy is opaque to the player;
    # in SOF2 the canopy is alpha-tested and you can see straight through it,
    # so a solid box there reads as an invisible wall. Keep the trunk and the
    # low branches, drop boxes whose base is above this fraction of the
    # model's height.
    FOLIAGE_CLIP_TOP = 0.45
    FOLIAGE_RE = re.compile(r"foliage|tree|bush|shrub|canopy|palm|branch", re.I)

    CLIP_MIN_SIZE = 20.0           # world units: smaller props get no clip (step height 18)
    CLIP_DETAILED_SIZE = 256.0     # world units: bigger props keep the detailed box set
    CLIP_REACH = 1024.0            # CoD units beyond the play area that still get clip

    @staticmethod
    def base_model_path(path):
        """models/cod4/x_lod2.md3 -> models/cod4/x.md3 (LOD steps rename the model)."""
        return re.sub(r"_lod\d+\.md3$", ".md3", path)

    def emit_prop_clip(self):
        """Prop collision as clip brushes. Every box is a brush with 6 planes
        and, when rotated, up to 12 edge bevels - mp_derail had 41,955 of them
        (790k BSP planes). So: ordinary props use the coarse set (<= 4 boxes),
        tiny ones none, unreachable ones (far outside the play area) none, and
        rotations within 0.01 of an axis are snapped so those boxes are axial
        (shared planes, no edge bevels)."""
        area = self.play_area()
        reach = self.CLIP_REACH * self.scale
        n_full = n_lo = n_small = n_far = 0
        n_dropped = 0
        n_occ = 0
        n_canopy = 0
        occ_models = Counter()
        for exf, coll, keys in self.pending_clip:
            if keys and keys <= self.dropped_props:
                n_dropped += 1                    # prop left out: no invisible wall
                continue
            boxes = coll["boxes"]
            lo_all = [min(b[0][k] for b in boxes) for k in range(3)]
            hi_all = [max(b[1][k] for b in boxes) for k in range(3)]
            size = max(hi_all[k] - lo_all[k] for k in range(3)) * exf.s
            if size < self.CLIP_MIN_SIZE:
                n_small += 1
                continue
            R = tuple(tuple(0.0 if abs(v) < 0.01 else (1.0 if v > 0.99 else (-1.0 if v < -0.99 else v))
                            for v in row) for row in exf.R)
            xf = Xform(R, exf.s, exf.o)
            if area:
                c = [xf.point(tuple((lo_all[k] + hi_all[k]) / 2 for k in range(3)))[k] for k in range(3)]
                lo, hi = area
                gap = max(max(lo[k] - c[k], c[k] - hi[k]) for k in range(3)) - size / 2
                if gap > reach:
                    n_far += 1
                    continue
            if size <= self.CLIP_DETAILED_SIZE and coll.get("boxes_lo"):
                boxes = coll["boxes_lo"]
                n_lo += 1
            else:
                n_full += 1
            mdl_name = ""
            for k in (keys or ()):
                mdl_name = os.path.basename(k[0])
                break
            if mdl_name and self.FOLIAGE_RE.search(mdl_name) and len(boxes) > 1:
                zlo = min(b[0][2] for b in boxes)
                zhi = max(b[1][2] for b in boxes)
                cut = zlo + (zhi - zlo) * self.FOLIAGE_CLIP_TOP
                keep = [b for b in boxes if b[0][2] <= cut]
                if keep and len(keep) < len(boxes):
                    n_canopy += len(boxes) - len(keep)
                    boxes = keep
            may_occlude = (self.occluders and mdl_name
                           and not self.OCCLUDER_SKIP.search(mdl_name))

            def occluder(mn, mx):
                """One caulk brush just inside this box, or None."""
                wd = sorted((mx[k] - mn[k]) * exf.s for k in range(3))
                if wd[1] < self.OCCLUDER_MIN_MID or wd[0] > self.OCCLUDER_MAX_MIN:
                    return None
                omn, omx = list(mn), list(mx)
                for k in range(3):
                    h = self.OCCLUDER_SHRINK / exf.s
                    if (omx[k] - omn[k]) <= 2.2 * h:        # thin panel: shrink less
                        h = (omx[k] - omn[k]) / 4.0
                    omn[k] += h
                    omx[k] -= h
                    if omx[k] - omn[k] <= 0.5:
                        return None
                return [xf.point((omx[0] if i & 1 else omn[0], omx[1] if i & 2 else omn[1],
                                  omx[2] if i & 4 else omn[2])) for i in range(8)]

            # CoD cuts a flat panel into many small boxes (a container face is
            # 24 of them), so none of them is a wall on its own. Check the
            # model's whole hull first: when that is panel-shaped, one brush
            # covers it and the per-box pass is skipped.
            whole = occluder(lo_all, hi_all) if may_occlude else None
            if whole:
                self.world_brushes.append(box_brush(whole, self.OCCLUDER_SHADER))
                n_occ += 1
                occ_models[mdl_name] += 1
                may_occlude = False
            for mn, mx in boxes:
                if may_occlude:
                    oc = occluder(mn, mx)
                    if oc:
                        self.world_brushes.append(box_brush(oc, self.OCCLUDER_SHADER))
                        n_occ += 1
                        occ_models[mdl_name] += 1
                # at least MIN_CLIP_THICK units thick in the world after scaling
                mn, mx = list(mn), list(mx)
                for k in range(3):
                    if (mx[k] - mn[k]) * exf.s < MIN_CLIP_THICK:
                        cc = (mn[k] + mx[k]) / 2
                        h = MIN_CLIP_THICK / exf.s / 2
                        mn[k], mx[k] = cc - h, cc + h
                corners = [xf.point((mx[0] if i & 1 else mn[0], mx[1] if i & 2 else mn[1],
                                     mx[2] if i & 4 else mn[2])) for i in range(8)]
                self.world_brushes.append(box_brush(corners, coll["shader"]))
                self.stats["prop clip brushes (CoD collision)"] += 1
        if n_canopy:
            self.budget_report.append(
                "foliage collision: %d canopy boxes dropped (above %.0f%% of the model's "
                "height) - SOF2 draws a canopy alpha-tested, so a solid box there is an "
                "invisible wall" % (n_canopy, self.FOLIAGE_CLIP_TOP * 100))
        if n_occ:
            self.budget_report.append(
                "vis occluders: %d caulk brushes from prop collision panels (>= %g units "
                "across, <= %g thick) - a misc_model blocks nothing on its own. Top: %s"
                % (n_occ, self.OCCLUDER_MIN_MID, self.OCCLUDER_MAX_MIN,
                   ", ".join("%s x%d" % (k, v) for k, v in occ_models.most_common(8))))
        elif self.occluders and self.pending_clip:   # pragma: no cover
            self.budget_report.append(
                "vis occluders: none - no prop collision box is a panel >= %g units across"
                % self.OCCLUDER_MIN_MID)
        if self.pending_clip:
            self.budget_report.append(
                "prop collision: %d props detailed, %d coarse (<= 4 boxes), %d too small, "
                "%d unreachable, %d left out (no clip)" % (n_full, n_lo, n_small, n_far, n_dropped))

    def apply_prop_budget(self):
        """Pick each misc_model's LOD the way CoD4 does at run time: CoD
        draws LOD i while the camera is closer than the model's lods[i].dist.
        The camera can be anywhere in the play area (spawn bounds + margin), so
        a placement gets the LOD CoD shows from the NEAREST playable point:
        props players walk past stay at full detail, vista props (forest,
        cranes, ships beyond the fence) get the LOD CoD itself draws for them.
        Only if the baked total is still over budget are placements stepped
        down further - outside the play area first, biggest saving x distance
        first - and props in the play area only as a last resort."""
        if not self.md3_dir or self.prop_budget <= 0:
            return
        import heapq
        lodinfo = {}
        lpath = os.path.join(self.md3_dir, "cod4_lods.json")
        if os.path.exists(lpath):
            try:
                lodinfo = json.load(open(lpath))
            except ValueError:
                lodinfo = {}
        rx = re.compile(r'"model" "models/cod4/([^"]+)\.md3"')
        chains = {}
        zext = {}                   # model -> vertical size of its mesh
        scale_of = {}               # entity index -> its "modelscale"
        reached_in = Counter()      # raised props the z credit brought back in
        places = []                 # [entity index, model, distance]
        area = self.play_area()
        for i, txt in enumerate(self.out_entities):
            if '"classname" "misc_model"' not in txt:
                continue
            m = rx.search(txt)
            if not m:
                continue
            mdl = m.group(1)
            if mdl not in chains:
                chain = []
                for k in range(0, 8):
                    name = mdl if k == 0 else "%s_lod%d" % (mdl, k)
                    n = md3_vertex_count(os.path.join(self.md3_dir, name + ".md3"))
                    if n is None:
                        break
                    chain.append((name, n))
                chains[mdl] = chain
                zlo, zhi = md3_zbounds(os.path.join(self.md3_dir, mdl + ".md3"))
                zext[mdl] = max(0.0, zhi - zlo)
            if not chains[mdl]:
                continue
            sc = 1.0
            ms = re.search(r'"modelscale" "([^"]+)"', txt)
            if ms:
                try:
                    sc = float(ms.group(1))
                except ValueError:
                    sc = 1.0
            scale_of[i] = sc
            d = 0.0
            o = re.search(r'"origin" "([^"]+)"', txt)
            if area and o:
                p = [float(v) for v in o.group(1).split()]
                lo, hi = area
                dx, dy, dz = (max(lo[k] - p[k], 0.0, p[k] - hi[k]) for k in range(3))
                # Vertically, measure to the model's body and not to its
                # origin. The play area is the box through the spawn points,
                # so its ceiling is only 256 units above the highest spawn and
                # anything tall or on a plinth has its ORIGIN above that:
                # mp_strike's me_statue sits at modelscale 3.125 with its
                # origin 161 units over the ceiling, so the one statue in the
                # middle of the map counted as "scenery outside the play
                # area" - stepped to the bottom of its LOD chain by distance,
                # and then deleted outright to fit the .bsp. A prop whose mesh
                # spans more than that gap reaches into the play area, so it
                # is not scenery. Horizontal distance still comes from the
                # origin, so vista props beyond the fence cull as before.
                was = math.sqrt(dx * dx + dy * dy + dz * dz)
                dz = max(0.0, dz - zext.get(mdl, 0.0) * sc)
                d = math.sqrt(dx * dx + dy * dy + dz * dz)
                if was > 0.0 and d == 0.0:
                    reached_in[mdl] += 1
            places.append([i, mdl, d])
        if reached_in:
            self.budget_report.append(
                "raised props counted as inside the play area (their mesh reaches into it, so "
                "they are not scenery): %d placements - %s" % (
                    sum(reached_in.values()),
                    ", ".join("%s x%d" % (m, n) for m, n in reached_in.most_common(10))))

        # 0. clutter: props smaller than CLUTTER_RADIUS once placed (cans,
        #    bottles, bricks, small debris) are left out entirely - in SOF2
        #    every model vertex is processed on the CPU each frame, and these
        #    hardly change the look. Their clip boxes are left out with them.
        clutter_idx, clutter = set(), Counter()
        org_of = {}
        for pl in places:
            oo = re.search(r'"origin" "([^"]+)"', self.out_entities[pl[0]])
            if oo:
                org_of[pl[0]] = parse_vec(oo.group(1))

        def supports(i, r):
            """another prop stands on/in this one (com_potted_plant02 in
            com_flower_pot02): leaving it out would leave that one floating"""
            o = org_of.get(i)
            if not o:
                return False
            for j, q in org_of.items():
                if j != i and abs(q[0] - o[0]) < r + 2 and abs(q[1] - o[1]) < r + 2 and \
                        o[2] - 1 < q[2] < o[2] + 2 * r + 8:
                    return True
            return False

        if self.clutter_radius > 0:
            keep = []
            for pl in places:
                i, mdl = pl[0], pl[1]
                if mdl not in self.model_radius:
                    self.model_radius[mdl] = md3_radius(os.path.join(self.md3_dir, mdl + ".md3"))
                sc = re.search(r'"modelscale" "([^"]+)"', self.out_entities[i])
                try:
                    sc = float(sc.group(1)) if sc else self.scale
                except ValueError:
                    sc = self.scale
                rad = self.model_radius[mdl]
                if rad is not None and rad * sc < self.clutter_radius and not supports(i, rad * sc):
                    clutter_idx.add(i)
                    clutter[mdl] += 1
                    # its clip goes with it (no invisible box left behind)
                    txt = self.out_entities[i]
                    mm = re.search(r'"model" "([^"]+)"', txt)
                    oo = re.search(r'"origin" "([^"]+)"', txt)
                    if mm and oo:
                        self.dropped_props.add((self.base_model_path(mm.group(1)), oo.group(1)))
                else:
                    keep.append(pl)
            places = keep
            if clutter:
                self.budget_report.append(
                    "clutter left out (placed radius < %g units): %d props - %s" % (
                        self.clutter_radius, sum(clutter.values()),
                        ", ".join("%s x%d" % (m, n) for m, n in clutter.most_common(12))))

        # 1. CoD's own LOD for the nearest playable point
        level = []
        for i, mdl, d in places:
            dist = lodinfo.get(mdl, [])
            k = 0
            dc = d / self.scale                       # back to CoD units
            # CoD's LOD distances are authored for the model at scale 1 and the
            # engine switches on screen size, so a model placed bigger holds
            # each LOD proportionally further out. Without the factor, the
            # 3.125x me_statue was judged as if it were a statuette.
            sc = scale_of.get(i, 1.0)
            while k + 1 < len(chains[mdl]) and k < len(dist) and dist[k] > 0 and dc >= dist[k] * sc:
                k += 1
            level.append(k)

        # 2. model_static: stored once, drawn at full detail, no .bsp space,
        #    but one entity slot each - and there are only ~730 slots
        #    (mp_backlot has 4,341 prop placements). So small props are
        #    grouped per CELL into one combined model_static: a cell of 40
        #    clumps costs one slot. Slots go to the units (a cell or one big
        #    prop) that take the most vertices out of the .bsp, heaviest
        #    first. Candidates: play-area props, and props outside that step 1
        #    put on a lower LOD (they get their full mesh back).
        statics = {}                 # placement -> model_static text ("" = member of a cell)
        new_static_ents = []
        why_not = Counter()
        n_cells = n_cell_members = 0
        if self.use_model_static:
            cap = self.static_entity_cap()
            surf_cache = {}

            def surfs_of(mdl):
                if mdl not in surf_cache:
                    sf = read_md3_surfaces(os.path.join(self.md3_dir, mdl + ".md3")) or []
                    rad = max((math.sqrt(v_dot(p, p)) for _, vs, _ in sf for p, _, _ in vs), default=0.0)
                    surf_cache[mdl] = (sf, rad)
                return surf_cache[mdl]

            used_models = set()
            state = {"cap": cap, "cells": 0, "members": 0,
                     "baked": sum(chains[places[j][1]][level[j]][1] for j in range(len(places)))}

            def allocate(cand_set, phase, stop_when_fits):
                singles, cells, pool = [], {}, []
                for j, (i, mdl, d) in enumerate(places):
                    if j not in cand_set or j in statics:
                        continue
                    if '"_cod4piece"' in self.out_entities[i]:
                        # a piece of a model too big for one md3 (minaret):
                        # stays baked into the .bsp with its other pieces - in
                        # a combined model_static it was drawn only while that
                        # model's origin (mid-air, among roof props) was visible
                        continue
                    kv = dict(re.findall(r'"([^"\n]*)" "([^"\n]*)"', self.out_entities[i]))
                    try:
                        o = parse_vec(kv["origin"])
                        ang = parse_vec(kv.get("angles", "0 0 0"))
                        sc = float(kv.get("modelscale", "1") or 1)
                    except (KeyError, ValueError):
                        continue
                    sf, rad = surfs_of(mdl)
                    if sf and rad * sc <= self.CELL_PROP_RADIUS:
                        pool.append((j, mdl, o, angles_matrix(*ang), sc, rad * sc))
                    else:
                        singles.append(j)
                # groups: grow around a seed with its nearest neighbours while the
                # combined box stays within GROUP_EXTENT. Packs dense areas far
                # tighter than a fixed grid (mp_strike: 130 grid cells used 130
                # of the ~240 model indices and still left 603 props baked).
                order = sorted(range(len(pool)), key=lambda q: (pool[q][2][0], pool[q][2][1]))
                xs = [pool[q][2][0] for q in order]
                taken = [False] * len(pool)
                import bisect
                gid = 0
                for q in order:
                    if taken[q]:
                        continue
                    j0, m0, o0, R0, s0, r0 = pool[q]
                    lo = [o0[k] - r0 for k in range(3)]
                    hi = [o0[k] + r0 for k in range(3)]
                    a = bisect.bisect_left(xs, o0[0] - self.GROUP_EXTENT)
                    b = bisect.bisect_right(xs, o0[0] + self.GROUP_EXTENT)
                    near = sorted((qq for qq in order[a:b] if not taken[qq] and qq != q),
                                  key=lambda qq: sum((pool[qq][2][k] - o0[k]) ** 2 for k in range(3)))
                    members = [pool[q]]
                    taken[q] = True
                    for qq in near:
                        oq, rq = pool[qq][2], pool[qq][5]
                        nlo = [min(lo[k], oq[k] - rq) for k in range(3)]
                        nhi = [max(hi[k], oq[k] + rq) for k in range(3)]
                        if all(nhi[k] - nlo[k] <= self.GROUP_EXTENT for k in range(3)) and \
                                not self.seg_blocked(o0, oq):
                            lo, hi = nlo, nhi
                            members.append(pool[qq])
                            taken[qq] = True
                    cells[(phase, gid, 0)] = [m[:5] for m in members]
                    gid += 1
                # Units compete for the scarce resource, a model index: a cell
                # (one index per file) or ALL single placements of one model
                # (one index for every copy). Sorted by vertices taken out of the
                # .bsp per unit, so a rare prop type no longer spends an index
                # that a cell of 40 clumps or 30 identical cars would use better.
                by_model = defaultdict(list)
                for j in singles:
                    by_model[(places[j][1], places[j][2] == 0)].append(j)
                for key, members in cells.items():
                    if len(members) == 1:
                        j = members[0][0]
                        by_model[(members[0][1], places[j][2] == 0)].append(j)
                # (priority, kind, ref, inside): play-area units weigh 4x
                units = []
                # ranked by vertices taken out of the .bsp PER ENTITY (the
                # scarcest resource now): a single car is worth as much as a
                # cell of clutter of the same weight
                for (mdl, ins), js in by_model.items():
                    units.append((chains[mdl][0][1] * (4 if ins else 1), "group", js, ins))
                for key, members in cells.items():
                    if len(members) > 1:
                        ins = any(places[m[0]][2] == 0 for m in members)
                        units.append((sum(chains[m[1]][0][1] for m in members) * (4 if ins else 1),
                                      "cell", key, ins))
                units.sort(key=lambda u: -u[0])

                def model_of(txt):
                    m = re.search(r'"model" "([^"]+)"', txt)
                    return m.group(1).lower() if m else ""

                def take_single(j):
                    """model_static for one placement, within both budgets -> reason or None."""
                    txt, why1 = self.static_entity(self.out_entities[places[j][0]], places[j][1])
                    if not txt:
                        return why1
                    mname = model_of(txt)
                    if mname not in used_models and len(used_models) >= self.MAX_STATIC_MODELS:
                        return "model index limit (%d distinct models)" % self.MAX_STATIC_MODELS
                    used_models.add(mname)
                    statics[j] = txt
                    state["baked"] -= chains[places[j][1]][level[j]][1]
                    return None

                pending = []
                import heapq as _hq
                work = [(-u[0], n, u) for n, u in enumerate(units)]
                _hq.heapify(work)
                seq = len(work)
                while work or pending:
                    while pending:
                        u = pending.pop()
                        _hq.heappush(work, (-u[0], seq, u))
                        seq += 1
                    if not work:
                        break
                    save, kind, ref, ins = _hq.heappop(work)[2]
                    # outside the play area: only while the baked props are
                    # over budget (otherwise baked = vertex-lit, finer than
                    # the light grid model_static uses)
                    if not ins and state["baked"] <= self.prop_budget:
                        continue
                    cap = state["cap"]
                    if kind == "group":
                        for j in ref:
                            if cap <= 0:
                                why_not["entity slots ran out"] += 1
                                continue
                            why = take_single(j)
                            if why:
                                why_not[why] += 1
                            else:
                                cap -= 1
                        state["cap"] = cap
                        continue
                    if cap <= 0:
                        why_not["entity slots ran out"] += len(cells[ref])
                        continue
                    room = self.MAX_STATIC_MODELS - len(used_models)
                    ents, why = self.static_cell(ref, cells[ref], surfs_of, min(cap, room))
                    if not ents and why == "entity slots ran out" and room < cap:
                        why = "model index limit (%d distinct models)" % self.MAX_STATIC_MODELS
                    if not ents and why in ("cell too large for one md3", "cell centre inside a wall") \
                            and len(cells[ref]) > 1:
                        # split in two along the longest axis and queue both halves
                        mem = cells[ref]
                        ext = [max(m[2][k] for m in mem) - min(m[2][k] for m in mem) for k in range(3)]
                        ax = ext.index(max(ext))
                        mem = sorted(mem, key=lambda m: m[2][ax])
                        half = len(mem) // 2
                        for part, sub in ((0, mem[:half]), (1, mem[half:])):
                            nk = (ref[0], ref[1] * 2 + part + 1, ref[2] + 1)
                            cells[nk] = sub
                            if len(sub) > 1:
                                sv = sum(chains[m[1]][0][1] for m in sub) * (4 if ins else 1)
                                pending.append((sv, "cell", nk, ins))
                            else:
                                pending.append((chains[sub[0][1]][0][1] * (4 if ins else 1), "group", [sub[0][0]], ins))
                        continue
                    if not ents:
                        # cell not possible: its props one by one instead
                        for m in cells[ref]:
                            if cap <= 0:
                                why_not["entity slots ran out"] += 1
                                continue
                            why1 = take_single(m[0])
                            if why1:
                                why_not[why1] += 1
                            else:
                                cap -= 1
                        state["cap"] = cap
                        continue
                    used_models.update(model_of(e) for e in ents)
                    state["cap"] = cap - len(ents)
                    new_static_ents.extend(ents)
                    for m in cells[ref]:
                        statics[m[0]] = ""
                        state["baked"] -= chains[m[1]][level[m[0]]][1]
                    state["cells"] += 1
                    state["members"] += len(cells[ref])


            # phase 1: the play area (and props CoD itself draws at a lower
            # LOD from there - they get their full mesh back)
            # One ranking for everything (mp_strike: 163k vertices of props
            # just outside the spawn box stayed baked, and a play-area-first
            # pass would have spent every model index before reaching them).
            allocate(set(range(len(places))), 0, False)
            n_cells, n_cell_members = state["cells"], state["members"]
        baked = [j for j in range(len(places)) if j not in statics]

        def verts(j):
            return chains[places[j][1]][level[j]][1]

        full = sum(chains[places[j][1]][0][1] for j in baked)
        total = sum(verts(j) for j in baked)
        by_dist = sum(1 for j in baked if level[j])

        # 2. still over budget: greedy, outside the play area first
        stepped = 0
        if total > self.prop_budget:
            heap = []

            def push(j):
                ch = chains[places[j][1]]
                if level[j] + 1 < len(ch):
                    save = ch[level[j]][1] - ch[level[j] + 1][1]
                    d = places[j][2]
                    tier = 0 if d > 0 else 1
                    heapq.heappush(heap, (tier, -save * (1.0 + d / 512.0), j))

            for j in baked:
                push(j)
            while total > self.prop_budget and heap:
                _, _, j = heapq.heappop(heap)
                before = verts(j)
                level[j] += 1
                total += verts(j) - before
                stepped += 1
                push(j)

        # 3. still over with every LOD used: thin out props that have no
        #    collision in CoD (grass/shrub clumps, leaves - nothing a player
        #    can bump into, so no clip brush is left behind). Outside the play
        #    area first; within a model, in bit-reversed order of placement,
        #    so a field gets evenly sparser (every 2nd, then every 4th ...)
        #    instead of losing one corner.
        thinned = Counter()
        removed = set()
        if total > self.prop_budget:
            def bitrev(i):
                r, b = 0, 1
                while b <= i:
                    b <<= 1
                bits = b.bit_length() - 1
                for k in range(bits):
                    if i >> k & 1:
                        r |= 1 << (bits - 1 - k)
                return r
            rank = defaultdict(int)
            count = Counter(places[j][1] for j in baked)
            cand = []
            for j in baked:
                mdl = places[j][1]
                if mdl in self.collision or not THIN_RE.search(mdl):
                    continue
                r = rank[mdl]
                rank[mdl] += 1
                cand.append(((0 if places[j][2] > 0 else 1), bitrev(r), -verts(j), j))
            # never more than half of a model's placements: a field may get
            # sparser, it must not disappear (mp_backlot lost every shrub)
            cand.sort()
            for *_, j in cand:
                if total <= self.prop_budget:
                    break
                if thinned[places[j][1]] * 2 >= count[places[j][1]]:
                    continue
                total -= verts(j)
                removed.add(j)
                thinned[places[j][1]] += 1
        if removed:
            self.budget_report.append(
                "thinned out %d vegetation placements (no collision) to fit the budget: %s" % (
                    len(removed), ", ".join("%s x%d" % (m, n) for m, n in thinned.most_common(8))))
        # 4. still over: leave out the heaviest props OUTSIDE the play area
        #    (scenery), farthest and heaviest first. Never inside the play area.
        #    Their clip goes too. Listed in the log so the map can be edited.
        left_out = Counter()
        if total > self.prop_budget:
            # pieces of one split model (me_mosque_tower02 -> _p0.._p11) go as
            # a whole: leaving some out left the minaret's crescent floating
            group_of, members = {}, defaultdict(list)
            for j in range(len(places)):
                g = re.search(r'"_cod4piece" "(\d+)"', self.out_entities[places[j][0]])
                if g:
                    group_of[j] = int(g.group(1))
                    members[int(g.group(1))].append(j)
            units, seen_g = [], set()
            baked_set = set(baked)
            for j in baked:
                if j in removed or places[j][2] <= 0:
                    continue
                g = group_of.get(j)
                if g is None:
                    units.append([j])
                elif g not in seen_g:
                    seen_g.add(g)
                    js = [q for q in members[g] if q not in removed and q in baked_set]
                    # a group is scenery only if every piece is outside the play area
                    if js and all(places[q][2] > 0 for q in members[g]):
                        units.append(js)
            units.sort(key=lambda js: -sum(verts(q) * (1.0 + places[q][2] / 1024.0) for q in js))
            for js in units:
                if total <= self.prop_budget:
                    break
                g = group_of.get(js[0])
                for q in (members[g] if g is not None else ()):
                    if q in statics and statics[q]:
                        statics[q] = ""          # a piece already made a model_static goes too
                for j in js:
                    total -= verts(j)
                    removed.add(j)
                    left_out[places[j][1]] += 1
                    txt = self.out_entities[places[j][0]]
                    mm = re.search(r'"model" "([^"]+)"', txt)
                    oo = re.search(r'"origin" "([^"]+)"', txt)
                    if mm and oo:
                        self.dropped_props.add((self.base_model_path(mm.group(1)), oo.group(1)))
            if left_out:
                self.budget_report.append(
                    "LEFT OUT %d scenery props outside the play area to fit the .bsp: %s" % (
                        sum(left_out.values()),
                        ", ".join("%s x%d" % (m, n) for m, n in left_out.most_common(20))))
        for j, txt in statics.items():
            if txt:
                self.out_entities[places[j][0]] = txt
        removed |= {j for j, txt in statics.items() if not txt}
        for j, (i, mdl, d) in enumerate(places):
            if j in statics or j in removed:
                continue
            if level[j]:
                self.out_entities[i] = self.out_entities[i].replace(
                    '"model" "models/cod4/%s.md3"' % mdl,
                    '"model" "models/cod4/%s.md3"' % chains[mdl][level[j]][0])
        if removed or clutter_idx:
            drop_idx = {places[j][0] for j in removed} | clutter_idx
            self.out_entities = [t for i, t in enumerate(self.out_entities) if i not in drop_idx]
        self.out_entities.extend(new_static_ents)
        inside = [j for j in baked if places[j][2] == 0]
        self.budget_report.append(
            "props: %d placements, %d in the play area%s" % (
                len(places), sum(1 for p in places if p[2] == 0),
                "" if area else " (no spawn points - all treated as in play)"))
        if self.use_model_static:
            self.budget_report.append(
                "model_static: %d placements of %d models (full detail, lit from the light grid) - "
                "%d of them grouped into %d cell models - %d distinct models (limit %d), "
                "%d entity slots left%s" % (
                    len(statics), len({places[j][1] for j in statics}), n_cell_members, n_cells,
                    len(used_models), self.MAX_STATIC_MODELS, self.static_entity_cap(),
                    "; kept baked: " + ", ".join("%d %s" % (n, r) for r, n in why_not.most_common())
                    if why_not else ""))
            self.budget_report.append(
                "model_static LOD: every model_static has <model>_1/_2.md3 lower-detail versions "
                "(CoD LOD 1/2; SOF2 switches by on-screen size, r_lodbias / r_lodscale)")
        self.budget_report.append("baked prop vertices: %d at full detail, %d after LOD selection (budget %d)"
                                  % (full, total, self.prop_budget))
        # compile_cod4_map.bat pairs this with the finished .bsp's size, and the
        # next convert of this map aims the budget from the two (bsp_budget.py)
        self.baked_prop_vertices = total
        heavy = Counter()
        heavy_n = Counter()
        for j in baked:
            if j in removed:
                continue
            heavy[places[j][1]] += verts(j)
            heavy_n[places[j][1]] += 1
        if heavy:
            self.budget_report.append("largest baked: " + ", ".join(
                "%s %dx = %d" % (m, heavy_n[m], v) for m, v in heavy.most_common(10)))
        self.budget_report.append(
            "baked: %d placements on a lower LOD by distance, %d more stepped down for the budget, "
            "%d play-area props below full detail"
            % (by_dist, stepped, sum(1 for j in inside if level[j])))
        if total > self.prop_budget:
            self.budget_report.append("STILL OVER BUDGET with every LOD used - compile it and the "
                                      "next Convert will re-aim from the real .bsp size; to cut it "
                                      "now, --drop-model (e.g. foliage_)")

    def fit_sky_hull(self):
        """CoD4's compiler allows a far bigger world than q3map2 (+-65536):
        mp_shipment's sky box reaches z 71006, q3map2 drops that slab, the
        box opens and the map leaks. Sky-only brushes reaching well past the
        real geometry are the outer hull; replace them with a tight sky box
        around the geometry (+ margin). Visually identical - idTech3 draws
        skyParms at infinity whatever the distance."""
        if self.bmin[0] > self.bmax[0] or not self.sky_brushes:
            return
        mins = [math.floor((self.bmin[k] - SKY_MARGIN) / 64.0) * 64.0 for k in range(3)]
        maxs = [math.ceil((self.bmax[k] + SKY_MARGIN) / 64.0) * 64.0 for k in range(3)]
        hull = [txt for txt, (mn, mx) in self.sky_brushes
                if any(mn[k] < mins[k] - 1 or mx[k] > maxs[k] + 1 for k in range(3))]
        drop = set(hull)
        before = len(self.world_brushes)
        self.world_brushes = [b for b in self.world_brushes if b not in drop]
        removed = before - len(self.world_brushes)
        # The box is added even when CoD's own hull fits: CoD maps needn't be
        # sealed the Quake way (mp_shipment leaked through its hull), and the
        # small brushes made detail for vis no longer seal anything. Overlap
        # with the original sky brushes is harmless.
        shader = world_shader(self.map_tag, "sky_cobra")
        for txt, _ in self.sky_brushes:
            # shader = the token after the THIRD ")" of a face line
            m = re.search(r"^\([^)]*\) \([^)]*\) \([^)]*\) (\S+) ", txt, re.M)
            if m:
                shader = m.group(1)
                break
        self.world_brushes += sky_box_brushes(mins, maxs, shader)
        if removed:
            self.stats["sky hull brushes replaced (oversized for q3map2)"] += removed
        else:
            self.stats["sealing sky box added around the map"] += 1
        self.sky_box = (mins, maxs)

    # Scenery outside the play area is vertex-lit through a "<shader>_far"
    # copy (q3map_nolightmap + rgbGen vertex, written by cod4mat2shader.py).
    # Lightmaps cost twice: .bsp size, and one renderer shader per
    # shader x lightmap pair - mp_strike had 1036 such pairs and hit SOF2's
    # shader limit in game.
    _FAR_RE = re.compile(r"(?<![\w/])(cod4/[A-Za-z0-9_/\-]+)")

    def to_far(self, txt):
        def sub(m):
            name = m.group(1)
            if name == SKIP_SHADER or "/sky" in name or name.endswith("_far") or name.endswith("_clip"):
                return name
            self.far_shaders.add(name)
            return name + "_far"
        return self._FAR_RE.sub(sub, txt)

    def manifest(self):
        mats = OrderedDict()
        for (mat, variant), n in sorted(self.mat_use.items()):
            info = self.assets.material(mat) or {}
            img = self.assets.color_map(mat)
            entry = mats.setdefault(mat, {
                "shader": "textures/" + world_shader(self.map_tag, mat),
                "colorMap": img,
                "size": self.assets.image_size(img) if img else None,
                "techset": info.get("techset"),
                "sortKey": info.get("sortKey"),
                "srcBlend": info.get("srcBlend"), "dstBlend": info.get("dstBlend"),
                "alphaTest": info.get("alphaTest"), "cull": info.get("cull"),
                "surfaceType": (info.get("surfaceFlags", 0) >> 20) if info else None,
                "contents": info.get("contents"),
                "textures": [(t["slot"], t["image"]) for t in info.get("textures", [])],
                "variants": {}, "found": bool(info),
                "overlay": mat in self.overlay_mats,
                "layer": self.layer_sort.get(mat),
                "ground": bool(self.mat_use.get((mat, "_ground"))),
            })
            entry["variants"][variant or "base"] = n
        sun = {k: self.worldspawn_keys.get(k) for k in
               ("suncolor", "sunlight", "sundirection", "sundiffusecolor", "ambient", "_color",
                "diffusefraction", "sunradiosity", "radiosityscale")}
        return {"map": self.map_name, "tag": self.map_tag, "fog": self.fog or None,
                "vision": getattr(self, "vision", None),
                "materials": mats, "models": dict(self.models.most_common()), "sun": sun,
                "far_shaders": sorted("textures/" + n for n in self.far_shaders)}


def md3_write_static(src, dst, scale, centre=None):
    """model_static copy of an .md3: SOF2's model_static ignores "modelscale"
    (found with the CS:S pipeline: props drew unscaled), so the scale is
    built into the vertices, and the origin moves to the frame-0 bounding-box
    centre - SOF2 draws a model_static only while its ORIGIN is in a visible
    cluster, and CoD props have theirs at the base, on or in the floor.
    Returns the centre c in source model units (the placement origin moves
    by R*scale*c), or None if it would not fit MD3's +-512 range."""
    try:
        d = bytearray(open(src, "rb").read())
    except OSError:
        return None
    if d[:4] != b"IDP3":
        return None
    nfr, ntag, nsurf = struct.unpack_from("<3i", d, 76)
    ofr, otag, osurf = struct.unpack_from("<3i", d, 92)
    if nfr < 1 or ntag:
        return None
    mins = struct.unpack_from("<3f", d, ofr)
    maxs = struct.unpack_from("<3f", d, ofr + 12)
    c = list(centre) if centre is not None else [(mins[k] + maxs[k]) * 0.5 for k in range(3)]
    so = osurf
    for _ in range(nsurf):
        if d[so:so + 4] != b"IDP3":
            return None
        sfr, nsh, nv = struct.unpack_from("<3i", d, so + 72)
        oxyz, oend = struct.unpack_from("<2i", d, so + 100)
        for v in range(sfr * nv):
            o = so + oxyz + v * 8
            xyz = struct.unpack_from("<3h", d, o)
            q = [int(round((xyz[k] / 64.0 - c[k]) * scale * 64.0)) for k in range(3)]
            if not all(-32768 <= x <= 32767 for x in q):
                return None
            struct.pack_into("<3h", d, o, *q)
        so += oend
    for f in range(nfr):
        o = ofr + f * 56
        mn = [(v - c[k]) * scale for k, v in enumerate(struct.unpack_from("<3f", d, o))]
        mx = [(v - c[k]) * scale for k, v in enumerate(struct.unpack_from("<3f", d, o + 12))]
        struct.pack_into("<3f", d, o, *mn)
        struct.pack_into("<3f", d, o + 12, *mx)
        struct.pack_into("<3f", d, o + 24, 0.0, 0.0, 0.0)
        struct.pack_into("<f", d, o + 36, max(math.sqrt(sum(v * v for v in mn)),
                                              math.sqrt(sum(v * v for v in mx))))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(d)
    return tuple(c)


def read_md3_surfaces(path):
    """.md3 frame 0 -> [(shader, [(pos, normal, uv)], [tri])] or None."""
    try:
        d = open(path, "rb").read()
    except OSError:
        return None
    if d[:4] != b"IDP3":
        return None
    ns, = struct.unpack_from("<i", d, 84)
    o, = struct.unpack_from("<i", d, 100)
    out = []
    for _ in range(ns):
        if d[o:o + 4] != b"IDP3":
            return None
        nsh, nv, nt = struct.unpack_from("<3i", d, o + 76)
        otri, osh, ost, oxyz, oend = struct.unpack_from("<5i", d, o + 88)
        shader = d[o + osh:o + osh + 64].split(b"\0")[0].decode("latin-1")
        tris = [struct.unpack_from("<3i", d, o + otri + 12 * k) for k in range(nt)]
        verts = []
        for k in range(nv):
            x, y, z, lng_b, lat_b = struct.unpack_from("<3h2B", d, o + oxyz + 8 * k)
            lat = lat_b * 2 * math.pi / 255.0
            lng = lng_b * 2 * math.pi / 255.0
            n = (math.cos(lat) * math.sin(lng), math.sin(lat) * math.sin(lng), math.cos(lng))
            uv = struct.unpack_from("<2f", d, o + ost + 8 * k)
            verts.append(((x / 64.0, y / 64.0, z / 64.0), n, uv))
        out.append((shader, verts, tris))
        o += oend
    return out


def md3_radius(path):
    """half the frame-0 bounding box diagonal of an .md3 (model units), or None."""
    try:
        with open(path, "rb") as fh:
            d = fh.read(200)
    except OSError:
        return None
    if d[:4] != b"IDP3" or len(d) < 132:
        return None
    ofr, = struct.unpack_from("<i", d, 92)
    with open(path, "rb") as fh:
        fh.seek(ofr)
        fr = fh.read(24)
    if len(fr) < 24:
        return None
    mn, mx = struct.unpack_from("<3f", fr, 0), struct.unpack_from("<3f", fr, 12)
    return math.sqrt(sum(((mx[k] - mn[k]) / 2.0) ** 2 for k in range(3)))


def md3_zbounds(path):
    """(min z, max z) of frame 0's bounding box, in output units and relative
    to the entity origin (xmodel2md3 bakes its re-centring shift into the
    mesh). (0.0, 0.0) when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            d = fh.read(256)
    except OSError:
        return 0.0, 0.0
    if d[:4] != b"IDP3":
        return 0.0, 0.0
    try:
        nframes, = struct.unpack_from("<i", d, 76)
        ofs, = struct.unpack_from("<i", d, 92)
        if nframes < 1 or ofs < 108 or ofs + 24 > len(d):
            return 0.0, 0.0
        return (struct.unpack_from("<f", d, ofs + 8)[0],      # mins[2]
                struct.unpack_from("<f", d, ofs + 20)[0])     # maxs[2]
    except struct.error:
        return 0.0, 0.0


def md3_vertex_count(path):
    try:
        with open(path, "rb") as fh:
            d = fh.read()
    except OSError:
        return None
    if d[:4] != b"IDP3":
        return None
    ns, = struct.unpack_from("<i", d, 84)
    o, = struct.unpack_from("<i", d, 100)
    n = 0
    for _ in range(ns):
        nv, = struct.unpack_from("<i", d, o + 80)
        oend, = struct.unpack_from("<i", d, o + 104)
        n += nv
        o += oend
    return n


def _redundant_line(prev, cur, nxt, eps=0.05):
    """cur lies on the straight segment prev->nxt at the same parameter in
    position AND uv - removing it leaves a linear mesh unchanged."""
    for a, b, c in zip(prev, cur, nxt):
        da = v_len(v_sub(b[0], a[0]))
        dc = v_len(v_sub(c[0], b[0]))
        if da + dc < 1e-6:
            continue
        t = da / (da + dc)
        p = v_add(a[0], v_scale(v_sub(c[0], a[0]), t))
        if v_len(v_sub(p, b[0])) > eps:
            return False
        for k in range(2):
            if abs(a[1][k] + (c[1][k] - a[1][k]) * t - b[1][k]) > 1e-3:
                return False
    return True


def _engine_culls(prev, cur, nxt):
    """SOF2's patch tessellation (and q3map2's RemoveLinearMeshColumnsRows)
    drops a row/column whose points all lie within 0.1 unit of the line
    through their neighbours - by POSITION only, its texture coordinates are
    lost (a zero-length neighbour span counts as on the line). NetRadiant
    keeps every point, so the texture looks right there and stretched in game."""
    for a, b, c in zip(prev, cur, nxt):
        d = v_sub(c[0], a[0])
        L = v_len(d)
        if L < 1e-4:
            continue
        t = v_dot(v_sub(b[0], a[0]), d) / (L * L)
        if v_len(v_sub(b[0], v_add(a[0], v_scale(d, t)))) >= 0.1:
            return False
    return True


def _uv_affine_ok(a, b, c, d, tol=0.1):
    """the texture coordinates of quad a-b-c-d (a,b,c,d around the cell) are
    one affine function of position: then a bezier/bilinear patch cell maps
    the texture exactly like CoD's two triangles do. Otherwise (a mesh bent
    round a corner, uneven texture runs) the patch warps it."""
    e1, e2 = v_sub(b[0], a[0]), v_sub(d[0], a[0])
    n = v_cross(e1, e2)
    if v_len(n) < 1e-6:
        e1, e2 = v_sub(c[0], b[0]), v_sub(c[0], d[0])
        n = v_cross(e1, e2)
        if v_len(n) < 1e-6:
            return True                    # degenerate cell, nothing to warp
    ax = max(range(3), key=lambda k: abs(n[k]))
    u, v = [k for k in range(3) if k != ax]
    # affine map (pu, pv) -> st from a, b, d; check c
    m = [[b[0][u] - a[0][u], d[0][u] - a[0][u]], [b[0][v] - a[0][v], d[0][v] - a[0][v]]]
    det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
    if abs(det) < 1e-6:
        return True
    x, y = c[0][u] - a[0][u], c[0][v] - a[0][v]
    s_ = (x * m[1][1] - y * m[0][1]) / det
    t_ = (y * m[0][0] - x * m[1][0]) / det
    for k in range(2):
        pred = a[1][k] + s_ * (b[1][k] - a[1][k]) + t_ * (d[1][k] - a[1][k])
        if abs(pred - c[1][k]) > tol:
            return False
    return True


def _nudge(p, q, r, eps=0.125):
    """the collapsed corner of a triangle patch, moved eps along the direction
    q->r (texture coordinates following): an exactly zero-length patch edge
    gives SOF2's patch collision degenerate cells ("CM_GridPlane
    unresolvable"); 1/8 unit is invisible and keeps the mapping exact"""
    e = v_sub(r[0], q[0])
    L = v_len(e)
    if L < 1e-6:
        return p
    f = eps / L
    return (v_add(p[0], v_scale(e, f)), (p[1][0] + (r[1][0] - q[1][0]) * f, p[1][1] + (r[1][1] - q[1][1]) * f))


def _tri_n(p, q, r):
    return v_cross(v_sub(q[0], p[0]), v_sub(r[0], p[0]))


def _convex_cell(a, b, c, d):
    """a-b-c-d is a proper (convex, not folded) quad: the corner turns all
    agree. A bow-tie cell (a mesh strip bent round a corner) folds over as a
    patch and smears its texture across the fold."""
    ns = [_tri_n(d, a, b), _tri_n(a, b, c), _tri_n(b, c, d), _tri_n(c, d, a)]
    ref = max(ns, key=v_len)
    if v_len(ref) < 1e-6:
        return True
    return all(v_dot(n, ref) >= -1e-6 for n in ns)


def _diag_ac(a, b, c, d):
    """split along a-c (else b-d): the diagonal whose two triangles face the
    same way, the shorter one if both do"""
    ac = v_dot(_tri_n(a, b, c), _tri_n(a, c, d)) > 0
    bd = v_dot(_tri_n(a, b, d), _tri_n(b, c, d)) > 0
    if ac != bd:
        return ac
    return v_len(v_sub(c[0], a[0])) <= v_len(v_sub(d[0], b[0]))


def mesh_pieces(g):
    """linear CoD mesh -> list of linear grids that patches draw exactly:
    cells whose texture mapping a patch would warp become two triangle
    patches each (a collapsed edge makes a patch cell an exact triangle),
    the rest strips; then split_at_culled_lines on every strip."""
    W, H = len(g), len(g[0])
    bad = set()
    for i in range(W - 1):
        for j in range(H - 1):
            q = (g[i][j], g[i + 1][j], g[i + 1][j + 1], g[i][j + 1])
            if not _uv_affine_ok(*q) or not _convex_cell(*q):
                bad.add((i, j))
    if not bad:
        return split_at_culled_lines(g), 0
    out = []
    for i in range(W - 1):
        j = 0
        while j < H - 1:
            if (i, j) in bad:
                a, b, c, d = g[i][j], g[i + 1][j], g[i + 1][j + 1], g[i][j + 1]
                # split along the shorter diagonal; each triangle as a 2x2 grid
                # whose second column is one point twice
                # (columns [P00, P01], [P10, P11]; winding P00-P10-P11-P01 as the cell's a-b-c-d)
                if _diag_ac(a, b, c, d):
                    out += [[[a, _nudge(a, b, c)], [b, c]], [[a, d], [_nudge(a, d, c), c]]]      # a-b-c, a-c-d
                else:
                    out += [[[a, d], [b, _nudge(b, a, d)]], [[d, _nudge(d, b, c)], [b, c]]]      # a-b-d, d-b-c
                j += 1
                continue
            k = j
            while k < H - 1 and (i, k) not in bad:
                k += 1
            out += split_at_culled_lines([g[i][j:k + 1], g[i + 1][j:k + 1]])
            j = k
    return out, len(bad)


def split_at_culled_lines(g):
    """split a linear mesh where SOF2 would drop a row/column that carries
    texture information (simplify_linear_grid already removed the ones that
    don't), so each becomes a patch edge, which is never dropped."""
    def cuts(lines):
        return [i for i in range(1, len(lines) - 1) if _engine_culls(lines[i - 1], lines[i], lines[i + 1])]

    out = []
    ci = cuts(g)
    for a, b in zip([0] + ci, ci + [len(g) - 1]):
        part = g[a:b + 1]
        rows = [list(r) for r in zip(*part)]
        ri = cuts(rows)
        for c, d in zip([0] + ri, ri + [len(rows) - 1]):
            out.append([list(col) for col in zip(*rows[c:d + 1])])
    return out


def simplify_linear_grid(grid):
    """drop interior rows/columns of a LINEAR mesh that are exactly
    interpolated by their neighbours (flat strips, evenly split decals).
    Cut ~1/3 of mp_shipment's patch control points with no visible change."""
    g = [list(c) for c in grid]
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(g) - 1:
            if _redundant_line(g[i - 1], g[i], g[i + 1]):
                del g[i]
                changed = True
            else:
                i += 1
        rows = [list(r) for r in zip(*g)]
        j = 1
        while j < len(rows) - 1:
            if _redundant_line(rows[j - 1], rows[j], rows[j + 1]):
                del rows[j]
                changed = True
            else:
                j += 1
        g = [list(c) for c in zip(*rows)]
    return g


def _solve3(a, b, c):
    """intersection point of three planes (n, d) or None."""
    (n1, d1), (n2, d2), (n3, d3) = a, b, c
    c23, c31, c12 = v_cross(n2, n3), v_cross(n3, n1), v_cross(n1, n2)
    det = v_dot(n1, c23)
    if abs(det) < 1e-9:
        return None
    return v_scale(v_add(v_add(v_scale(c23, d1), v_scale(c31, d2)), v_scale(c12, d3)), 1.0 / det)


_PT_RE = re.compile(r"\( (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)[ )]")
_OUT_FACE_RE = re.compile(r"^\( (\S+) (\S+) (\S+) \) \( (\S+) (\S+) (\S+) \) \( (\S+) (\S+) (\S+) \)", re.M)


def fix_brush(txt):
    """BobToolz "Fix Brushes" (contrib/bobtoolz DBrush::RemoveRedundantPlanes),
    on the WRITTEN points: drop duplicate planes (keeping the textured face
    over caulk), faces with a bad (zero) normal, and faces that touch fewer
    than 3 of the brush's corners - each of those is one face lost instead of
    a q3map2 "duplicate plane" / "FloatPlane: bad normal" / "mirrored plane".
    Returns (text or None if nothing solid is left, faces removed)."""
    lines = txt.split("\n")
    faces = []                      # (line index, normal, dist, shader)
    for i, l in enumerate(lines):
        m = _OUT_FACE_RE.match(l)
        if not m:
            continue
        v = [float(x) for x in m.groups()]
        a, b, c = v[0:3], v[3:6], v[6:9]
        n = v_cross(v_sub(a, b), v_sub(c, b))
        ln = math.sqrt(v_dot(n, n))
        e1 = math.sqrt(v_dot(v_sub(a, b), v_sub(a, b)))
        e2 = math.sqrt(v_dot(v_sub(c, b), v_sub(c, b)))
        sh = l.split(") ", 3)[-1].split()[0] if l.count(")") >= 3 else ""
        if ln < 0.01 or ln < 1e-5 * e1 * e2:
            faces.append((i, None, 0.0, sh))           # bad normal
            continue
        n = v_scale(n, 1.0 / ln)
        faces.append((i, n, v_dot(n, b), sh))
    drop = set(i for i, n, _, _ in faces if n is None)
    good = [f for f in faces if f[1] is not None]
    # duplicates
    for x in range(len(good)):
        for y in range(x + 1, len(good)):
            fi, fj = good[x], good[y]
            if fi[0] in drop or fj[0] in drop:
                continue
            if v_dot(fi[1], fj[1]) > 0.99999 and abs(fi[2] - fj[2]) < 0.01:
                drop.add(fi[0] if fi[3] == "tools/_caulk" else fj[0])
    good = [f for f in good if f[0] not in drop]
    planes = [(f[1], f[2]) for f in good]
    pts = []
    k = len(planes)
    for x in range(k):
        for y in range(x + 1, k):
            for z in range(y + 1, k):
                p = _solve3(planes[x], planes[y], planes[z])
                if p is None or any(v_dot(n, p) > d + 0.1 for n, d in planes):
                    continue
                if not any(abs(p[0] - q[0]) < 0.05 and abs(p[1] - q[1]) < 0.05 and abs(p[2] - q[2]) < 0.05
                           for q in pts):
                    pts.append(p)
    for f in good:
        if sum(1 for p in pts if abs(v_dot(f[1], p) - f[2]) < 0.1) < 3:
            drop.add(f[0])                              # redundant: owns < 3 corners
    left = [f for f in good if f[0] not in drop]
    if len(left) < 4:
        return None, len(drop)
    if not drop:
        return txt, 0
    return "\n".join(l for i, l in enumerate(lines) if i not in drop), len(drop)


def brush_problem(txt):
    """Why q3map2 would reject or choke on this brush text, or None. Uses the
    WRITTEN (rounded) points, like q3map2's PlaneFromPoints."""
    planes = []
    for m in _OUT_FACE_RE.finditer(txt):
        v = [float(x) for x in m.groups()]
        a, b, c = v[0:3], v[3:6], v[6:9]
        n = v_cross(v_sub(a, b), v_sub(c, b))
        ln = math.sqrt(v_dot(n, n))
        e1 = math.sqrt(v_dot(v_sub(a, b), v_sub(a, b)))
        e2 = math.sqrt(v_dot(v_sub(c, b), v_sub(c, b)))
        if ln < 0.01 or ln < 1e-5 * e1 * e2:
            return "collinear face points (bad normal)"
        n = v_scale(n, 1.0 / ln)
        planes.append((n, v_dot(n, b)))
    if len(planes) < 4:
        return "fewer than 4 faces"
    bb = brush_bounds(planes)
    if bb is None or min(bb[1][k] - bb[0][k] for k in range(3)) < 0.1:
        return "no volume (mirrored planes)"
    return None


def planar_patch(pts, tol=0.05):
    """(normal, dist, bounds) if the points are all in one plane, else None"""
    if len(pts) < 3:
        return None
    a = pts[0]
    far = max(pts, key=lambda q: v_len(v_sub(q, a)))
    best, bn = 0.0, None
    for c in pts:
        n = v_cross(v_sub(far, a), v_sub(c, a))
        l = v_len(n)
        if l > best:
            best, bn = l, n
    if not bn or best < 1e-6:
        return None
    n = v_scale(bn, 1.0 / best)
    d = v_dot(n, a)
    if any(abs(v_dot(n, q) - d) > tol for q in pts):
        return None
    return n, d, (tuple(min(q[k] for q in pts) for k in range(3)), tuple(max(q[k] for q in pts) for k in range(3)))


def patch_triangles(ctrl, linear, steps):
    """CoD patch columns of (xyz, st, alpha) -> triangles of (xyz, st, alpha).
    mesh: the grid itself (CoD meshes are linear); curve: bezier, 3x3 at a time."""
    def lerp(a, b, t):
        return (tuple(a[0][k] + (b[0][k] - a[0][k]) * t for k in range(3)),
                (a[1][0] + (b[1][0] - a[1][0]) * t, a[1][1] + (b[1][1] - a[1][1]) * t),
                a[2] + (b[2] - a[2]) * t)

    def quad_tris(g):
        out = []
        for i in range(len(g) - 1):
            for j in range(len(g[0]) - 1):
                a, b, c, d = g[i][j], g[i + 1][j], g[i + 1][j + 1], g[i][j + 1]
                for t in ((a, b, c), (a, c, d)):
                    if v_len(v_cross(v_sub(t[1][0], t[0][0]), v_sub(t[2][0], t[0][0]))) > 1e-6:
                        out.append(t)
        return out

    if linear:
        return quad_tris(ctrl)

    def bez(p0, p1, p2, t):
        return lerp(lerp(p0, p1, t), lerp(p1, p2, t), t)

    tris = []
    w, h = len(ctrl), len(ctrl[0])
    for i in range(0, w - 2, 2):
        for j in range(0, h - 2, 2):
            g = []
            for si in range(steps + 1):
                u = si / steps
                cols = [bez(ctrl[i][j + k], ctrl[i + 1][j + k], ctrl[i + 2][j + k], u) for k in range(3)]
                g.append([bez(cols[0], cols[1], cols[2], sj / steps) for sj in range(steps + 1)])
            tris.extend(quad_tris(g))
    return tris


def write_lwo_layers(shader_tris):
    """LWO2 with a TXUV and an RGBA vertex map (alpha = CoD vertex alpha).
    q3map2's picomodel reads it with Y/Z swapped (stored here pre-swapped)
    and t flipped (st[1] = 1 - v), and keeps the alpha through lighting."""
    def s0(t):
        b = t.encode("ascii", "replace") + b"\0"
        return b + b"\0" if len(b) % 2 else b

    def vx(i):
        return struct.pack(">H", i) if i < 0xFF00 else struct.pack(">I", 0xFF000000 | i)

    def chunk(tag, data):
        return tag.encode() + struct.pack(">I", len(data)) + data + (b"\0" if len(data) % 2 else b"")

    def sub(tag, data):
        return tag.encode() + struct.pack(">H", len(data)) + data + (b"\0" if len(data) % 2 else b"")

    names = sorted(shader_tris)
    pts, uv, rgba, pols, ptag = [], [], [], [], []
    for ti, name in enumerate(names):
        for tri in shader_tris[name]:
            idx = []
            # reversed: picomodel/assimp flip LWO winding on load; written
            # a-b-c the faces pointed down (normals too) - harmless for the
            # cull-none blend layers, but the ground under them disappeared
            for xyz, st, a in (tri[0], tri[2], tri[1]):
                idx.append(len(pts))
                pts.append(xyz)
                uv.append((st[0], 1.0 - st[1]))
                rgba.append(max(0.0, min(1.0, a / 255.0)))
            pols.append(idx)
            ptag.append(ti)
    body = chunk("LAYR", struct.pack(">HH", 0, 0) + struct.pack(">fff", 0, 0, 0) + s0("layers"))
    body += chunk("PNTS", b"".join(struct.pack(">fff", q[0], q[2], q[1]) for q in pts))
    body += chunk("VMAP", b"TXUV" + struct.pack(">H", 2) + s0("UVMap") +
                  b"".join(vx(i) + struct.pack(">ff", *t) for i, t in enumerate(uv)))
    body += chunk("VMAP", b"RGBA" + struct.pack(">H", 4) + s0("Alpha") +
                  b"".join(vx(i) + struct.pack(">ffff", 1.0, 1.0, 1.0, a) for i, a in enumerate(rgba)))
    body += chunk("POLS", b"FACE" + b"".join(struct.pack(">H", 3) + b"".join(vx(i) for i in t) for t in pols))
    body += chunk("TAGS", b"".join(s0(n) for n in names))
    body += chunk("PTAG", b"SURF" + b"".join(vx(i) + struct.pack(">H", t) for i, t in enumerate(ptag)))
    for n in names:
        body += chunk("SURF", s0(n) + s0("") +
                      sub("COLR", struct.pack(">fff", 1.0, 1.0, 1.0) + struct.pack(">H", 0)) +
                      sub("DIFF", struct.pack(">f", 1.0) + struct.pack(">H", 0)) +
                      sub("SIDE", struct.pack(">H", 3)) +
                      sub("VCOL", struct.pack(">f", 1.0) + struct.pack(">H", 0) + b"RGBA" + s0("Alpha")))
    form = b"LWO2" + body
    return b"FORM" + struct.pack(">I", len(form)) + form


def brush_bounds(planes):
    """(mins, maxs) of a convex brush given its (normal, dist) planes."""
    mn, mx = [1e18] * 3, [-1e18] * 3
    n = len(planes)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                p = _solve3(planes[i], planes[j], planes[k])
                if p is None or any(v_dot(nn, p) > dd + 0.1 for nn, dd in planes):
                    continue
                for a in range(3):
                    mn[a] = min(mn[a], p[a])
                    mx[a] = max(mx[a], p[a])
    return (mn, mx) if mn[0] <= mx[0] else None


def box_brush(c, shader):
    """brush from 8 box corners (index bits: 1=x, 2=y, 4=z), any orientation."""
    centre = v_scale(v_add(c[0], c[7]), 0.5)
    lines = ["{"]
    for i0, i1, i2 in ((0, 2, 4), (1, 3, 5), (0, 1, 4), (2, 3, 6), (0, 1, 2), (4, 5, 6)):
        p1, p2, p3 = c[i0], c[i1], c[i2]
        n = v_cross(v_sub(p1, p2), v_sub(p3, p2))
        if v_dot(n, v_sub(p2, centre)) < 0:
            p2, p3 = p3, p2
        lines.append("( {} ) ( {} ) ( {} ) {} 0 0 0 0.25 0.25 {} 0 0".format(
            fmt3(p1), fmt3(p2), fmt3(p3), shader, CONTENTS_DETAIL))
    lines.append("}")
    return "\n".join(lines)


def sky_box_brushes(mins, maxs, shader, thick=16.0):
    """six slabs enclosing mins..maxs (inner faces exactly on the box)."""
    out = []
    lo = [mins[k] - thick for k in range(3)]
    hi = [maxs[k] + thick for k in range(3)]
    slabs = []
    for axis in range(3):
        for side in (0, 1):
            a, b = list(lo), list(hi)
            if side == 0:
                b[axis] = mins[axis]
            else:
                a[axis] = maxs[axis]
            slabs.append((a, b))
    for a, b in slabs:
        x0, y0, z0 = a
        x1, y1, z1 = b
        faces = [  # standard axial box, q3 winding (normals outward)
            ((x0, y0, z1), (x1, y0, z1), (x0, y1, z1)),   # top    +z
            ((x0, y1, z0), (x1, y1, z0), (x0, y0, z0)),   # bottom -z
            ((x0, y1, z0), (x0, y0, z0), (x0, y1, z1)),   # -x
            ((x1, y0, z0), (x1, y1, z0), (x1, y0, z1)),   # +x
            ((x0, y0, z0), (x1, y0, z0), (x0, y0, z1)),   # -y
            ((x1, y1, z0), (x0, y1, z0), (x1, y1, z1)),   # +y
        ]
        lines = ["{"]
        centre = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        for p1, p2, p3 in faces:
            # same plane convention as every other face here: n = (p1-p2) x (p3-p2),
            # pointing out of the brush; flip the point order if it doesn't
            n = v_cross(v_sub(p1, p2), v_sub(p3, p2))
            if v_dot(n, v_sub(p2, centre)) < 0:
                p2, p3 = p3, p2
            lines.append("( {} ) ( {} ) ( {} ) {} 0 0 0 0.25 0.25 0 0 0".format(
                fmt3(p1), fmt3(p2), fmt3(p3), shader))
        lines.append("}")
        out.append("\n".join(lines))
    return out


def fmt3(p):
    # fixed 4 decimals: "%.6g" kept only 6 significant digits, so at x=12345
    # a point lost everything past 0.1 and small rotated boxes (prop clip)
    # came out with nearly collinear points -> "FloatPlane: bad normal"
    out = []
    for x in p:
        t = "{:.4f}".format(x).rstrip("0").rstrip(".")
        out.append("0" if t in ("-0", "") else t)
    return " ".join(out)


def round_ang(a):
    a = round(a, 4)
    return 0.0 if abs(a) < 1e-4 else a


def linear_to_bezier(grid):
    """Linear W x H control grid -> exact quadratic bezier (2W-1) x (2H-1):
    a midpoint control point makes each bezier span a straight, uniformly
    parametrized segment, so positions and UVs stay linear."""
    W, H = len(grid), len(grid[0])

    def mid(a, b):
        return (v_scale(v_add(a[0], b[0]), 0.5), ((a[1][0] + b[1][0]) * 0.5, (a[1][1] + b[1][1]) * 0.5))

    cols = []
    for c in range(W):
        col = []
        for r in range(H):
            col.append(grid[c][r])
            if r < H - 1:
                col.append(mid(grid[c][r], grid[c][r + 1]))
        cols.append(col)
    out = []
    for c in range(W):
        out.append(cols[c])
        if c < W - 1:
            out.append([mid(a, b) for a, b in zip(cols[c], cols[c + 1])])
    return out


def split_grid(grid, max_cells=MAX_PATCH_CELLS):
    """split an odd-sized bezier control grid into chunks of <= MAX_PATCH,
    sharing the boundary row/column so the surface stays continuous.

    max_cells additionally caps (w-1)*(h-1) per piece, which is what SOF2's
    patch collision can take (see MAX_PATCH_CELLS). The longer axis is cut
    first, so a long thin patch is not squared off for no reason. Pass 0 to
    cap on MAX_PATCH alone, for a patch that carries no collision."""
    W, H = len(grid), len(grid[0])

    cw, ch = MAX_PATCH, MAX_PATCH
    if max_cells:
        # shrink the longer axis until the cell count fits, keeping sizes odd
        while (min(cw, W) - 1) * (min(ch, H) - 1) > max_cells:
            if min(cw, W) >= min(ch, H):
                cw -= 2
            else:
                ch -= 2
            if cw <= 3 or ch <= 3:
                break

    def ranges(n, cap):
        if n <= cap:
            return [(0, n)]
        out, a = [], 0
        while a < n - 1:
            b = min(a + cap, n)
            if (b - a) % 2 == 0:
                b -= 1
            out.append((a, b))
            a = b - 1
        return out

    for c0, c1 in ranges(W, cw):
        for r0, r1 in ranges(H, ch):
            yield [col[r0:r1] for col in grid[c0:c1]]


# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--prefabs", help="folder containing prefabs/ (default: next to/above the map)")
    ap.add_argument("--assets", help="unzipped cod4_assets folder (materials/, images/)")
    ap.add_argument("--manifest", help="write JSON manifest (default: <dst>.json)")
    ap.add_argument("--class-map", help="JSON {cod_class: sof2_class} overrides")
    ap.add_argument("--model-offsets", help="cod4_md3_offsets.json from xmodel2md3.py "
                    "(re-centred / split models); default: <dst dir>/models/cod4/cod4_md3_offsets.json if present")
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                    help="CoD -> SOF2 world scale (default %(default)s, as the CS:S pipeline)")
    ap.add_argument("--no-prop-collision", action="store_true",
                    help="skip the clip brushes generated from CoD model collision")
    ap.add_argument("--max-static", type=int, default=None,
                    help="most model_static entities (default %d: what fits the 256-entity snapshot "
                         "even with fast vis). Higher needs a full -vis; check with bsp_static_pvs.py"
                         % Converter.MAX_STATIC_ENTITIES)
    ap.add_argument("--fog", metavar="START,HALF,R,G,B",
                    help="CoD setExpFog values to use instead of the ones in the map's .gsc files")
    ap.add_argument("--no-fog", action="store_true", help="no fog / distancecull")
    ap.add_argument("--occluders", action="store_true",
                    help="turn wall-like prop collision panels into structural caulk brushes, "
                         "so vis can cull behind them (a misc_model blocks nothing by itself). "
                         "OFF by default: measured on mp_derail it produced 205 brushes and "
                         "moved visible clusters from 147,426 to 147,425 - the panels CoD "
                         "gives us are container faces and vehicle sides, too small to matter. "
                         "Worth trying on a map whose buildings are models")
    ap.add_argument("--min-vertex-light", type=float, default=None, metavar="N",
                    help="worldspawn _minvertexlight, 0-255 (default %g; 0 = off). Vertex-lit "
                         "surfaces - every alpha-tested and blended world surface, and the "
                         "baked terrain - cannot go darker than this. Without it a decal lying "
                         "on a floor samples the floor plane, reads as occluded and renders "
                         "black." % Converter.MIN_VERTEX_LIGHT)
    ap.add_argument("--fog-max", type=float, default=None,
                    help="pull the fog in to be opaque by this distance, so vis can drop the far "
                         "side of the map. Default 0: CoD's own fog distance, whatever it is "
                         "(mp_strike's is 17,500 units). 8000 is about as near as a big outdoor "
                         "map takes without the air looking wrong")
    ap.add_argument("--clutter-radius", type=float, default=None,
                    help="leave out props smaller than this placed radius (default %g, 0 = keep all)"
                    % Converter.CLUTTER_RADIUS)
    ap.add_argument("--no-auto-detail", action="store_true",
                    help="keep every CoD brush structural (much slower -vis)")
    ap.add_argument("--no-model-static", action="store_true",
                    help="bake every prop as misc_model (no model_static entities)")
    ap.add_argument("--prop-vertex-budget", type=int, default=None,
                    help='max baked misc_model vertices; over it, models step down their LODs. Left out, it is taken from the size of the last .bsp this map compiled to (bsp_budget.py), or 60000 on a map never compiled; 0 = off')
    ap.add_argument("--drop-model", action="append", default=[],
                    help="regex of xmodel names to leave out (repeatable)")
    a = ap.parse_args()

    prefab_root = a.prefabs
    if not prefab_root:
        d = os.path.dirname(os.path.abspath(a.src))
        for _ in range(5):
            if os.path.isdir(os.path.join(d, "prefabs")):
                prefab_root = d
                break
            d = os.path.dirname(d)
    cmap = json.load(open(a.class_map)) if a.class_map else None
    dst_dir = os.path.dirname(os.path.abspath(a.dst))
    offs_path = a.model_offsets
    if not offs_path:
        for root in (dst_dir, os.path.dirname(dst_dir)):   # <out>/ or <out>/maps/
            cand = os.path.join(root, "models", "cod4", "cod4_md3_offsets.json")
            if os.path.exists(cand):
                offs_path = cand
                break
    offsets = json.load(open(offs_path)) if offs_path and os.path.exists(offs_path) else {}
    if not offsets:
        print("note: no model offsets file - run xmodel2md3.py first so big models land correctly")
    collision = {}
    if offs_path and not a.no_prop_collision:
        cpath = os.path.join(os.path.dirname(offs_path), "cod4_collision.json")
        if os.path.exists(cpath):
            collision = json.load(open(cpath))
    budget = a.prop_vertex_budget
    if budget is None:
        # <out-root>/<map>/maps/<map>.map  ->  <out-root>
        out_root = os.path.dirname(os.path.dirname(dst_dir))
        name = os.path.splitext(os.path.basename(a.dst))[0]
        try:
            import bsp_budget
            budget, why = bsp_budget.suggest(out_root, name, PROP_VERTEX_BUDGET)
        except Exception as e:
            budget = PROP_VERTEX_BUDGET
            why = ("baked prop budget: %d (could not read the last .bsp size: %s)"
                   % (PROP_VERTEX_BUDGET, e))
        print(why)
    if a.max_static is not None:
        Converter.MAX_STATIC_ENTITIES = a.max_static
    conv = Converter(prefab_root, Assets(a.assets), cmap, drop_models=a.drop_model,
                     model_offsets=offsets, scale=a.scale, collision=collision,
                     md3_dir=os.path.dirname(offs_path) if offs_path and os.path.exists(offs_path) else None,
                     prop_budget=budget, use_model_static=not a.no_model_static)
    conv.auto_detail = not a.no_auto_detail
    if a.min_vertex_light is not None:
        conv.min_vertex_light = a.min_vertex_light
    conv.occluders = a.occluders
    conv.blend_dir = os.path.join(os.path.dirname(dst_dir), "models", "cod4", "blend")
    if a.fog_max is not None:
        conv.fog_max = a.fog_max
    if a.clutter_radius is not None:
        conv.clutter_radius = a.clutter_radius
    if a.no_fog:
        conv.fog = False
    elif a.fog:
        conv.fog = conv.make_fog([float(v) for v in a.fog.split(",")], "--fog")
    text = conv.convert(a.src)
    os.makedirs(dst_dir, exist_ok=True)
    with open(a.dst, "w", newline="\n") as fh:
        fh.write(text)
    man = conv.manifest()
    man["baked_prop_vertices"] = getattr(conv, "baked_prop_vertices", 0)
    mpath = a.manifest or os.path.splitext(a.dst)[0] + ".json"
    with open(mpath, "w") as fh:
        json.dump(man, fh, indent=1)

    print("prefab root:", prefab_root)
    for k, v in sorted(conv.stats.items()):
        print("  %-45s %d" % (k, v))
    print("entities written:")
    for k, v in conv.class_out.most_common():
        print("  %-45s %d" % (k, v))
    if conv.class_drop:
        print("entities dropped:")
        for k, v in conv.class_drop.most_common():
            print("  %-45s %d" % (k, v))
    print("materials used: %d (%d without asset data), models: %d unique / %d placements"
          % (len(man["materials"]), sum(1 for m in man["materials"].values() if not m["found"]),
             len(man["models"]), sum(man["models"].values())))
    for line in conv.budget_report:
        print("prop budget:", line)
    if conv.missing_prefabs:
        print("MISSING prefabs:", sorted(conv.missing_prefabs))
    for k, v in conv.warn.most_common(30):
        print("  warn: %s (x%d)" % (k, v))
    print("->", a.dst)
    print("->", mpath)


if __name__ == "__main__":
    main()
