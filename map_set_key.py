"""
map_set_key.py <map> <key> <value>  - set (or replace) a worldspawn key in an
idTech3 .map, in place. Used by compile_cod4_map.bat to raise
"_lightmapscale" when a map has too many lightmap/shader pairs (each pair is a
renderer shader; SOF2 has 2048 in all). q3map2 fixes every surface's lightmap
resolution in -bsp, so this has to go into the .map, not onto the -light line.
"""
import re
import sys


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    path, key, value = sys.argv[1:]
    text = open(path, encoding="latin-1").read()
    m = re.search(r'\{\s*\n"classname" "worldspawn"\n', text)
    if not m:
        sys.exit("no worldspawn in " + path)
    end = text.index("\n{", m.end()) if "\n{" in text[m.end():] else len(text)
    head = text[m.start():end]
    kv = re.compile(r'^"%s" "[^"]*"\n' % re.escape(key), re.M)
    if kv.search(head):
        head = kv.sub('"%s" "%s"\n' % (key, value), head, count=1)
    else:
        head = head.replace('"classname" "worldspawn"\n', '"classname" "worldspawn"\n"%s" "%s"\n' % (key, value), 1)
    text = text[:m.start()] + head + text[end:]
    with open(path, "w", encoding="latin-1", newline="\n") as fh:
        fh.write(text)
    print('%s: worldspawn "%s" "%s"' % (path, key, value))


if __name__ == "__main__":
    main()
