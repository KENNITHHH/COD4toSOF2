#!/usr/bin/env python3
"""
xmodel2md3.py - CoD4 (IW3) raw xmodel (v25) -> SOF2 .md3

Reads xmodel/<name> (LOD table + per-LOD materials), xmodelsurfs/<lod file>
and xmodelparts/<lod0 file> exactly as KisakCOD src/xanim/xmodel_load_obj.cpp
does, poses every vertex by its bone's base pose (XModelCalcBasePose:
world = parent (x) local, 3-short quats), and writes MD3 with the same
surface builder sof2_md3.py uses (1000-vertex surface cap, normals,
qpath shortening).

Shaders: every surface points at "models/cod4/<material>", which
cod4mat2shader.py defines (runtime form + "<name>_bsp" baked form).

MD3 stores positions as int16 / 64, so nothing may sit further than 512 units
from the model origin:
  - a model that fits after re-centring is written re-centred and its offset
    recorded in cod4_md3_offsets.json;
  - a model that is too big even then (cranes, towers) is cut into pieces on
    a 960-unit grid, each piece its own <name>_pN.md3 with its own offset.
iwmap2map.py reads that json (--model-offsets) and moves/duplicates the
misc_model placements to match, so placement in the map is unchanged.

Usage:
  python xmodel2md3.py --assets <cod4_assets> --out <root> [--manifest map.json] [names...]
  (writes <root>/models/cod4/*.md3 and <root>/models/cod4/cod4_md3_offsets.json)
"""
import argparse
import json
import math
import os
import re
import struct
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from xmodel_v25 import read_xmodel, read_surfs, read_parts   # noqa: E402
import sof2_md3 as M3                                         # noqa: E402  (SOF2 .md3 writer)
from cod4_collision import model_collision                    # noqa: E402

MODEL_DIR = "models/cod4"
MD3_LIMIT = 511.0            # keep a margin inside int16/64
PIECE_GRID = 960.0


def safe(name):
    return re.sub(r"[^a-z0-9_/\-]", "_", name.lower())


