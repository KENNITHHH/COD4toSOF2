#!/usr/bin/env python3
"""
cod4_collect_assets.py - gather exactly the CoD4 (IW3) raw assets a Radiant
.map needs for the CoD4 -> SOF2 pipeline, into one zip.

Standard library only. Windows / Linux.

What it does
  1. Reads the .map and recursively inlines every misc_prefab reference
     (case-insensitive lookup, like Windows) to find every xmodel and every
     brush/patch material the level actually uses.
  2. Indexes every root you pass: loose files AND the contents of .iwd
     archives (iwd = zip; stock .iwi textures live in main/iw_XX.iwd).
  3. Follows the dependency chain by scanning each binary asset for names:
        xmodel/<m>        -> xmodelparts/*, xmodelsurfs/*, materials/*
        materials/<m>     -> images/*.iwi
     (names inside these v25 binaries are plain ASCII strings, so a string
     scan matched against the index is reliable without a full parser.)
  4. Pulls the source_data/*.gdt files that define any of those materials
     or xmodels (text: colorMap, alphaTest, surfaceType, LOD distances ...).
  5. Writes <out>/cod4_assets.zip + <out>/report.txt, and with --list a full
     listing of every file under the roots incl. iwd contents.

Usage (Windows example)
  python cod4_collect_assets.py ^
      --map "C:\\Games\\COD4\\map_source\\mp_shipment.map" ^
      --root "C:\\Games\\COD4" --root "C:\\Games\\COD4\\ModTools" ^
      --out "C:\\temp\\cod4_collect" --list

  --prefabs  folder that CONTAINS 'prefabs/' (default: auto, walks up from
             the .map and checks map_source/ under each root)
"""
import argparse
import os
import re
import sys
import zipfile
from collections import defaultdict

ASSET_DIRS = ("xmodel", "xmodelparts", "xmodelsurfs", "materials", "images",
              "techsets", "techniques", "source_data", "model_export",
              "xmodel_export", "material_properties", "statemaps",
              # CoD reads vision/<name>.vision through the normal filesystem,
              # so on a stock install it is a member of one of main\iw_XX.iwd
              # rather than a rawfile in the map's .ff or a loose raw\ file.
              # Indexing the directory is what makes it findable at all.
              "vision")

# brush/patch materials that are compiler tools - no asset to fetch
TOOL_MATERIALS = {
    "caulk", "clip", "clip_weap", "clip_nosight", "clip_nosight_metal",
    "clip_nosight_nothing", "clip_player", "clip_metal", "clip_wood",
    "trigger", "nodraw_decal", "nodraw", "origin", "lightgrid_volume",
    "mantle_on", "mantle_over", "portal", "portal_nodraw", "hint", "skip",
    "ladder", "lightmap_gray", "shadowcaster",
}

PRINTABLE_RE = re.compile(rb"[\x20-\x7e]{3,}")


# --------------------------------------------------------------------------
# 1. map + prefab closure
# --------------------------------------------------------------------------
RE_MODEL = re.compile(r'"model"\s+"([^"]+)"')
RE_FACE_MAT = re.compile(r'^\s*\(.*?\)\s*\(.*?\)\s*\(.*?\)\s+(\S+)\s+-?[\d.]+\s+-?[\d.]+', re.M)
RE_PATCH_MAT = re.compile(
    r'^\s+(?:mesh|curve)\s*\n\s*\{\s*\n(?:\s*(?:contents|toolFlags|smoothing)[^\n]*\n)*\s*(\S+)\s*\n',
    re.M)


def find_prefab_root(map_path, roots):
    d = os.path.dirname(os.path.abspath(map_path))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "prefabs")):
            return d
        d = os.path.dirname(d)
    for r in roots:
        for cand in (os.path.join(r, "map_source"), r):
            if os.path.isdir(os.path.join(cand, "prefabs")):
                return cand
    return None


