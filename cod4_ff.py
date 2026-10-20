"""
cod4_ff.py - read raw script files (.gsc / .csv) out of a CoD4 PC fastfile.

  python cod4_ff.py <zone/english/mp_strike.ff> [name ...] [--out DIR]

A CoD4 PC fastfile is "IWffu100" + a 4-byte version + one zlib stream
holding the zone. Raw files (asset type rawfile: the map's .gsc scripts,
maps/createart/<map>_art.gsc with the fog settings, ...) are stored in it as
their name, a 0 byte, then the plain text, ending with another 0 byte - so
they can be found by name without parsing the whole zone.

Used by cod4_collect_assets.py: mp_strike's fog (setExpFog) lives only in
maps/createart/mp_strike_art.gsc inside mp_strike.ff, not in raw/.
"""
import argparse
import os
import re
import sys
import zlib


def zone_data(ff_path):
    with open(ff_path, "rb") as fh:
        head = fh.read(12)
        if not head.startswith(b"IWffu100"):
            raise ValueError("not a CoD4 PC fastfile (IWffu100): " + ff_path)
        d = zlib.decompressobj()
        out = []
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            out.append(d.decompress(chunk))
            if d.eof:
                break
        out.append(d.flush())
    return b"".join(out)


def raw_files(zone, names=None):
    """{name: text} for raw files whose names are in `names` (or every
    .gsc/.csv/.cfg name found when names is None)."""
    found = {}
    if names is None:
        names = set(m.group(1).decode("latin-1") for m in re.finditer(
            rb"((?:maps|mp|ui_mp|clientscripts|vision)/[\w/\-]+\.(?:gsc|csv|cfg|vision))\x00", zone))
    for name in names:
        key = name.encode("latin-1") + b"\x00"
        i = zone.find(key)
        while i >= 0:
            j = zone.find(b"\x00", i + len(key))
            text = zone[i + len(key):j if j >= 0 else len(zone)]
            # a real raw file body is text; a bare name reference is not followed by one
            if len(text) > 8 and all(32 <= c < 127 or c in (9, 10, 13) for c in text[:256]):
                found[name] = text.decode("latin-1")
                break
            i = zone.find(key, i + 1)
    return found


def find_map_ff(roots, map_name):
    for r in roots:
        for sub in ("zone", os.path.join("..", "zone")):
            zd = os.path.join(r, sub)
            if not os.path.isdir(zd):
                continue
            for lang in sorted(os.listdir(zd)):
                p = os.path.join(zd, lang, map_name + ".ff")
                if os.path.isfile(p):
                    return p
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ff")
    ap.add_argument("names", nargs="*", help="raw file names (default: list them)")
    ap.add_argument("--out", help="write the files under this folder")
    a = ap.parse_args()
    files = raw_files(zone_data(a.ff), a.names or None)
    for name, text in sorted(files.items()):
        if a.out:
            dst = os.path.join(a.out, *name.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="latin-1", newline="") as fh:
                fh.write(text)
            print("->", dst)
        else:
            print("%-50s %d bytes" % (name, len(text)))
    if not files:
        print("no raw files found")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
