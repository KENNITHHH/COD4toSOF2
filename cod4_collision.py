"""
cod4_collision.py - CoD4 xmodel collision -> boxes for SOF2 clip brushes.

CoD4 props collide against TRIANGLE MESHES stored in the xmodel file
(XModelCollSurf: per triangle a plane plus barycentric edge functions
svec/tvec, KisakCOD xmodel.h) - "complex as simple" in Unreal terms. A crate
has 124 collision triangles, a container wall 3216, a truck 5047. idTech3
collides only with convex brushes, and q3map2's per-triangle autoclip on
meshes that size blows the plane limit (seen in the CS:S pipeline), so each
solid collision mesh is approximated by a small set of boxes:

  1. triangles -> surface voxels (dense point sampling, resolution r)
  2. interior filled (flood from outside; open shells stay one voxel thick)
  3. greedy merge into axis-aligned boxes (model space)
  4. if a surface needs more than MAX_BOXES, retry at 2r, 4r ...

Boxes are in model space; iwmap2map.py turns each into an oriented clip
brush per placement (rotation + scale applied).
"""
import numpy as np

MAX_BOXES = 24                      # per MODEL (all its solid surfaces together)
RESOLUTIONS = (4.0, 8.0, 16.0, 32.0, 64.0, 128.0)
CONTENTS_SOLID = 0x1


def tri_vertices(tri):
    """(plane, svec, tvec) -> 3 vertices. CoD: s = dot(svec.xyz, p) - svec.w,
    t likewise; v0 is s=t=0, v1 s=1, v2 t=1 (verified: reproduces each
    surface's stored bounds exactly)."""
    pl, sv, tv = tri
    m = np.array([pl[:3], sv[:3], tv[:3]], dtype=float)
    if abs(np.linalg.det(m)) < 1e-12:
        return None
    out = []
    for s, t in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)):
        out.append(np.linalg.solve(m, np.array([pl[3], s + sv[3], t + tv[3]])))
    return np.array(out)


def _sample_triangles(tris, step):
    pts = []
    for v0, v1, v2 in tris:
        e1, e2 = v1 - v0, v2 - v0
        n = int(max(np.linalg.norm(e1), np.linalg.norm(e2), np.linalg.norm(v2 - v1)) / step) + 2
        a = np.linspace(0.0, 1.0, n)
        A, B = np.meshgrid(a, a)
        m = (A + B) <= 1.0
        A, B = A[m], B[m]
        pts.append(v0 + np.outer(A, e1) + np.outer(B, e2))
    return np.concatenate(pts) if pts else np.zeros((0, 3))


def _flood_exterior(occ):
    """occupied grid (padded by one empty layer) -> exterior mask, 6-connected."""
    ext = np.zeros_like(occ)
    ext[0, 0, 0] = True
    frontier = ext.copy()
    while frontier.any():
        grow = np.zeros_like(frontier)
        grow[1:] |= frontier[:-1]
        grow[:-1] |= frontier[1:]
        grow[:, 1:] |= frontier[:, :-1]
        grow[:, :-1] |= frontier[:, 1:]
        grow[:, :, 1:] |= frontier[:, :, :-1]
        grow[:, :, :-1] |= frontier[:, :, 1:]
        frontier = grow & ~occ & ~ext
        ext |= frontier
    return ext


def _enclosed(occ):
    """voxels with occupied cells in at least 4 of the 6 axis directions:
    fills closed shells AND shells open on one or two sides (open crates)
    while leaving thin walls and open frames alone."""
    hits = np.zeros(occ.shape, dtype=np.int8)
    for axis in range(3):
        fwd = np.maximum.accumulate(occ, axis=axis)
        bwd = np.flip(np.maximum.accumulate(np.flip(occ, axis), axis=axis), axis)
        hits += fwd.astype(np.int8) + bwd.astype(np.int8)
    return occ | (hits >= 4)


def _greedy_boxes(solid):
    boxes = []
    todo = solid.copy()
    X, Y, Z = solid.shape
    while todo.any():
        x, y, z = np.argwhere(todo)[0]
        x1 = x + 1
        while x1 < X and todo[x1, y, z]:
            x1 += 1
        y1 = y + 1
        while y1 < Y and todo[x:x1, y1, z].all():
            y1 += 1
        z1 = z + 1
        while z1 < Z and todo[x:x1, y:y1, z1].all():
            z1 += 1
        todo[x:x1, y:y1, z:z1] = False
        boxes.append(((x, y, z), (x1, y1, z1)))
    return boxes


def mesh_boxes(tris, max_boxes=MAX_BOXES):
    """triangles (N,3,3) -> [(mins, maxs)] boxes in model space."""
    tris = [t for t in tris if t is not None]
    if not tris:
        return []
    allv = np.concatenate(tris)
    lo, hi = allv.min(0), allv.max(0)
    start = max(4.0, float((hi - lo).max()) / 48.0)      # ~48 voxels across the model
    for r in [x for x in RESOLUTIONS if x >= start / 2] or RESOLUTIONS[-1:]:
        origin = np.floor(lo / r) * r - r                    # one empty layer of padding
        dims = (np.ceil((hi - origin) / r).astype(int) + 2)
        if np.prod(dims) > 4_000_000:
            continue
        occ = np.zeros(dims, dtype=bool)
        pts = _sample_triangles(tris, r / 3.0)
        idx = np.floor((pts - origin) / r).astype(int)
        idx = np.clip(idx, 0, dims - 1)
        occ[idx[:, 0], idx[:, 1], idx[:, 2]] = True
        solid = _enclosed(occ) | ~_flood_exterior(occ)
        boxes = _greedy_boxes(solid)
        if len(boxes) <= max_boxes or r == RESOLUTIONS[-1]:
            out = []
            for (a, b) in boxes:
                mn = origin + np.array(a) * r
                mx = origin + np.array(b) * r
                # never extend past the real collision bounds (voxel rounding)
                mn = np.maximum(mn, lo)
                mx = np.minimum(mx, hi)
                if (mx - mn).min() < 0.5:                     # flat surface -> 1 unit slab
                    k = int(np.argmin(mx - mn))
                    c = (mn[k] + mx[k]) / 2
                    mn[k], mx[k] = c - 0.5, c + 0.5
                out.append(([round(float(v), 3) for v in mn], [round(float(v), 3) for v in mx]))
            return out
    return []


def model_collision(xm, pose=None, max_boxes=MAX_BOXES):
    """xmodel (xmodel_v25.read_xmodel) -> boxes for its SOLID collision
    surfaces (model space as stored; `pose` ignored)."""
    tris = []
    for cs in xm.get("collSurfs", []):
        if not (cs["contents"] & CONTENTS_SOLID):
            continue                       # foliage, glass-only, clip-missile ... not player-solid
        for tri in cs["tris"]:
            v = tri_vertices(tri)
            if v is None:
                continue
            # model space as stored, like the render vertices (see
            # xmodel2md3.surf_vertices) - no bone transform
            tris.append(v)
    return mesh_boxes(tris, max_boxes)