# ------------------------------------------------------------------ bones
def q_mul(a, b):
    """Hamilton product a (x) b, quats as (x, y, z, w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def q_norm(q):
    l = math.sqrt(sum(c * c for c in q))
    return tuple(c / l for c in q) if l > 1e-12 else (0.0, 0.0, 0.0, 1.0)


def q_rot(q, v):
    x, y, z, w = q
    # v' = q v q*
    tx = 2 * (y * v[2] - z * v[1])
    ty = 2 * (z * v[0] - x * v[2])
    tz = 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty),
            v[1] + w * ty + (z * tx - x * tz),
            v[2] + w * tz + (x * ty - y * tx))


def base_pose(parts):
    """XModelCalcBasePose. KisakCOD: QuatMultiply(local, parentWorld) which
    is the Hamilton product parent (x) local; trans = parent applied to the
    local translation."""
    bones = parts["bones"]
    pose = []
    for i, b in enumerate(bones):
        if i < parts["numRoot"]:
            pose.append(((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))
            continue
        pq, pt = pose[b["parent"]]
        q = q_norm(q_mul(pq, b["quat"]))
        t = q_rot(pq, b["trans"])
        pose.append((q, (t[0] + pt[0], t[1] + pt[1], t[2] + pt[2])))
    return pose


# --------------------------------------------------------------- convert
_TECH_CACHE = {}


def is_shadow_proxy(assets, mat):
    """True when the material's techset is CoD's shadow-only 'shadowcaster'."""
    if mat not in _TECH_CACHE:
        tech = ""
        p = os.path.join(assets, "materials", mat)
        if os.path.exists(p):
            try:
                from material_iw3 import read_material
                tech = (read_material(open(p, "rb").read()).get("techset") or "").lower()
            except Exception:
                tech = ""
        _TECH_CACHE[mat] = tech.startswith("shadowcaster")
    return _TECH_CACHE[mat]
def surf_vertices(s, pose=None):
    """[(pos, normal, uv)] for one xmodelsurfs surface.

    CoD4's raw v25 surfaces store rigid vertices ALREADY in model space (the
    bind pose): on vehicle_80s_sedan1 every part's raw centroid sits on its
    own bone (wheels on tag_wheel_*, bumpers on tag_bumper_*), and the
    180-degree-rotated right-wheel bones carry unmirrored geometry. Applying
    the bone transform on top shifted each part out by its bone offset -
    the "exploded" doors/wheels seen in game. So vertices are used as stored;
    `pose` is accepted for API compatibility and ignored."""
    return [(v["p"], v["n"], v["uv"]) for v in s["verts"]]


def piece_key(p):
    return tuple(int(math.floor(c / PIECE_GRID)) for c in p)


def _lerp_vert(a, b, t):
    p = tuple(a[0][k] + (b[0][k] - a[0][k]) * t for k in range(3))
    n = tuple(a[1][k] + (b[1][k] - a[1][k]) * t for k in range(3))
    l = math.sqrt(sum(c * c for c in n)) or 1.0
    uv = (a[2][0] + (b[2][0] - a[2][0]) * t, a[2][1] + (b[2][1] - a[2][1]) * t)
    return (p, tuple(c / l for c in n), uv)


def _split_poly(poly, axis, plane):
    """split a convex polygon of (pos, normal, uv) at pos[axis] == plane."""
    lo, hi = [], []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        da, db = a[0][axis] - plane, b[0][axis] - plane
        (lo if da < 0 else hi).append(a)
        if (da < 0) != (db < 0) and da != db:
            m = _lerp_vert(a, b, da / (da - db))
            lo.append(m)
            hi.append(m)
    return lo, hi


def clip_to_grid(tri):
    """cut one triangle along every PIECE_GRID plane it crosses; returns
    triangles that each lie inside one grid cell (UVs/normals interpolated)."""
    polys = [list(tri)]
    for axis in range(3):
        out = []
        for poly in polys:
            vals = [v[0][axis] for v in poly]
            k0 = int(math.floor(min(vals) / PIECE_GRID)) + 1
            k1 = int(math.ceil(max(vals) / PIECE_GRID)) - 1
            rest = poly
            for k in range(k0, k1 + 1):
                lo, hi = _split_poly(rest, axis, k * PIECE_GRID)
                if len(lo) >= 3:
                    out.append(lo)
                rest = hi
                if len(rest) < 3:
                    break
            if len(rest) >= 3:
                out.append(rest)
        polys = out
    tris = []
    for poly in polys:
        for i in range(1, len(poly) - 1):
            tris.append([poly[0], poly[i], poly[i + 1]])
    return tris


def write_md3(path, name, surfaces, shift):
    """surfaces: [(shader, verts, tris)] with verts already in model space."""
    blobs = []
    mins = [1e9] * 3
    maxs = [-1e9] * 3
    idx = 0
    for shader, verts, tris in surfaces:
        for cv, ct in M3.split_oversized_md3_surface(verts, tris, M3.MD3_MAX_VERTS_PER_SURFACE):
            for p, _, _ in cv:
                for k in range(3):
                    mins[k] = min(mins[k], p[k] - shift[k])
                    maxs[k] = max(maxs[k], p[k] - shift[k])
            blobs.append(M3.build_surface("surf%d" % idx, cv, ct, shader, shift))
            idx += 1
    if not blobs:
        return None
    radius = math.sqrt(max(sum(c * c for c in mins), sum(c * c for c in maxs)))
    frame = struct.pack("<3f3f3ff16s", mins[0], mins[1], mins[2], maxs[0], maxs[1], maxs[2],
                        0.0, 0.0, 0.0, radius, b"\0" * 16)
    body = b"".join(blobs)
    ofs_frames = 108
    ofs_surf = ofs_frames + len(frame)
    header = struct.pack("<4si64s9i", b"IDP3", 15, name.encode("ascii", "replace")[:63].ljust(64, b"\0"),
                         0, 1, 0, len(blobs), 0, ofs_frames, ofs_surf, ofs_surf, ofs_surf + len(body))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header + frame + body)
    return {"surfaces": len(blobs), "mins": mins, "maxs": maxs}


def lod_tri_sets(assets, L, surfs, pose, warn):
    """[(shader, verts, tris)] for one LOD's surfaces."""
    mats = L["materials"]
    tri_sets = []
    for si, s in enumerate(surfs):
        mat = mats[si] if si < len(mats) else "$default"
        if mat.startswith("$"):
            warn["surface with $default material skipped"] += 1
            continue
        if is_shadow_proxy(assets, mat):
            # CoD 'shadowcaster' techset: a shadow-only proxy mesh (tree
            # trunks/canopies), never drawn. SOF2 has no such pass - q3map2
            # shadows come from the real surfaces (alphashadow on foliage).
            warn["shadowcaster proxy surface dropped"] += 1
            continue
        verts = surf_vertices(s, pose)
        # Drop zero-area triangles. CoD's meshes carry some (a collapsed edge
        # on a LOD, a degenerate strip join) and the engine never notices;
        # q3map2 does, and reports them per model - mp_shipment's compile had
        # 17 of those warnings, one of them for 76 triangles at once. They
        # contribute nothing to draw or to trace, so they go out here.
        tris, dropped = [], 0
        for t in s["tris"]:
            a, b, c = (verts[i][0] for i in t[:3])
            u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            if n[0] * n[0] + n[1] * n[1] + n[2] * n[2] < 1e-10:
                dropped += 1
                continue
            tris.append(tuple(t))
        if dropped:
            warn["degenerate triangles dropped"] += dropped
        if not tris:
            warn["surface with no triangles left skipped"] += 1
            continue
        tri_sets.append(("models/cod4/" + safe(mat), verts, tris))
    return tri_sets


