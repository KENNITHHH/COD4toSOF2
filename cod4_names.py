"""
Shader names shared by iwmap2map.py (writes them into the .map) and
cod4mat2shader.py (defines them), so both always agree.

World shaders are per map: textures/cod4/<tag>/<material>. With one shared
textures/cod4/<material> namespace every converted map's .shader file defined
the same names, and q3map2 uses the FIRST definition it reads: mp_strike
compiled against mp_backlot's / mp_derail's older copies (shaderlist order),
so its own lightmap, _far and _nonsolid settings were silently ignored for
every material the maps share. Texture images stay shared in textures/cod4/.

Names are kept under idTech3's 64-character MAX_QPATH, with room for the
suffixes the pipeline adds later ("_nonsolid", "_far", q3map2's "_bsp"); a
longer material name is shortened with a short hash so it stays unique.
"""
import hashlib
import re

MAX_NAME = 63
WORLD_SUFFIX_ROOM = len("_nonsolid_far")


def safe(name):
    return re.sub(r"[^a-z0-9_/\-]", "_", name.lower())


def _hash(s, n):
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:n]


def map_tag(map_name):
    """mp_strike -> strike, mp_backlot_geo -> backlot_geo (long names hashed)."""
    t = safe(map_name).replace("/", "_")
    if t.startswith("mp_"):
        t = t[3:]
    if len(t) > 12:
        t = t[:8] + "_" + _hash(t, 3)
    return t or "map"


def _fit(name, room):
    if len(name) <= room:
        return name
    return name[:room - 7] + "_" + _hash(name, 6)


def world_shader(tag, mat):
    """.map texture name (no "textures/") of a CoD world material."""
    pre = "cod4/%s/" % tag
    room = MAX_NAME - len("textures/") - len(pre) - WORLD_SUFFIX_ROOM
    return pre + _fit(safe(mat), room)