def map_closure(map_path, prefab_root):
    idx = {}
    if prefab_root:
        base = os.path.join(prefab_root, "prefabs")
        for d, _, fs in os.walk(base):
            for f in fs:
                full = os.path.join(d, f)
                rel = os.path.relpath(full, prefab_root).replace("\\", "/").lower()
                idx[rel] = full
    xmodels, materials, missing_prefabs = set(), set(), set()
    seen = set()
    stack = [os.path.abspath(map_path)]
    nprefab = 0
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        with open(p, "r", errors="ignore") as fh:
            text = fh.read()
        for m in RE_MODEL.findall(text):
            if m.lower().startswith("prefabs/") and m.lower().endswith(".map"):
                hit = idx.get(m.replace("\\", "/").lower())
                if hit:
                    stack.append(hit)
                    nprefab += 1
                else:
                    missing_prefabs.add(m)
            elif "/" not in m and "\\" not in m and not m.startswith("*"):
                xmodels.add(m)
        materials.update(RE_FACE_MAT.findall(text))
        materials.update(RE_PATCH_MAT.findall(text))
    materials = {m for m in materials if m.lower() not in TOOL_MATERIALS}
    return xmodels, materials, missing_prefabs, len(seen) - 1


# --------------------------------------------------------------------------
# 2. index roots (loose + iwd)
# --------------------------------------------------------------------------
class Entry:
    __slots__ = ("src", "member", "size")

    def __init__(self, src, member, size):
        self.src, self.member, self.size = src, member, size

    _zips = {}

    def read(self):
        if self.member is None:
            with open(self.src, "rb") as fh:
                return fh.read()
        z = Entry._zips.get(self.src)
        if z is None:
            z = Entry._zips[self.src] = zipfile.ZipFile(self.src)
        return z.read(self.member)

    def where(self):
        return self.src if self.member is None else "%s :: %s" % (self.src, self.member)


def asset_key(relpath):
    """'raw/materials/wc@foo' -> 'materials/wc@foo' (lowercase, '/')."""
    parts = relpath.replace("\\", "/").split("/")
    low = [x.lower() for x in parts]
    for i, comp in enumerate(low):
        if comp in ASSET_DIRS and i < len(parts) - 1:
            return "/".join(low[i:])
    return None


def short_name(key_rest):
    """material/xmodel names may carry 'wc@', 'mc@', 'wc/' style prefixes."""
    n = key_rest.rsplit("/", 1)[-1]
    if "@" in n:
        n = n.split("@", 1)[1]
    return n


def build_index(roots, listing, exclude=()):
    index = {}            # asset key -> Entry
    by_short = defaultdict(list)   # (dir, shortname) -> [key]
    dupes = 0
    excl = [os.path.normcase(os.path.abspath(e)) for e in exclude]
    for root in roots:
        for d, dirs, fs in os.walk(root):
            # never index our own output: with the output folder inside one of
            # the roots (the usual setup), those loose copies would win over
            # the .iwd originals and be read back while being rewritten,
            # leaving 0-byte files
            here = os.path.normcase(os.path.abspath(d))
            if any(here == e or here.startswith(e + os.sep) for e in excl) or \
                    os.path.basename(d).lower() == "_cache":
                dirs[:] = []
                continue
            for f in fs:
                full = os.path.join(d, f)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root)
                if listing is not None:
                    listing.append("%12d  %s" % (size, full))
                entries = []
                if f.lower().endswith((".iwd", ".zip")) and size > 0:
                    try:
                        with zipfile.ZipFile(full) as z:
                            for zi in z.infolist():
                                if zi.is_dir():
                                    continue
                                if listing is not None:
                                    listing.append("%12d  %s :: %s" % (zi.file_size, full, zi.filename))
                                entries.append((zi.filename, Entry(full, zi.filename, zi.file_size)))
                    except zipfile.BadZipFile:
                        pass
                entries.append((rel, Entry(full, None, size)))
                for relname, ent in entries:
                    key = asset_key(relname)
                    if not key:
                        continue
                    if key in index:
                        dupes += 1
                        # loose files beat iwd members, first root wins otherwise
                        if index[key].member is not None and ent.member is None:
                            index[key] = ent
                        continue
                    index[key] = ent
                    top, rest = key.split("/", 1)
                    by_short[(top, short_name(rest))].append(key)
    return index, by_short, dupes


