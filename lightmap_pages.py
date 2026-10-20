"""
lightmap_pages.py <shaderfile> <size>  - give every lightmapped shader in an
installed .shader file bigger lightmap pages ("q3map_lightmapSize <size> <size>")
and an explicit "tcGen lightmap" on its $lightmap stage.

Why: every shader x lightmap page pair is its own renderer shader, and SOF2
has 2048 in all (mp_strike: 1029 pairs at the internal 128x128 pages - props
lost their shaders). With a custom page size q3map2 -light writes the pages as
maps/<map>/lm_NNNN.tga and one generated shader per pair into
shaders/q3map2_<map>.shader (the page as a normal image stage), so the same
lighting needs far fewer pairs: mp_shipment 136 -> 58 at 512x512. q3map2 only
text-replaces "$lightmap" with the image name, so the stage must say
"tcGen lightmap" itself (harmless for internal lightmaps).

Only -light needs to run again after this; the lightmap size is read there.
"""
import re
import sys


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    path, size = sys.argv[1], int(sys.argv[2])
    text = open(path, encoding="latin-1").read()
    out, n, pos = [], 0, 0
    # shader blocks: "name\n{ ... }" at top level
    for m in re.finditer(r"^(\S[^\n]*)\n\{\n(.*?)\n\}", text, re.S | re.M):
        body = m.group(2)
        if "$lightmap" not in body:
            continue
        body2 = re.sub(r"^\tq3map_lightmapSize[^\n]*\n", "", body + "\n", flags=re.M | re.I).rstrip("\n")
        body2 = "\tq3map_lightmapSize %d %d\n" % (size, size) + body2
        body2 = re.sub(r"(\n\t\tmap \$lightmap\n)(?!\t\ttcGen lightmap)", r"\1\t\ttcGen lightmap\n", body2)
        out.append(text[pos:m.start(2)])
        out.append(body2)
        pos = m.end(2)
        n += 1
    out.append(text[pos:])
    with open(path, "w", encoding="latin-1", newline="\n") as fh:
        fh.write("".join(out))
    print("%s: %d lightmapped shaders -> %dx%d lightmap pages" % (path, n, size, size))


if __name__ == "__main__":
    main()
