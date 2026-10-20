"""
lod_test.py <SOF2 base folder> <model path or folder> [--undo]

  e.g.  lod_test.py "E:\\...\\Soldier of Fortune II - MODDING\\base" models/cod4/cells
        (a folder = every .md3 in it: all of a map's combined prop models)

Finds out how SOF2 picks lower-detail versions of an .md3. It writes
coloured copies of the model under every naming scheme SOF2 might use:

    models1/<path>.md3 ... models4/<path>.md3   red, green, blue, yellow
    <path>_1.md3, <path>_2.md3                  magenta, cyan   (Quake 3 style)

plus shaders/lodtest.shader (added to shaderlist.txt). Start the map,
look at that model and walk away from it / change r_lodbias:
  - turns red/green/blue/yellow while walking away -> SOF2 uses the
    modelsN folders by distance
  - turns magenta/cyan while walking away         -> Quake 3 style _N files
  - colour only changes with r_lodbias + vid_restart (or never) -> the
    folders are a detail setting, not distance LOD
  - stays textured                                -> neither is loaded
--undo removes everything it wrote.
"""
import os
import struct
import sys

COLOURS = [("models1", None, "red", "1 0 0"), ("models2", None, "green", "0 1 0"),
           ("models3", None, "blue", "0 0 1"), ("models4", None, "yellow", "1 1 0"),
           (None, "_1", "magenta", "1 0 1"), (None, "_2", "cyan", "0 1 1")]
SHADER_FILE = "lodtest"


def recolour(data, shader):
    """Copy of an MD3 with every surface's shader name replaced."""
    buf = bytearray(data)
    if buf[:4] != b"IDP3":
        sys.exit("not an MD3 file")
    num_surf, ofs_surf = struct.unpack_from("<i", buf, 84)[0], struct.unpack_from("<i", buf, 100)[0]
    name = shader.encode("ascii").ljust(64, b"\0")
    p = ofs_surf
    for _ in range(num_surf):
        num_shaders = struct.unpack_from("<i", buf, p + 76)[0]
        ofs_shaders = struct.unpack_from("<i", buf, p + 92)[0]
        for k in range(num_shaders):
            q = p + ofs_shaders + k * 68
            buf[q:q + 64] = name
            struct.pack_into("<i", buf, q + 64, 0)
        p += struct.unpack_from("<i", buf, p + 104)[0]
    return bytes(buf)


def targets(base, rel):
    stem, ext = os.path.splitext(rel)
    sub = rel.split("/", 1)[1]                       # path after "models/"
    for folder, suffix, colour, rgb in COLOURS:
        path = (folder + "/" + sub) if folder else (stem + suffix + ext)
        yield os.path.join(base, *path.split("/")), colour, rgb


def shaderlist(base, add):
    p = os.path.join(base, "shaders", "shaderlist.txt")
    lines = open(p).read().splitlines() if os.path.exists(p) else []
    has = any(l.strip().lower() == SHADER_FILE for l in lines)
    if add and not has:
        lines.append(SHADER_FILE)
    elif not add and has:
        lines = [l for l in lines if l.strip().lower() != SHADER_FILE]
    else:
        return
    open(p, "w").write("\n".join(lines) + "\n")


def model_list(base, rel):
    d = os.path.join(base, *rel.split("/"))
    if not os.path.isdir(d):
        return [rel]
    return [rel.rstrip("/") + "/" + f for f in sorted(os.listdir(d))
            if f.lower().endswith(".md3") and not f[:-4].endswith(("_1", "_2"))]


def main():
    args = [a for a in sys.argv[1:] if a != "--undo"]
    if len(args) != 2:
        sys.exit(__doc__)
    base, rel = args[0], args[1].replace("\\", "/").strip("/")
    if not rel.lower().startswith("models/"):
        sys.exit("model path must start with models/")
    models = model_list(base, rel)
    if "--undo" in sys.argv:
        n = 0
        for m in models:
            for path, _, _ in targets(base, m):
                if os.path.exists(path):
                    os.remove(path)
                    n += 1
        sh = os.path.join(base, "shaders", SHADER_FILE + ".shader")
        if os.path.exists(sh):
            os.remove(sh)
        shaderlist(base, False)
        print("removed %d test files" % n)
        return
    for m in models:
        data = open(os.path.join(base, *m.split("/")), "rb").read()
        for path, colour, rgb in targets(base, m):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(recolour(data, "lodtest/" + colour))
    print("%d model(s): coloured copies written (models1-4 red/green/blue/yellow, _1/_2 magenta/cyan)" % len(models))
    blocks = ["lodtest/%s\n{\n\tnopicmip\n\t{\n\t\tmap $whiteimage\n\t\trgbGen const ( %s )\n\t}\n}\n"
              % (colour, rgb) for _, _, colour, rgb in COLOURS]
    open(os.path.join(base, "shaders", SHADER_FILE + ".shader"), "w").write("\n".join(blocks))
    shaderlist(base, True)
    print("Start the map and walk away from some props. Afterwards run the same command with --undo")


if __name__ == "__main__":
    main()