# --------------------------------------------------------------------------
# 3. dependency walk
# --------------------------------------------------------------------------
def strings_of(data):
    out = set()
    for s in PRINTABLE_RE.findall(data):
        s = s.decode("ascii", "ignore").strip().lower().replace("\\", "/")
        if s:
            out.add(s)
            out.add(s.rsplit("/", 1)[-1])
    return out


def lookup(index, by_short, top, name):
    name = name.lower().replace("\\", "/")
    k = "%s/%s" % (top, name)
    if k in index:
        return [k]
    if top == "images" and not name.endswith(".iwi"):
        k2 = k + ".iwi"
        if k2 in index:
            return [k2]
    hits = by_short.get((top, short_name(name)), [])
    if not hits and top == "images":
        hits = by_short.get((top, short_name(name) + ".iwi"), [])
    return list(hits)


def xmodel_lods(data):
    """Exact v25 header parse (KisakCOD XModelLoadConfigFile + coll data +
    per-LOD surf/material lists). Returns ([lod surf files], [materials]).
    The LOD filenames sit right after a float distance, so a plain string
    scan glues the float's last byte onto the name - hence a real parse."""
    import struct
    p = 0
    if struct.unpack_from("<h", data, p)[0] != 25:
        return None
    p = 2 + 1 + 24
    p = data.index(b"\0", p) + 1                      # physics preset
    files = []
    for _ in range(4):
        p += 4                                        # dist
        e = data.index(b"\0", p)
        files.append(data[p:e].decode("latin-1"))
        p = e + 1
    p += 4                                            # collLod
    ncs = struct.unpack_from("<i", data, p)[0]
    p += 4
    for _ in range(ncs):
        nt = struct.unpack_from("<i", data, p)[0]
        p += 4 + nt * 48 + 36
    mats = []
    for f in files:
        if not f:
            continue
        n = struct.unpack_from("<H", data, p)[0]
        p += 2
        for _ in range(n):
            e = data.index(b"\0", p)
            mats.append(data[p:e].decode("latin-1"))
            p = e + 1
    return [f for f in files if f], mats


def walk(index, by_short, xmodels, materials):
    picked = {}
    missing = defaultdict(set)
    mat_queue = set(m.lower() for m in materials)
    for xm in sorted(xmodels):
        keys = lookup(index, by_short, "xmodel", xm)
        if not keys:
            missing["xmodel"].add(xm)
            continue
        for k in keys:
            picked[k] = index[k]
            data = index[k].read()
            parsed = None
            try:
                parsed = xmodel_lods(data)
            except (ValueError, IndexError, Exception):
                parsed = None
            if parsed:
                files, mats = parsed
                for i, f in enumerate(files):
                    hits = lookup(index, by_short, "xmodelsurfs", f)
                    if not hits:
                        missing["xmodelsurfs"].add("%s (%s LOD%d)" % (f, xm, i))
                    for kk in hits:
                        picked[kk] = index[kk]
                # xmodelparts is named after the LOD0 surf file; static
                # single-bone models usually have none, which is fine
                for kk in lookup(index, by_short, "xmodelparts", files[0]):
                    picked[kk] = index[kk]
                for mname in mats:
                    if not lookup(index, by_short, "materials", mname):
                        missing["materials"].add(mname + " (model " + xm + ")")
                    mat_queue.add(mname.lower())
                continue
            # unknown version: fall back to string scan
            for s in strings_of(data):
                for top in ("xmodelparts", "xmodelsurfs"):
                    for kk in lookup(index, by_short, top, s):
                        picked[kk] = index[kk]
                if lookup(index, by_short, "materials", s):
                    mat_queue.add(s)
    # xmodelsurfs also name their materials in some builds - scan them too
    for k in [k for k in picked if k.startswith("xmodelsurfs/")]:
        for s in strings_of(picked[k].read()):
            if lookup(index, by_short, "materials", s):
                mat_queue.add(s)
    images_wanted = set()
    for m in sorted(mat_queue):
        keys = lookup(index, by_short, "materials", m)
        if not keys:
            if m in (x.lower() for x in materials):
                missing["materials"].add(m)
            continue
        for k in keys:
            picked[k] = index[k]
            for s in strings_of(index[k].read()):
                for kk in lookup(index, by_short, "images", s):
                    picked[kk] = index[kk]
                    images_wanted.add(kk)
    return picked, missing, mat_queue


