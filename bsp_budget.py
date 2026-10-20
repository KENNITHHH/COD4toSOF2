#!/usr/bin/env python3
"""
bsp_budget.py - pick the baked prop budget from the last .bsp this map made.

Why this exists
---------------
SOF2 is a 32-bit process and reads the whole .bsp in ONE allocation. What fits
depends on com_hunkMegs, because the hunk is reserved as one contiguous block
at startup and a big hunk eats the address space the .bsp load then needs. At
the stock 512/256 a .bsp of just under 64 MB loads; at 1024 the same map can
die in Z_Malloc. So the real limit is a file size, not a vertex count.

The converter cannot know the file size while it is writing the .map - the
lightmaps and vis that dominate it do not exist yet. So the compile step
records what the .bsp actually weighed and how many baked prop vertices went
into it, and the next convert of that map uses those two numbers to aim at
BSP_TARGET_MB. Nothing to type, and a miss corrects itself on the next run.

The first convert of a map has no record and uses PROP_VERTEX_BUDGET.

    python bsp_budget.py record  <out-root> <name> <bsp> <manifest.json|count>
                                 [pairs] [lightmapscale]
    python bsp_budget.py show    <out-root> [name]
    python bsp_budget.py pairs   <bsp> [-v]
    python bsp_budget.py mark    <out-root> <name>
    python bsp_budget.py lmscale <out-root> <name>
"""
import json
import os
import struct
import sys

# Just under the 64 MB that loads at the stock com_hunkMegs 512/256, with room
# for the .bsp to grow a little between the size we measured and the next build.
BSP_TARGET_MB = 62.0

# What one more baked prop vertex costs in the .bsp: 80 bytes of RBSP drawVert,
# ~24 of indexes, and the lightmap luxels and drawsurf that come with it. Only
# used before a map has been compiled once; after that the measurement wins.
BYTES_PER_BAKED_VERT = 160

# Never move more than this much in one step, so a bad reading cannot produce a
# map that will not load - or one with nothing in it.
MAX_STEP = 2.0
FLOOR, CEILING = 20000, 400000

FILE = "cod4tosof2_budget.json"


def _path(out_root):
    return os.path.join(out_root, FILE)