def write_lower_lods(assets, xm, first, sdir, pose, out_root, base, shift, warn, info):
    """<base>_lod1.md3, _lod2 ... from the LODs after `first`, all with the SAME
    origin shift as the main .md3 (so the map placement stays valid). The
    prop budget in iwmap2map.py steps placements down this chain - the meshes
    CoD itself draws at a distance - when the baked props are over budget."""
    k = 0
    for L in xm["lods"][first + 1:]:
        path = os.path.join(sdir, L["file"])
        if not os.path.exists(path):
            continue
        surfs, _ = read_surfs(open(path, "rb").read())
        ts = lod_tri_sets(assets, L, surfs, pose, warn)
        pts = [p for _, vs, _ in ts for p, _, _ in vs]
        if not pts:
            continue
        if any(abs(p[a] - shift[a]) > MD3_LIMIT for p in pts for a in range(3)):
            break               # would not fit with LOD0's offset; stop the chain
        k += 1
        write_md3(os.path.join(out_root, MODEL_DIR, "%s_lod%d.md3" % (base, k)), base, ts, shift)
        info.setdefault("lod_verts", []).append(len(pts))
        info.setdefault("lod_dist", []).append(L["dist"])
    if k:
        warn["models with a lower-LOD chain"] += 1


# CoD surface type (infoParms order) -> SOF2 shipped clip shader, following
# phy2clip.py: the _shoot_* family because a prop's collision should stop
# bullets as well as players (SOF2's _clip has no shotclip).
COD_SURF = ["default", "bark", "brick", "carpet", "cloth", "concrete", "dirt", "flesh",
            "foliage", "glass", "grass", "gravel", "ice", "metal", "mud", "paper", "plaster",
            "rock", "sand", "snow", "water", "wood", "asphalt", "ceramic", "plastic", "rubber",
            "cushion", "fruit", "paintedmetal", "opaqueglass"]
CLIP_SHADER = {
    "metal": "tools/_shoot_metal", "paintedmetal": "tools/_shoot_metal",
    "wood": "tools/_shoot_wood", "bark": "tools/_shoot_wood", "paper": "tools/_shoot_wood",
    "concrete": "tools/_shoot_stone", "brick": "tools/_shoot_stone", "rock": "tools/_shoot_stone",
    "plaster": "tools/_shoot_stone", "asphalt": "tools/_shoot_stone", "ceramic": "tools/_shoot_stone",
    "cloth": "tools/_shoot_cloth", "carpet": "tools/_shoot_cloth", "cushion": "tools/_shoot_cloth",
    "sand": "tools/_shoot_sandbag", "dirt": "tools/_shoot_sandbag", "gravel": "tools/_shoot_sandbag",
    "mud": "tools/_shoot_sandbag",
}