def collect_gdts(index, names):
    """source_data/*.gdt entries defining any of `names`."""
    names = {n.lower() for n in names}
    hits = {}
    pat = re.compile(r'"([^"]+)"\s*\(\s*"([^"]+\.gdf)"\s*\)')
    for k, ent in index.items():
        if not (k.startswith("source_data/") and k.endswith(".gdt")):
            continue
        try:
            text = ent.read().decode("latin-1", "ignore")
        except Exception:
            continue
        found = [n for n, gdf in pat.findall(text) if n.lower() in names
                 or short_name(n.lower()) in names]
        if found:
            hits[k] = (ent, found)
    return hits


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", required=True)
    ap.add_argument("--root", action="append", required=True,
                    help="COD4 game folder and/or ModTools folder (repeatable)")
    ap.add_argument("--prefabs", help="folder containing prefabs/ (default: auto)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", action="store_true", help="also write full file listing incl. iwd contents")
    ap.add_argument("--extract", action="store_true",
                    help="write the assets as plain files under <out> (materials/, images/, xmodel*/...) "
                         "instead of cod4_assets.zip - what the converter reads directly")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    prefab_root = a.prefabs or find_prefab_root(a.map, a.root)
    print("prefab root:", prefab_root or "(not found - prefabs will not be expanded!)")
    xmodels, materials, miss_pf, npf = map_closure(a.map, prefab_root)
    print("map closure: %d prefab files, %d xmodels, %d materials, %d prefabs missing"
          % (npf, len(xmodels), len(materials), len(miss_pf)))

    listing = [] if a.list else None
    print("indexing roots (incl. .iwd contents) ...")
    out_abs = os.path.abspath(a.out)
    index, by_short, dupes = build_index(a.root, listing,
                                         exclude=[out_abs, os.path.dirname(out_abs)])
    counts = defaultdict(int)
    for k in index:
        counts[k.split("/", 1)[0]] += 1
    print("indexed:", dict(counts), "(%d duplicate keys)" % dupes)
    if listing is not None:
        with open(os.path.join(a.out, "file_list.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(listing))

    picked, missing, all_mats = walk(index, by_short, xmodels, materials)
    gdts = collect_gdts(index, set(xmodels) | all_mats)

    zpath = os.path.join(a.out, "cod4_assets.zip")
    if a.extract:
        zpath = a.out
        for k, ent in sorted(picked.items()) + [(k, e) for k, (e, _) in sorted(gdts.items())]:
            dst = os.path.join(a.out, *k.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(ent.read())
    else:
      with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for k, ent in sorted(picked.items()):
            z.writestr(k, ent.read())
        for k, (ent, _) in sorted(gdts.items()):
            z.writestr(k, ent.read())
        # the map + prefabs themselves, so the pipeline side has one package
        z.write(a.map, "map_source/" + os.path.basename(a.map))

    # the map's own scripts from its fastfile (zone/<lang>/<map>.ff): the fog
    # (setExpFog) is in maps/createart/<map>_art.gsc, which ships only there
    raw_out = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import cod4_ff
        mname = os.path.splitext(os.path.basename(a.map))[0]
        ff = cod4_ff.find_map_ff(a.root, mname)
        got = {}
        if True:
            want = ["maps/createart/%s_art.gsc" % mname, "maps/mp/%s.gsc" % mname]
            zone = cod4_ff.zone_data(ff) if ff else b""
            got = cod4_ff.raw_files(zone, want) if ff else {}
            # The vision file holds CoD's colour grading (r_filmTweak*). It is
            # not a detail: mp_strike's worldspawn sun is "0.5 0.6 1.0", a blue
            # sun, and the warm image CoD shows is that blue light run through
            # this file's film tint as a post-process. SOF2 has no such pass,
            # so the tint has to be folded into the baked light instead.
            # VisionSetNaked() in the art script names the vision to use; it is
            # normally the map's own name.
            vis = set(re.findall(r'(?i)\bvisionset\w*\s*\(\s*"([^"]+)"', "\n".join(got.values())))
            vis.add(mname)
            vwant = ["vision/%s.vision" % v for v in sorted(vis)]
            got.update(cod4_ff.raw_files(zone, vwant))
            if ff and not any(n.endswith(".vision") for n in got):
                # stock maps keep their vision in a shared fastfile.
                # Guarded on ff: a map with no fastfile of its own (mp_derail
                # on this install) reached os.path.dirname(None) here and the
                # whole collect reported "could not read map scripts", which
                # hid the real reason - there is no .ff to read.
                zdir = os.path.dirname(ff)
                for other in sorted(os.listdir(zdir))[:40]:
                    low = other.lower()
                    if not low.endswith(".ff") or not low.startswith(("common", "code_post_gfx")):
                        continue
                    try:
                        got.update(cod4_ff.raw_files(cod4_ff.zone_data(os.path.join(zdir, other)), vwant))
                    except Exception:
                        continue
                    if any(n.endswith(".vision") for n in got):
                        break
            if not any(n.endswith(".vision") for n in got):
                # and failing that, the indexed roots - which is where it
                # actually lives on a stock install (inside main\iw_XX.iwd)
                for want_v in vwant:
                    ent = index.get(want_v)
                    if ent:
                        got[want_v] = ent.read().decode("latin-1", "replace")
                        break
            for name, text in got.items():
                dst = os.path.join(a.out, "raw", *name.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w", encoding="latin-1", newline="") as fh:
                    fh.write(text)
                raw_out.append(name)
            if ff:
                print("fastfile %s: %s" % (ff, ", ".join(raw_out) or "no map scripts found"))
            else:
                print("fastfile: no zone/<lang>/%s.ff under the roots; %s"
                      % (mname, ", ".join(raw_out) + " came from the indexed roots instead"
                         if raw_out else "no art script or vision file found, so no fog and "
                         "no colour grading for this map"))
    except Exception as e:                       # never fail the collect over this
        print("fastfile: could not read map scripts (%s)" % e)

    by_top = defaultdict(int)
    for k in picked:
        by_top[k.split("/", 1)[0]] += 1
    with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write("map: %s\nprefab root: %s\nroots: %s\n\n" % (a.map, prefab_root, a.root))
        fh.write("indexed asset counts: %s\n" % dict(counts))
        fh.write("collected: %s, gdt files: %d\n\n" % (dict(by_top), len(gdts)))
        for top in ("xmodel", "xmodelsurfs", "materials"):
            fh.write("MISSING %s (%d):\n" % (top, len(missing[top])))
            for n in sorted(missing[top]):
                fh.write("  %s\n" % n)
            fh.write("\n")
        fh.write("MISSING prefabs (%d):\n" % len(miss_pf))
        for n in sorted(miss_pf):
            fh.write("  %s\n" % n)
        fh.write("\nCOLLECTED FILES:\n")
        for k, ent in sorted(picked.items()):
            fh.write("  %-60s <- %s\n" % (k, ent.where()))
        for k, (ent, names) in sorted(gdts.items()):
            fh.write("  %-60s <- %s  [%d defs]\n" % (k, ent.where(), len(names)))

    print("collected:", dict(by_top), "gdt:", len(gdts))
    print("missing xmodels: %d, missing xmodelsurfs: %d, missing materials: %d"
          % (len(missing["xmodel"]), len(missing["xmodelsurfs"]), len(missing["materials"])))
    print("->", zpath)
    print("->", os.path.join(a.out, "report.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