def load(out_root):
    try:
        with open(_path(out_root), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


# The _lightmapscale ladder in compile_cod4_map.bat, in order.
LM_LADDER = ("1", "1.5", "2", "3", "4")
# Under this share of the target the next compile may try one step finer
# again, so a map that shrank gets its lightmap detail back.
LM_RECOVER = 0.70


def record(out_root, name, bsp_bytes, baked_verts, pairs=0, lmscale=""):
    """Called by compile_cod4_map.bat once the .bsp is finished.

    Merges, never replaces: the record also carries decisions (vl_terrain,
    lmscale) that must survive the next compile.
    """
    data = load(out_root)
    rec = dict(data.get(name) or {})
    rec.update({"bsp_bytes": int(bsp_bytes), "baked_verts": int(baked_verts),
                "shader_pairs": int(pairs)})
    if pairs > MAX_SHADER_PAIRS:
        rec["vl_terrain"] = True
    if lmscale:
        rec["lmscale"] = str(lmscale)
    data[name] = rec
    try:
        os.makedirs(out_root, exist_ok=True)
        with open(_path(out_root), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
    except Exception as e:
        print("could not record the .bsp size: %s" % e)
        return None
    return data[name]


def suggest(out_root, name, default):
    """(budget, explanation) for this map's next convert."""
    rec = load(out_root).get(name)
    if not rec or not rec.get("baked_verts") or not rec.get("bsp_bytes"):
        return default, ("baked prop budget: %d vertices (no compile measured yet; "
                         "the next compile sets it from the real .bsp size)" % default)

    had_bytes, had_verts = rec["bsp_bytes"], rec["baked_verts"]
    target = BSP_TARGET_MB * 1024 * 1024
    want = had_verts + (target - had_bytes) / float(BYTES_PER_BAKED_VERT)
    want = max(had_verts / MAX_STEP, min(had_verts * MAX_STEP, want))
    capped = want > CEILING
    want = int(max(FLOOR, min(CEILING, want)))
    return want, ("baked prop budget: %d vertices - last compile of this map made "
                  "%.1f MB from %d, and the target is %.0f MB%s"
                  % (want, had_bytes / 1048576.0, had_verts, BSP_TARGET_MB,
                     " (at the %d ceiling: this map would take more props than that "
                     "before it reaches the target size)" % CEILING if capped else ""))


# ---------------------------------------------------------------- MAX_SHADERS
# The renderer registers one shader_t per (shader, lightmap page) a surface
# uses, and they all share the MAX_SHADERS 2048 pool with the game's own
# menu, weapon, player and effect shaders. q3map2's "unique lightmap/shader
# combinations" line is NOT this number - it ran about half of it on both
# mp_strike (628 vs 1135) and mp_derail (1030 vs 2057), so counting it from
# the finished .bsp is the only honest measure.
#
# Measured: mp_strike at 1135 loads with every shader; mp_derail at 2057 hits
# MAX_SHADERS in the console, and an earlier mp_strike near 1900 left props
# without shaders. 1400 keeps room for the game's own registrations.
MAX_SHADER_PAIRS = 1400

RBSP_SURFACE = 148          # rbspDrawSurface_t
RBSP_SHADER = 72            # rbspShader_t (64-byte name + flags + contents)


def shader_pairs(bsp):
    """(pairs, pages, {shader name: pages it appears on}) from a finished .bsp."""
    with open(bsp, "rb") as fh:
        d = fh.read()
    if d[:4] != b"RBSP":
        raise ValueError("not an RBSP (SOF2) .bsp: %r" % d[:4])
    lumps = [struct.unpack_from("<ii", d, 8 + i * 8) for i in range(18)]
    sho, shl = lumps[1]
    suo, sul = lumps[13]
    names = [d[sho + i * RBSP_SHADER: sho + i * RBSP_SHADER + 64].split(b"\0")[0]
             .decode("latin1") for i in range(shl // RBSP_SHADER)]
    pairs = set()
    pages = set()
    per = {}
    for i in range(sul // RBSP_SURFACE):
        o = suo + i * RBSP_SURFACE
        sh = struct.unpack_from("<i", d, o)[0]
        used = [k for k in struct.unpack_from("<4i", d, o + 36) if k >= 0]
        nm = names[sh] if 0 <= sh < len(names) else "?%d" % sh
        for k in used or [-1]:
            pairs.add((sh, k))
            per.setdefault(nm, set()).add(k)
            if k >= 0:
                pages.add(k)
    return len(pairs), len(pages), {k: len(v) for k, v in per.items()}


def start_lmscale(out_root, name):
    """Where the next compile of this map should start on the _lightmapscale
    ladder.

    Every step of that ladder costs a whole rebuild, and vis is the expensive
    part: mp_strike spent 4 x 570 seconds on vis - all four passes identical,
    because _lightmapscale changes nothing vis cares about - to walk 1, 1.5,
    2, 3 and arrive at 3. Starting from what worked last time makes that one
    pass instead of four. If the last compile came in comfortably under the
    target the start moves one step finer again, so a map that shrank wins its
    lightmap detail back rather than staying coarse for ever.
    """
    rec = load(out_root).get(name) or {}
    last = str(rec.get("lmscale") or "")
    if last not in LM_LADDER:
        return "1"
    i = LM_LADDER.index(last)
    pairs = int(rec.get("shader_pairs") or 0)
    if i > 0 and 0 < pairs < MAX_SHADER_PAIRS * LM_RECOVER:
        return LM_LADDER[i - 1]
    return last


def vertex_light_terrain(out_root, name):
    """True once a compile of this map has gone over MAX_SHADER_PAIRS.

    The baked terrain under CoD's blend layers is the single biggest consumer:
    on mp_derail its "_bsp" shaders held 669 of 2262 pairs and touched 322 of
    359 lightmap pages, 154 of them theirs alone. Dropping their lightmaps
    costs less detail than it sounds - those control grids average 61 units
    between points, so vertex lighting lands near the 48-unit luxels they had
    and well inside the 96 the _lightmapscale ladder would otherwise reach.
    """
    rec = load(out_root).get(name) or {}
    # Sticky. The decision cannot be re-derived from the latest pair count,
    # because once the terrain IS vertex-lit the count comes in under target -
    # reading it live made the setting oscillate, lightmapped and over budget
    # one convert, vertex-lit and under it the next. A map that needed this
    # keeps it until --no-vertex-light-terrain says otherwise.
    if rec.get("vl_terrain"):
        return True
    return int(rec.get("shader_pairs") or 0) > MAX_SHADER_PAIRS


def mark_terrain_vertex_lit(out_root, name):
    """Persist the decision the shader step just made, so it holds."""
    data = load(out_root)
    rec = dict(data.get(name) or {})
    if rec.get("vl_terrain"):
        return True
    rec["vl_terrain"] = True
    data[name] = rec
    try:
        os.makedirs(out_root, exist_ok=True)
        with open(_path(out_root), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
    except Exception:
        return False
    return True


def main(argv):
    if len(argv) >= 3 and argv[1] == "pairs":
        try:
            n, pages, per = shader_pairs(argv[2])
        except Exception as e:
            print(0)
            sys.stderr.write("could not count shader pairs: %s\n" % e)
            return 0
        print(n)                                  # the .bat reads this line
        if "-v" in argv:
            sys.stderr.write("%d (shader, lightmap) pairs over %d lightmap pages, "
                             "%d shaders\n" % (n, pages, len(per)))
            for nm, c in sorted(per.items(), key=lambda kv: -kv[1])[:10]:
                sys.stderr.write("  %4d pages  %s\n" % (c, nm))
        return 0
    if len(argv) >= 6 and argv[1] == "record":
        out_root, name, bsp, src = argv[2], argv[3], argv[4], argv[5]
        if os.path.isfile(src):                     # the converter's manifest
            try:
                with open(src, encoding="utf-8") as fh:
                    n = int(json.load(fh).get("baked_prop_vertices") or 0)
            except Exception as e:
                return "cannot read %s: %s" % (src, e)
            if not n:
                print("no baked prop count in %s - nothing to record" % os.path.basename(src))
                return 0
        else:
            n = int(src)
        try:
            size = os.path.getsize(bsp)
        except OSError as e:
            return "cannot measure %s: %s" % (bsp, e)
        pairs = 0
        if len(argv) >= 7:
            try:
                pairs = int(argv[6])
            except ValueError:
                pairs = 0
        r = record(out_root, name, size, n, pairs, argv[7] if len(argv) >= 8 else "")
        if r:
            nxt, why = suggest(out_root, name, n)
            print("recorded: %.1f MB from %d baked prop vertices" % (size / 1048576.0, n))
            print("next convert of %s will use %d" % (name, nxt))
            if pairs > MAX_SHADER_PAIRS:
                print("%d shader/lightmap pairs is over %d - the next convert will make the "
                      "baked terrain vertex-lit" % (pairs, MAX_SHADER_PAIRS))
        return 0
    if len(argv) >= 4 and argv[1] == "lmscale":
        # one bare token for the .bat to read back with "set /p"
        print(start_lmscale(argv[2], argv[3]))
        return 0
    if len(argv) >= 4 and argv[1] == "mark":
        # for a record written before the decision was stored: its pair count
        # was measured WITH the terrain already vertex-lit, so it reads as
        # under budget and nothing can infer the decision from it
        ok = mark_terrain_vertex_lit(argv[2], argv[3])
        print("%s: baked terrain set to vertex-lit%s"
              % (argv[3], "" if ok else " - COULD NOT WRITE"))
        return 0 if ok else 1
    if len(argv) >= 3 and argv[1] == "show":
        data = load(argv[2])
        for k in sorted(data):
            v = data[k]
            print("%-20s %.1f MB  %d baked verts" % (k, v["bsp_bytes"] / 1048576.0,
                                                     v["baked_verts"]))
        if not data:
            print("nothing recorded yet in", _path(argv[2]))
        return 0
    return __doc__.strip().splitlines()[-2].strip()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