def clip_shader_for(assets, xm):
    """clip shader from the surface type of the material covering most triangles."""
    from material_iw3 import read_material
    weight = Counter()
    L = xm["lods"][-1]
    for mat in L["materials"]:
        p = os.path.join(assets, "materials", mat)
        if not os.path.exists(p):
            continue
        try:
            sf = read_material(open(p, "rb").read()).get("surfaceFlags", 0)
        except Exception:
            continue
        idx = (sf >> 20) & 0x1F
        weight[COD_SURF[idx] if idx < len(COD_SURF) else "default"] += 1
    for surf, _ in weight.most_common():
        if surf in CLIP_SHADER:
            return CLIP_SHADER[surf]
    return "tools/_shoot"


def convert_model(assets, name, out_root, warn):
    xpath = os.path.join(assets, "xmodel", name)
    if not os.path.exists(xpath):
        warn["xmodel missing"] += 1
        return None
    xm = read_xmodel(open(xpath, "rb").read())
    sdir = os.path.join(assets, "xmodelsurfs")
    lod_used = None
    for li, L in enumerate(xm["lods"]):
        if os.path.exists(os.path.join(sdir, L["file"])):
            lod_used = li
            break
    if lod_used is None:
        warn["no xmodelsurfs for any LOD"] += 1
        return {"error": "no surfs"}
    L = xm["lods"][lod_used]
    surfs, exact = read_surfs(open(os.path.join(sdir, L["file"]), "rb").read())
    if not exact:
        warn["xmodelsurfs not fully consumed (format drift?)"] += 1
    pose = None
    needs_bones = any(len(s["vlists"]) != 1 or s["deformed"] or (s["vlists"] and s["vlists"][0][1] != 0)
                      for s in surfs)
    ppath = os.path.join(assets, "xmodelparts", xm["lods"][0]["file"])
    if os.path.exists(ppath):
        pose = base_pose(read_parts(open(ppath, "rb").read()))
    elif needs_bones:
        warn["multi-bone model without xmodelparts (posed at bind offsets)"] += 1

    tri_sets = lod_tri_sets(assets, L, surfs, pose, warn)

    allp = [p for _, vs, _ in tri_sets for p, _, _ in vs]
    if not allp:
        return {"error": "empty"}
    mins = [min(p[k] for p in allp) for k in range(3)]
    maxs = [max(p[k] for p in allp) for k in range(3)]
    fits_raw = all(-MD3_LIMIT <= mins[k] and maxs[k] <= MD3_LIMIT for k in range(3))
    base = safe(name)
    # CoD draws LOD i while the camera is closer than lods[i].dist (0 = no
    # limit, the last LOD); iwmap2map.py picks each placement's LOD from these
    info = {"lod": lod_used, "verts": len(allp), "tris": sum(len(t) for _, _, t in tri_sets),
            "pieces": [], "lod_dist": [L["dist"]]}
    if fits_raw:
        write_md3(os.path.join(out_root, MODEL_DIR, base + ".md3"), base, tri_sets, (0.0, 0.0, 0.0))
        info["pieces"].append([base, [0.0, 0.0, 0.0]])
        write_lower_lods(assets, xm, lod_used, sdir, pose, out_root, base, (0.0, 0.0, 0.0), warn, info)
        return info
    centre = [(mins[k] + maxs[k]) / 2 for k in range(3)]
    if all(maxs[k] - mins[k] <= 2 * MD3_LIMIT for k in range(3)):
        write_md3(os.path.join(out_root, MODEL_DIR, base + ".md3"), base, tri_sets, centre)
        info["pieces"].append([base, [round(c, 4) for c in centre]])
        warn["re-centred (offset recorded)"] += 1
        write_lower_lods(assets, xm, lod_used, sdir, pose, out_root, base, centre, warn, info)
        return info
    # cut into grid pieces by triangle centroid
    buckets = {}
    for shader, verts, tris in tri_sets:
        for t in tris:
            for sub in clip_to_grid([verts[i] for i in t]):
                c = [sum(v[0][k] for v in sub) / 3 for k in range(3)]
                buckets.setdefault(piece_key(c), {}).setdefault(shader, []).append(sub)
    for pi, (key, by_shader) in enumerate(sorted(buckets.items())):
        surfaces = []
        pts = []
        for shader, tri_verts in by_shader.items():
            vs, ts = [], []
            for tv in tri_verts:
                ts.append((len(vs), len(vs) + 1, len(vs) + 2))
                vs.extend(tv)
            surfaces.append((shader, vs, ts))
            pts += [v[0] for v in vs]
        pmin = [min(p[k] for p in pts) for k in range(3)]
        pmax = [max(p[k] for p in pts) for k in range(3)]
        pc = [(pmin[k] + pmax[k]) / 2 for k in range(3)]
        if any(pmax[k] - pmin[k] > 2 * MD3_LIMIT for k in range(3)):
            warn["piece still > 1022 units (a single huge triangle?)"] += 1
        pname = "%s_p%d" % (base, pi)
        write_md3(os.path.join(out_root, MODEL_DIR, pname + ".md3"), pname, surfaces, pc)
        info["pieces"].append([pname, [round(c, 4) for c in pc]])
    warn["split into pieces"] += 1
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--assets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", help="iwmap2map manifest: convert the models it lists")
    a = ap.parse_args()
    names = list(a.names)
    if a.manifest:
        names += list(json.load(open(a.manifest)).get("models", {}).keys())
    if not names:
        names = sorted(os.listdir(os.path.join(a.assets, "xmodel")))
    names = sorted(set(names))
    warn = Counter()
    offsets = {}
    collision = {}
    lods = {}
    report = {}
    for n in names:
        try:
            info = convert_model(a.assets, n, a.out, warn)
        except Exception as e:      # report and keep going
            info = {"error": "%s: %s" % (type(e).__name__, e)}
            warn["exception"] += 1
        report[n] = info
        if info and "pieces" in info:
            offsets[safe(n)] = info["pieces"]
            lods[safe(n)] = info.get("lod_dist", [])
        try:
            xm = read_xmodel(open(os.path.join(a.assets, "xmodel", n), "rb").read())
            ppath = os.path.join(a.assets, "xmodelparts", xm["lods"][0]["file"])
            pose = base_pose(read_parts(open(ppath, "rb").read())) if os.path.exists(ppath) else None
            boxes = model_collision(xm, pose)
            if boxes:
                # "boxes_lo": at most 4 boxes, for ordinary props - every box
                # is a brush with its own planes and bevels (mp_derail: 41,955
                # clip brushes, 790k planes)
                lo = boxes if len(boxes) <= 4 else (model_collision(xm, pose, 4) or boxes)
                collision[safe(n)] = {"shader": clip_shader_for(a.assets, xm), "boxes": boxes,
                                      "boxes_lo": lo}
                warn["models with collision boxes"] += 1
        except Exception as e:
            warn["collision failed: %s" % type(e).__name__] += 1
    odir = os.path.join(a.out, MODEL_DIR)
    os.makedirs(odir, exist_ok=True)
    with open(os.path.join(odir, "cod4_md3_offsets.json"), "w") as fh:
        json.dump(offsets, fh, indent=1)
    with open(os.path.join(odir, "cod4_collision.json"), "w") as fh:
        json.dump(collision, fh)
    with open(os.path.join(odir, "cod4_lods.json"), "w") as fh:
        json.dump(lods, fh)
    print("collision: %d models, %d boxes (model space)" % (
        len(collision), sum(len(c["boxes"]) for c in collision.values())))
    ok = sum(1 for r in report.values() if r and "pieces" in r)
    lod_hist = Counter(r["lod"] for r in report.values() if r and "lod" in r)
    print("models: %d converted, %d failed; source LOD used: %s" % (ok, len(names) - ok, dict(lod_hist)))
    for k, v in warn.most_common():
        print("  %-60s %d" % (k, v))
    for n, r in report.items():
        if not r or "error" in r:
            print("  FAILED %s: %s" % (n, r and r.get("error")))
    print("->", odir)


if __name__ == "__main__":
    main()
