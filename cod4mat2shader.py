#!/usr/bin/env python3
"""
cod4mat2shader.py - CoD4 (IW3) materials -> SOF2 .shader + textures

Inputs : the manifest written by iwmap2map.py (world materials used by the
         .map, their nonsolid variants, the model list) and the unzipped
         cod4_assets folder (materials/, images/, xmodel/).
Outputs: <out>/shaders/cod4_<map>.shader
         <out>/textures/cod4/<image>.tga        (RLE TGA, alpha kept)

Every material is read from its compiled CoD4 file (material_iw3.py), then
expressed as the same Source-style parameter set sof2_shader.py already turns
into SOF2 shaders, so the CoD content gets exactly the conventions that were
verified in game for the CS:S pipeline:
  - lightmapped world form, 2x lightmap, decals vertex-lit + additive twin
    (apply_sof2_lighting_conventions)
  - models: runtime form + "<name>_bsp" baked form (compile -bsp with -rename)
  - q3map_material from the CoD surface type (infoParms order, KisakCOD
    src/universal/surfaceflags.cpp), not from guessing names
  - "<name>_nonsolid" copies for faces iwmap2map flagged nonColliding

CoD-specific cases handled here:
  - multiply decals (CoD blend ZERO/SRC_COLOR = 1x modulate)
  - additive decals (ONE ONE), alpha-blend decals (sortKey 9..24) -> polygonOffset
  - 'unlit' techsets -> fullbright, no lightmap
  - 'shadowcaster' techsets -> invisible
  - the sky cubemap -> six skyParms faces, resampled per pixel (see Q3_SKY)
"""
import argparse
import json
import os
import re
import sys
import traceback
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cod4_vision import (ambient_colour, film_tint, graded,  # noqa: E402
                         radiosity_scalar,
                         LIGHT_GAMMA as V_LIGHT_GAMMA,
                         SUN_TARGET_LUXEL as V_SUN_TARGET_LUXEL,
                         SUN_SCALE as V_SUN_SCALE, SKY_SCALE as V_SKY_SCALE)
from material_iw3 import read_material          # noqa: E402
from xmodel_v25 import read_xmodel              # noqa: E402
import iwi2tga                                   # noqa: E402
import sof2_shader as V                          # noqa: E402  (SOF2 .shader writer)
from cod4_vision import film_tint, graded
from cod4_names import world_shader               # noqa: E402

TEX_DIR = "cod4"               # textures/cod4/...
WORLD_PREFIX = "cod4/"         # world shader = textures/cod4/<map tag>/<mat> (cod4_names.py)
MODEL_PREFIX = "models/cod4/"  # model shader = models/cod4/<mat>

# CoD4 infoParms surface type index (surfaceFlags >> 20) -> name
COD_SURF = ["default", "bark", "brick", "carpet", "cloth", "concrete", "dirt", "flesh",
            "foliage", "glass", "grass", "gravel", "ice", "metal", "mud", "paper", "plaster",
            "rock", "sand", "snow", "water", "wood", "asphalt", "ceramic", "plastic", "rubber",
            "cushion", "fruit", "paintedmetal", "opaqueglass"]
# CoD surface name -> SOF2 q3map_material (SOF2's valid list, see sof2_shader.py)
COD_TO_SOF2_MATERIAL = {
    "bark": "Solidwood", "brick": "Concrete", "carpet": "Carpet", "cloth": "Fabric",
    "concrete": "Concrete", "dirt": "Dirt", "flesh": "Flesh", "foliage": "GreenLeaves",
    "glass": "Glass", "grass": "Shortgrass", "gravel": "Gravel", "ice": "Ice",
    "metal": "Solidmetal", "mud": "Mud", "paper": "Fabric", "plaster": "Plaster",
    "rock": "Rock", "sand": "Sand", "snow": "Snow", "water": "Water", "wood": "Solidwood",
    "asphalt": "Concrete", "ceramic": "Tiles", "plastic": "Plastic", "rubber": "Rubber",
    "cushion": "Fabric", "fruit": "None", "paintedmetal": "Solidmetal",
    "opaqueglass": "Glass", "default": None,
}
# sof2_shader's SURFACEPROP_MAP turns 'metal'/'glass'/'water' into surfaceparms;
# keep that behaviour for the CoD names that mean the same thing
COD_SURFACEPARM = {"paintedmetal": "metalsteps", "opaqueglass": "nomarks"}

# ---------------------------------------------------------------- sky
# CoD stores the sky as a D3D cubemap (faces +X,-X,+Y,-Y,+Z,-Z, standard
# D3D/GL face orientation - checked on cobra_ft: seam error 5.1 vs 7-10 with a
# face deliberately mis-oriented; +Z is the zenith, Z-up like the world).
# idTech3's skybox faces are defined by tr_sky.c st_to_vec + the suffix order
# rt,bk,lf,ft,up,dn. Rather than guess renames/rotations, every pixel of each
# idTech3 face is resampled from the cubemap along its exact world direction.
# tr_sky.c draws side i with image suf[sky_texorder[i]], suf = rt,bk,lf,ft,up,dn
# and sky_texorder = {0,2,1,3,4,5}: side 1 (-X) shows "lf", side 2 (+Y) "bk".
# (Missing that swap put lf/bk on the wrong sides - seams in game.)
Q3_SKY = [  # suffix, st_to_vec of the side it is drawn on (k>0: b[k-1], k<0: -b[-k-1]; b = s, t, dist)
    ("_rt", (3, -1, 2)), ("_lf", (-3, 1, 2)), ("_bk", (1, 3, 2)),
    ("_ft", (-1, -3, 2)), ("_up", (-2, -1, 3)), ("_dn", (2, -1, -3)),
]


def _cube_sample(faces, d):
    """bilinear sample of a D3D cubemap (list of 6 HxWx3 float arrays) along
    directions d (N,3)."""
    import numpy as np
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    ax = np.abs(d)
    major = np.argmax(ax, axis=1)
    out = np.zeros((len(d), 3))
    table = [  # face index, sc, tc, ma
        (0, lambda: -z, lambda: -y, lambda: ax[:, 0]), (1, lambda: z, lambda: -y, lambda: ax[:, 0]),
        (2, lambda: x, lambda: z, lambda: ax[:, 1]), (3, lambda: x, lambda: -z, lambda: ax[:, 1]),
        (4, lambda: x, lambda: -y, lambda: ax[:, 2]), (5, lambda: -x, lambda: -y, lambda: ax[:, 2]),
    ]
    for fi, sc, tc, ma in table:
        axis, positive = fi // 2, fi % 2 == 0
        m = (major == axis) & ((d[:, axis] > 0) == positive)
        if not m.any():
            continue
        F = faces[fi]
        h, w = F.shape[:2]
        u = ((sc()[m] / ma()[m]) + 1) * 0.5 * w - 0.5
        v = ((tc()[m] / ma()[m]) + 1) * 0.5 * h - 0.5
        u0 = np.clip(np.floor(u).astype(int), 0, w - 1)
        v0 = np.clip(np.floor(v).astype(int), 0, h - 1)
        u1 = np.clip(u0 + 1, 0, w - 1)
        v1 = np.clip(v0 + 1, 0, h - 1)
        fu = np.clip(u - u0, 0, 1)[:, None]
        fv = np.clip(v - v0, 0, 1)[:, None]
        out[m] = (F[v0, u0] * (1 - fu) * (1 - fv) + F[v0, u1] * fu * (1 - fv) +
                  F[v1, u0] * (1 - fu) * fv + F[v1, u1] * fu * fv)
    return out


def cubemap_to_q3_sky(ims, rot=0, mirror=False):
    """6 PIL images (D3D order) -> [(suffix, PIL image)] idTech3 skybox faces.
    rot (degrees about Z) / mirror (y -> -y) re-orient the sky around the
    vertical axis - the one thing the cloud images can't pin down."""
    import numpy as np
    from PIL import Image
    faces = [np.asarray(im.convert("RGB")).astype(float) for im in ims]
    n = faces[0].shape[0]
    a = (np.arange(n) + 0.5) / n
    U, V = np.meshgrid(a, a)
    s = (2 * U - 1).ravel()           # tr_sky.c: image u = (s+1)/2
    tt = (1 - 2 * V).ravel()          #           image v = 1 - (t+1)/2
    b = [s, tt, np.ones_like(s)]
    out = []
    for suffix, stv in Q3_SKY:
        d = np.stack([b[k - 1] if k > 0 else -b[-k - 1] for k in stv], axis=1)
        d /= np.linalg.norm(d, axis=1)[:, None]
        if mirror:
            d[:, 1] = -d[:, 1]
        if rot:
            c, s_ = np.cos(np.radians(rot)), np.sin(np.radians(rot))
            d = np.stack([c * d[:, 0] - s_ * d[:, 1], s_ * d[:, 0] + c * d[:, 1], d[:, 2]], axis=1)
        rgb = _cube_sample(faces, d).reshape(n, n, 3)
        out.append((suffix, Image.fromarray(np.clip(rgb + 0.5, 0, 255).astype("uint8"), "RGB")))
    return out


# water: CoD's water UVs repeat every ~1350 units (the colour slot is unused
# by its procedural technique); x5 gives SOF2's water texture a ~270 unit repeat
WATER_SCALE = 5.0
SKY_REFLECTION = [None]      # textures/cod4/<sky>_up once the sky is known


def surf_name(info):
    idx = (info.get("surfaceFlags", 0) >> 20) & 0x1F
    return COD_SURF[idx] if idx < len(COD_SURF) else "default"


def color_map(info):
    for t in info["textures"]:
        if t["slot"].lower() == "colormap" and t["image"]:
            return t["image"]
    for t in info["textures"]:
        if t["image"] and not t["image"].startswith("$"):
            return t["image"]
    return None


def img_rel(image):
    """textures/cod4/<image> stem, safe for qpaths ('#', '~', '&' -> '_')."""
    return TEX_DIR + "/" + re.sub(r"[^a-z0-9_\-]", "_", image.lower())


def classify(info):
    tech = (info.get("techset") or "").lower()
    src, dst = info.get("srcBlend"), info.get("dstBlend")
    sort = info.get("sortKey") or 0
    if tech == "sky":
        return "sky"
    if tech.startswith("water"):
        return "water"
    if tech.startswith("shadowcaster"):
        return "invisible"
    if (src, dst) in (("GL_ZERO", "GL_SRC_COLOR"), ("GL_DST_COLOR", "GL_ZERO")):
        return "multiply"
    if (src, dst) == ("GL_ONE", "GL_ONE"):
        return "additive"
    opaque = (src in (None, "GL_ONE") and dst in (None, "GL_ZERO"))
    if tech.startswith("unlit") and opaque:
        return "unlit"
    if not opaque:
        return "blend"
    if info.get("alphaTest"):
        return "alphatest"
    return "opaque"


# CoD's own answer to "does this need a depth offset": GFXS1_POLYGON_OFFSET in
# the material's render state (material_iw3.read_material -> polygonOffset).
# It is the same flag as SOF2's polygonOffset, so when CoD sets it the shader
# gets it - no guessing from the draw order or from the geometry. mp_shipment's
# me_window_02 and the me_decal_* planes clipped into the wall because neither
# of the other two signals reached them.
#
# The second answer, kept as a fallback for materials with no bias set.
# sortKey is the material's draw order
# and 9..24 is its decal band - a flat thing laid onto a surface, which is
# exactly what needs polygonOffset in SOF2 or it z-fights with the wall under
# it. Measured on mp_derail: 76 materials carry a decal sortKey, and 8 of them
# are alpha-tested or opaque (posters, me_decal_papers, floor grates, a metal
# vent), so restricting this to the blended kinds let those z-fight. The band
# excludes sky (5), the blend-layer sort (9 is the low edge, kept) and glass
# (43 and up).
DECAL_SORT_LO, DECAL_SORT_HI = 9, 24


def is_decal(info, kind):
    if kind in ("sky", "water", "invisible"):
        return False
    bias = info.get("polygonOffset") or 0
    if bias in (1, 2):
        return True
    sort = info.get("sortKey") or 0
    return DECAL_SORT_LO <= sort <= DECAL_SORT_HI


def body_for(info, image_stem, kind):
    """Source-style param body for sof2_shader.convert_vmt_to_shader."""
    b = {"$basetexture": [image_stem]}
    sn = surf_name(info)
    if sn != "default":
        b["$surfaceprop"] = [sn]
    if kind == "alphatest":
        b["$alphatest"] = ["1"]
    elif kind == "blend":
        b["$translucent"] = ["1"]
    elif kind == "additive":
        b["$additive"] = ["1"]
    if info.get("cull") == "none":
        b["$nocull"] = ["1"]
    return b


def custom_shader(name, image_path, info, kind):
    sn = surf_name(info)
    mat = COD_TO_SOF2_MATERIAL.get(sn)
    head = [name, "{", "\tqer_editorimage {}".format(image_path)]
    if mat and mat != "None":
        head.append("\tq3map_material {}".format(mat))
    if kind == "multiply":
        # CoD multiply decal: dst * tex (1x). Modulates the already-lit
        # surface below, so it needs no lighting of its own.
        return head + [
            "\t// CoD multiply decal (blend ZERO / SRC_COLOR)",
            "\tpolygonOffset", "\tsurfaceparm trans", "\tsurfaceparm nonsolid",
            "\tsurfaceparm nomarks", "\tsurfaceparm nolightmap", "\tq3map_nolightmap",
            "\tsort banner", "\tcull none",
            "\t{", "\t\tmap {}".format(image_path), "\t\tblendFunc GL_DST_COLOR GL_ZERO",
            "\t\trgbGen identity", "\t}", "}"]
    if kind == "unlit":
        return head + [
            "\t// CoD unlit techset: fullbright",
            "\tsurfaceparm nolightmap", "\tq3map_nolightmap",
            "\t{", "\t\tmap {}".format(image_path), "\t\trgbGen identity", "\t}", "}"]
    if kind == "water":
        # CoD4's 'water' techset renders engine-generated ripple normal maps;
        # its colorMap is only a placeholder (case64blue = the 1-2-3-4 grid),
        # so nothing visual converts. Built instead on SOF2's own stock water
        # textures (colombia.shader), plus the map's converted sky as a cheap
        # environment reflection. Opaque base (nothing to see below a sea),
        # vertex-lit like Raven's water, 3 stages.
        sky = SKY_REFLECTION[0]
        lines = [name, "{",
                 "\tqer_editorimage textures/colombia/water_terrain",
                 "\t// CoD4 water techset -> SOF2 water (stock colombia textures + sky reflection)",
                 "\tsurfaceparm nonsolid", "\tsurfaceparm nomarks", "\tsurfaceparm trans",
                 "\tq3map_material Water", "\tq3map_nolightmap", "\tq3map_onlyvertexlighting",
                 "\t{", "\t\tmap textures/colombia/water_terrain", "\t\trgbGen exactVertex",
                 "\t\ttcMod turb 0 0.03 0 0.05", "\t\ttcMod scroll 0.01 0.006",
                 "\t\ttcMod scale %g %g" % (WATER_SCALE, WATER_SCALE), "\t}",
                 "\t{", "\t\tmap textures/colombia/water_terrain2",
                 "\t\tblendFunc GL_SRC_ALPHA GL_ONE_MINUS_SRC_ALPHA", "\t\trgbGen exactVertex",
                 "\t\ttcMod turb 0 0.05 0.3 0.07", "\t\ttcMod scroll -0.012 0.008",
                 "\t\ttcMod scale %g %g" % (WATER_SCALE * 1.4, WATER_SCALE * 1.4), "\t}"]
        if sky:
            lines += ["\t{", "\t\tmap %s" % sky, "\t\tblendFunc GL_ONE GL_ONE",
                      "\t\trgbGen const ( 0.22 0.24 0.26 )", "\t\ttcGen environment",
                      "\t\t// sky reflection", "\t}"]
        return lines + ["}"]
    if kind == "invisible":
        return [name, "{", "\t// CoD shadowcaster: no visible surface",
                "\tsurfaceparm nodraw", "\tsurfaceparm nonsolid", "\tsurfaceparm trans",
                "\tsurfaceparm nomarks", "\tsurfaceparm nolightmap", "}"]
    raise ValueError(kind)


def decal_no_depthwrite(text, names):
    """flat overlays (papers on a floor, signs on a wall) that write depth -
    alpha-tested with depthWrite - would hide the blend layers drawn on top
    of them at the same depth (flicker). They stop writing depth and draw in
    the decal sort, after the surface they lie on."""
    out, cur, has_sort, lines = [], None, False, text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        name = ln.strip().lower()
        if name in names and i + 1 < len(lines) and lines[i + 1].strip() == "{":
            depth, k, block = 0, i + 1, []
            while k < len(lines):
                st = lines[k].strip()
                depth += st.count("{") - st.count("}")
                block.append(lines[k])
                if depth == 0:
                    break
                k += 1
            body = [b for b in block if b.strip().lower() != "depthwrite"]
            if not any(b.strip().lower().startswith("sort ") for b in body) and \
                    any(b.strip().lower().startswith("alphafunc") for b in body):
                body.insert(1, "\tsort decal")
            out.append(ln)
            out.extend(body)
            i = k + 1
            continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def layer_shaders(name, image_path, info, kind, depth):
    """<name>_blend (+ the _bsp twin q3map2 -rename asks for): a CoD ground
    layer from iwmap2map's blend .lwo, faded in by its vertex alpha. Layer
    depth -> sort just above SOF2's decal sort so the stack draws in CoD's
    order (road, then cracks, then papers, then trash)."""
    sn = surf_name(info)
    mat = COD_TO_SOF2_MATERIAL.get(sn)
    head = ["\tqer_editorimage {}".format(image_path),
            "\t// CoD vertex-alpha layer %d: faded in by the vertex alpha" % depth]
    if mat and mat != "None":
        head.append("\tq3map_material {}".format(mat))
    head += ["\tsurfaceparm nonsolid", "\tsurfaceparm trans", "\tsurfaceparm nomarks",
             "\tq3map_nolightmap", "\tq3map_onlyvertexlighting", "\tpolygonOffset",
             "\tsort %g" % (4 + 0.1 * min(depth, 9)), "\tcull none"]
    if kind == "multiply":
        stages = ["\t{", "\t\tmap " + image_path, "\t\tblendFunc GL_DST_COLOR GL_ZERO",
                  "\t\trgbGen identity", "\t}"]
    elif kind in ("additive", "unlit") or (info.get("techset") or "").lower().startswith("unlit"):
        add = kind == "additive" or info.get("dstBlend") == "GL_ONE"
        stages = ["\t{", "\t\tmap " + image_path,
                  "\t\tblendFunc GL_SRC_ALPHA " + ("GL_ONE" if add else "GL_ONE_MINUS_SRC_ALPHA"),
                  "\t\trgbGen identity", "\t\talphaGen vertex", "\t}"]
    else:
        stages = ["\t{", "\t\tmap " + image_path, "\t\tblendFunc GL_SRC_ALPHA GL_ONE_MINUS_SRC_ALPHA",
                  "\t\trgbGen vertex", "\t\talphaGen vertex", "\t}",
                  "\t{", "\t\tmap " + image_path, "\t\tblendFunc GL_SRC_ALPHA GL_ONE",
                  "\t\trgbGen vertex", "\t\talphaGen vertex",
                  "\t\t// same stage again, added: SOF2 clamps vertex colour at 1.0x, this is the 2x", "\t}"]
    return ["\n".join([n, "{"] + head + stages + ["}"]) for n in (name + "_blend", name + "_blend_bsp")]


# How CoD's sun numbers become q3map2's. The old values (200 and 1000) were
# admitted guesses. These are derived, and the sun one is exact:
#
#   q3map2 normalizes q3map_sun's colour so its max component is 1, sets
#   light.photons = the intensity token, and for a Sun light uses
#   add = photons, colour = colour * add, scaled by dot(N, L).  So for a
#   surface facing the sun, THE INTENSITY IS THE LUXEL VALUE, 0-255.
#
#   The world shader draws  { map tex }{ map $lightmap blendFunc
#   GL_DST_COLOR GL_SRC_COLOR }, which is src*DST + dst*SRC = 2*tex*lm. So a
#   surface of albedo a goes pure white at  lm = 255 / (2a)  - for snow
#   (a = 0.85) that is lm = 150, and every luxel above it is thrown away.
#
#   Targeting lm = 140 after gamma leaves a little headroom. At LIGHT_GAMMA
#   that is 255*(140/255)**gamma before gamma, and CoD's sunlight is 1.2 on
#   mp_derail, so the scale is that over 1.2.
#
# q3map_skyLight is in the SAME units: CreateSkyLights builds
# (iterations/2 * iterations) suns over the hemisphere, each with
# photons = the value token, so at 3 iterations a flat-up surface collects
# about 0.8x the value. So the sky light takes SUN_SCALE too.
#
# It used to take 5x that, which is where "bland" came from. CoD's own
# sun:ambient on mp_strike is 1.3 : 0.1, and multiplying the ambient by five
# turned a 13:1 ratio into 2.6:1 - a flat wash with almost no shadow depth,
# and on a map whose CoD ambient colour is blue (_color 0.5 0.5 0.7) that
# wash is what the eye reads. Shadows are filled by -bounce in the compile
# instead, the way CoD fills them: with light off the warm ground and walls.
# in cod4_vision, so the light entities in iwmap2map share the same anchor
LIGHT_GAMMA, SUN_TARGET_LUXEL = V_LIGHT_GAMMA, V_SUN_TARGET_LUXEL
SUN_SCALE, SKY_SCALE = V_SUN_SCALE, V_SKY_SCALE


def sky_shader(name, sky_stem, sun, sun_scale=None, sky_scale=None, vision=None):
    lines = [name, "{"]
    tint = film_tint(vision)
    try:
        sc = [float(x) for x in (sun.get("suncolor") or "1 1 1").split()]
        p, y, _ = [float(x) for x in (sun.get("sundirection") or "-45 0 0").split()]
        light = float(sun.get("sunlight") or 1.0)
        elev = -p
        lines.append("\t// CoD4 sun: suncolor {} sunlight {} sundirection {}".format(
            sun.get("suncolor"), sun.get("sunlight"), sun.get("sundirection")))
        if tint:
            lt, dt, desat = tint
            was = list(sc)
            sc = graded(sc, lt, desat)
            lines.append("\t// CoD grades the frame: lightTint {} darkTint {} desaturation {}."
                         " SOF2 has no such pass, so the sun takes the light tint"
                         " ({:.2g} {:.2g} {:.2g} -> {:.2g} {:.2g} {:.2g}) and the sky light"
                         " below takes the dark tint.".format(
                             " ".join("%.3g" % v for v in lt), " ".join("%.3g" % v for v in dt),
                             "%.3g" % desat, *(was + sc)))
        ss = SUN_SCALE if sun_scale is None else sun_scale
        lines.append("\t// intensity IS the luxel a sun-facing surface gets, 0-255;"
                     " x%g targets %g after gamma %g" % (ss, SUN_TARGET_LUXEL, LIGHT_GAMMA))
        # r_primaryLightTweakDiffuseStrength scales the sun's diffuse term, so
        # unlike the tint it is lighting rather than grading and belongs on the
        # intensity. mp_crash sets it to 1.4.
        diffuse = 1.0
        if vision:
            try:
                diffuse = max(0.0, float(vision.get("r_primarylightdiffuse") or 1.0))
            except ValueError:
                diffuse = 1.0
        if abs(diffuse - 1.0) > 1e-6:
            lines.append("\t// CoD's primaryLightTweakDiffuseStrength %g on the sun" % diffuse)
        lines.append("\tq3map_sun {:.3g} {:.3g} {:.3g} {:g} {:g} {:g}".format(
            sc[0], sc[1], sc[2], round(light * ss * diffuse), y % 360, elev))
        # CoD's flat ambient AND its sun radiosity, because q3map2's own
        # radiosity is not affordable here: on mp_strike one -bounce pass
        # turned 76 lights into 122,201 diffuse lights and did not finish in
        # over two hours. CoD's numbers for the same thing cost nothing.
        amb, amb_only, rad = radiosity_scalar(sun)
        if amb > 0:
            ac = ambient_colour(sun, vision)
            # q3map2 gives a sky light the shader's own colour (si->color), and
            # without this that is the average of the sky image - so the whole
            # hemisphere fill came out sky-blue whatever CoD's ambient was.
            lines.append("\tq3map_lightRGB {:.3g} {:.3g} {:.3g}".format(*ac))
            ks = SKY_SCALE if sky_scale is None else sky_scale
            lines.append("\t// CoD ambient %g%s, x%g on the sun's own scale"
                         % (amb_only, " + sun radiosity %g" % rad if rad else "", ks))
            lines.append("\tq3map_skyLight {:g} 3".format(max(1, round(amb * ks))))
    except ValueError:
        pass
    lines += ["\tsurfaceparm noimpact", "\tsurfaceparm nolightmap", "\tsurfaceparm sky",
              "\tqer_editorimage textures/tools/editor_images/qer_sky",
              "\tnopicmip", "\tnomipmaps",
              "\tskyParms textures/{} 512 -".format(sky_stem), "}"]
    return lines


def model_materials(assets, models):
    mats = Counter()
    for m in models:
        p = os.path.join(assets, "xmodel", m)
        if not os.path.exists(p):
            continue
        try:
            x = read_xmodel(open(p, "rb").read())
        except Exception:
            continue
        for L in x["lods"]:
            for mt in L["materials"]:
                mats[mt] += 1
    return mats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("--assets", required=True)
    ap.add_argument("--out", required=True, help="output root (gets shaders/ and textures/)")
    ap.add_argument("--name", help="shader file base name (default: cod4_<manifest stem>)")
    ap.add_argument("--vertex-light-terrain", action="store_true",
                    help="draw the baked terrain under CoD blend layers vertex-lit instead of "
                         "lightmapped. Done on its own once a compile of the map goes over "
                         "MAX_SHADERS; its lightmaps are the biggest single consumer of the "
                         "2048 shader pool")
    ap.add_argument("--no-vertex-light-terrain", action="store_true",
                    help="keep the baked terrain lightmapped even if MAX_SHADERS was exceeded")
    ap.add_argument("--vertex-light-seethrough", action="store_true",
                    help="make see-through world shaders (fences, grilles, decals) vertex-lit "
                         "instead of lightmapped, to save shader/lightmap pairs. Off by "
                         "default: vertex lighting samples at the corners, so a decal lying "
                         "on a floor samples the floor plane and comes out black")
    ap.add_argument("--terrain-2x", action="store_true",
                    help="draw the vertex-lit terrain twice, added, the way baked props are. "
                         "Only if it comes out too dark: vertex lighting has no per-luxel "
                         "shadowing, so doubling it usually saturates")
    a = ap.parse_args()

    man = json.load(open(a.manifest))
    base = a.name or "cod4_" + os.path.splitext(os.path.basename(a.manifest))[0]
    # A compile of this map that went over MAX_SHADERS switches the baked
    # terrain to vertex lighting; see bsp_budget.vertex_light_terrain.
    map_name = re.sub(r"^cod4_", "", base)
    vl_terrain = False
    if not a.no_vertex_light_terrain:
        out_root = os.path.dirname(os.path.normpath(a.out))
        try:
            import bsp_budget
            vl_terrain = bsp_budget.vertex_light_terrain(out_root, map_name)
            if not vl_terrain:
                # worth saying out loud: a missing record silently reverts the
                # terrain to lightmapped and puts a big map back over
                # MAX_SHADERS, which is exactly what a deleted output folder did
                rec = bsp_budget.load(out_root).get(map_name)
                print("baked terrain: lightmapped (%s in %s)"
                      % ("no compile recorded yet" if not rec
                         else "last compile was within the shader budget",
                         os.path.join(out_root, bsp_budget.FILE)))
        except Exception as e:
            print("baked terrain: lightmapped (could not read the compile record: %s)" % e)
            vl_terrain = False
    if a.vertex_light_terrain:
        vl_terrain = True
    if vl_terrain and not a.no_vertex_light_terrain:
        try:
            import bsp_budget
            bsp_budget.mark_terrain_vertex_lit(
                os.path.dirname(os.path.normpath(a.out)), map_name)
        except Exception:
            pass
    sh_dir = os.path.join(a.out, "shaders")
    tx_dir = os.path.join(a.out, "textures", TEX_DIR)
    os.makedirs(sh_dir, exist_ok=True)
    os.makedirs(tx_dir, exist_ok=True)

    mdir = os.path.join(a.assets, "materials")
    mindex = {f.lower(): os.path.join(mdir, f) for f in os.listdir(mdir)}

    # per-map world shader names (cod4_names.py), same as iwmap2map.py wrote
    tag = man.get("tag")
    if not tag:
        sys.exit("manifest has no map tag - convert the map again with this update's iwmap2map.py")
    jobs = []   # (shader name, material name, is_model)
    for mat in man["materials"]:
        jobs.append((world_shader(tag, mat), mat, False))
    mm = model_materials(a.assets, man.get("models", {}))
    for mat in sorted(mm):
        jobs.append((MODEL_PREFIX + re.sub(r"[^a-z0-9_/\-]", "_", mat.lower()), mat, True))

    for _rel, _mat, _ in jobs:
        _p = mindex.get(_mat.lower())
        if _p:
            try:
                _info = read_material(open(_p, "rb").read())
            except Exception:
                continue
            if classify(_info) == "sky" and color_map(_info):
                SKY_REFLECTION[0] = "textures/" + img_rel(color_map(_info)) + "_up"
                break
    blocks, warnings = [], []
    images_needed = set()
    decal_names, nonsolid_names = [], []
    overlay_names, layer_blocks, effects_add, ground_names = [], [], [], []
    decal_bias, decal_sort, bias_shaders = [], [], set()
    kinds = Counter()
    alpha_cache = {}

    def img_alpha(img):
        """does the CoD image have a real alpha channel"""
        if img not in alpha_cache:
            try:
                _hd, _ims = iwi2tga.decode(open(os.path.join(a.assets, "images", img + ".iwi"), "rb").read())
                alpha_cache[img] = bool(_ims) and iwi2tga.has_alpha(_ims[0])
            except Exception:
                alpha_cache[img] = True          # unknown: leave the shader alone
        return alpha_cache[img]
    unmapped_sp, unmapped_mat = Counter(), Counter()
    sky_written = False
    errors = []

    def err(what):
        tb = traceback.format_exc().strip().splitlines()
        errors.append("%s: %s  [%s]" % (what, tb[-1], tb[-3].strip() if len(tb) >= 3 else ""))

    for shader_rel, mat, is_model in jobs:
      try:
          path = mindex.get(mat.lower())
          if not path:
              warnings.append("material file missing: " + mat)
              continue
          info = read_material(open(path, "rb").read())
          kind = classify(info)
          kinds[kind] += 1
          img = color_map(info)
          if kind == "sky":
              if img:
                  images_needed.add((img, "sky"))
                  blocks.append("\n".join(sky_shader(V.with_textures_prefix(shader_rel),
                                                     img_rel(img), man.get("sun", {}),
                                                     vision=man.get("vision"))))
                  sky_written = True
              continue
          if not img and kind != "invisible":
              warnings.append("no colorMap: " + mat)
              continue
          if img:
              images_needed.add((img, "plain"))
          stem = img_rel(img) if img else ""
          shader_name = V.with_textures_prefix(shader_rel)
          if kind in ("multiply", "unlit", "invisible", "water"):
              blocks.append("\n".join(custom_shader(shader_name, "textures/" + stem, info, kind)))
          else:
              b = body_for(info, stem, kind)
              mm_map = dict(COD_TO_SOF2_MATERIAL)
              sp_map = dict(COD_SURFACEPARM)
              txt = V.convert_vmt_to_shader(shader_rel, "LightmappedGeneric", b, sp_map, unmapped_sp,
                                            mm_map, unmapped_mat, warnings)
              if txt and kind == "blend" and img and (mat.lower().startswith("gfx_") or
                                                    (info.get("techset") or "").lower().startswith("effect")) \
                      and not img_alpha(img):
                  # light beams / glare (gfx_floodlight_beam_*): CoD fades them
                  # with vertex alpha, which an MD3 cannot carry; the image has
                  # no alpha, so a plain blend draws its black. Additive instead:
                  # black adds nothing.
                  txt = txt.replace("blendFunc GL_SRC_ALPHA GL_ONE_MINUS_SRC_ALPHA", "blendFunc GL_ONE GL_ONE")
                  txt = re.sub(r"rgbGen (lightingDiffuse|vertex|exactVertex)", "rgbGen identity", txt)
                  effects_add.append(shader_name)
              if txt and kind == "alphatest":
                  # cut-out shadows in q3map2 for foliage/fences (CoD used a
                  # separate shadowcaster mesh for this, which xmodel2md3 drops)
                  txt = re.sub(r"(\n\tqer_editorimage [^\n]+)", r"\1\n\tsurfaceparm alphashadow", txt)
              if txt:
                  blocks.append(txt)
              if (info.get("polygonOffset") or 0) in (1, 2):
                  bias_shaders.add(shader_name.lower())
              if is_decal(info, kind):
                  decal_names.append(shader_name)
                  (decal_bias if (info.get("polygonOffset") or 0) in (1, 2)
                   else decal_sort).append(mat)
          if not is_model and "_nonsolid" not in shader_rel:
              if man["materials"].get(mat, {}).get("variants", {}).get("_nonsolid"):
                  nonsolid_names.append(shader_name)
          if not is_model and man["materials"].get(mat, {}).get("overlay"):
              overlay_names.append(shader_name.lower())
          if not is_model and man["materials"].get(mat, {}).get("ground"):
              ground_names.append((shader_name, "textures/" + stem if img else "", info))
          depth = None if is_model else man["materials"].get(mat, {}).get("layer")
          if depth and img:
              layer_blocks.extend(layer_shaders(shader_name, "textures/" + stem, info, kind, depth))
      except Exception:
        err("material " + mat)

    text = "\n\n".join(blocks)
    if decal_names:
        text, _ = V.add_polygon_offset(text, set(decal_names))
    if overlay_names:
        # flat patches lying in a wall face (signs; iwmap2map find_overlays)
        text, _ = V.add_polygon_offset(text, set(overlay_names), add_cull=False)
        text = decal_no_depthwrite(text, set(overlay_names))
    if nonsolid_names:
        copies, missing = V.nonsolid_copies(text, nonsolid_names)
        text += "\n\n" + "\n\n".join(copies)
    # A "_nonsolid" variant is CoD's non-colliding copy of a material. Every
    # one of them used to get polygonOffset here, on the theory that a
    # non-colliding piece lies in a wall's face. That was wrong twice over.
    # ch_brick_wall_08_nonsolid on mp_shipment is an ordinary opaque wall -
    # CoD gives it sortKey 4 and no depth bias at all - and pushing the wall
    # forward by the same amount as the decal lying on it cancels the decal's
    # offset, so the z-fighting it was meant to cure stayed exactly as it was.
    # The copies are cut from text that already carries the base material's
    # offset, so CoD's own flag decides and nothing is added per variant.
    n_ns_po = 0
    for it in V._split_shader_text(text):
        if it[0] == "shader" and it[1].lower().endswith("_nonsolid"):
            if any("polygonoffset" in v.lower() for k, v in it[2] if k == "line"):
                n_ns_po += 1
    text = V.apply_sof2_lighting_conventions(text)
    # See-through world shaders (decals, blended overlays, alpha-tested
    # fences/grilles) vertex-lit instead of lightmapped. Every shader x
    # lightmap pair is its own renderer shader and SOF2 has 2048 in all
    # (mp_strike: 1029 pairs + the props' + the game's own -> props without
    # shaders). Opaque surfaces keep their lightmaps.
    items = V._split_shader_text(text)
    out_items, n_vl = [], 0
    for it in items:
        if it[0] == "shader" and it[1].lower().startswith("textures/") and "/sky" not in it[1].lower():
            _, name, elems = it
            stages = [v for k, v in elems if k == "stage"]
            has_lm = any(V._is_lightmap_stage(v) for v in stages)
            tex = [v for v in stages if not V._is_lightmap_stage(v)]
            see_through = bool(tex) and any(
                re.match(r"^\s*(blendFunc|alphaFunc)\b", l.strip(), re.I) for l in tex[0])
            if has_lm and see_through:
                multiply = any(re.match(r"^\s*blendFunc\s+(GL_DST_COLOR\s+GL_ZERO|GL_ZERO\s+GL_SRC_COLOR|filter)\b",
                                        l.strip(), re.I) for l in tex[0])
                if multiply:
                    # darkens what is under it: takes no light of its own
                    hdr = [(k, v) for k, v in elems if k == "line" and not re.match(
                        r"^\s*q3map_(nolightmap|onlyvertexlighting)\b", v.strip(), re.I)]
                    st = [V._stage_set(v, "rgbGen", "identity") for v in tex]
                    form = hdr + [("line", "\tq3map_nolightmap")] + [("stage", v) for v in st]
                else:
                    form = V._prop_bsp_form(elems)
                out_items.append(("shader", name, form))
                n_vl += 1
                continue
        out_items.append(it)
    if n_vl:
        text = "\n\n".join(V._join_shader(it[1], it[2]) if it[0] == "shader" else "\n".join(it[1])
                           for it in out_items)
        print("see-through world shaders made vertex-lit (no lightmap pairs): %d" % n_vl)
    # "<shader>_far": vertex-lit copies for scenery outside the play area
    # (iwmap2map.py renames those faces). Raven's baked-prop form: no
    # lightmap, rgbGen vertex plus the additive 2x twin. Only shaders that
    # have a lightmap stage get one; the rest (already vertex lit, unlit,
    # water) are copied unchanged under the _far name.
    far = set(n.lower() for n in man.get("far_shaders", []))
    if far:
        items = V._split_shader_text(text)
        extra = []
        made = set()
        for it in items:
            if it[0] != "shader" or it[1].lower() not in far:
                continue
            _, name, elems = it
            has_lm = any(k == "stage" and V._is_lightmap_stage(v) for k, v in elems)
            form = V._prop_bsp_form(elems) if has_lm else elems
            extra.append(V._join_shader(name + "_far", form))
            made.add(name.lower())
        for n in sorted(far - made):
            warnings.append("_far shader without a base shader: " + n)
        if extra:
            text += "\n\n" + "\n\n".join(extra)
        print("far (vertex-lit scenery) shaders: %d" % len(extra))
    # level-wide fog (worldspawn "fog" key from iwmap2map.py): linear to
    # opaque at the distance matching CoD's exponential setExpFog
    fog = man.get("fog")
    if fog:
        text += "\n\n" + "\n".join([
            "textures/" + world_shader(tag, "fog"), "{",
            "\t// CoD setExpFog start %g halfway %g (%s)" % (fog["start"], fog["half"], fog["src"]),
            "\tqer_editorimage textures/tools/editor_images/qer_fogblack",
            "\tsurfaceparm nonsolid", "\tsurfaceparm fog",
            "\tfogparms ( %.3f %.3f %.3f ) %d" % (fog["rgb"][0], fog["rgb"][1], fog["rgb"][2], fog["opaque"]),
            "}"])
        print("fog: level-wide, colour %.2f %.2f %.2f, opaque at %d" % (*fog["rgb"], fog["opaque"]))
    # CoD "portal_nodraw" (the non-portal faces of a portal brush) -> skip.
    # Not SOF2's tools/_skip: it has no "surfaceparm skip", so q3map2 would
    # split the BSP on every one of those faces (same as vmf2map/skip)
    text += "\n\n" + "\n".join([
        "textures/cod4/skip", "{", "\tqer_editorimage textures/tools/_skip", "\tqer_nocarve",
        "\tqer_trans 0.5", "\tsurfaceparm nodraw", "\tsurfaceparm nonsolid", "\tsurfaceparm trans",
        "\tsurfaceparm noimpact", "\tsurfaceparm skip", "}"])

    if layer_blocks:
        text += "\n\n" + "\n\n".join(layer_blocks)
    if ground_names and vl_terrain:
        print("baked terrain under blend layers: vertex-lit, %s (the last compile of this map "
              "went over MAX_SHADERS; %d ground shaders)"
              % ("doubled" if a.terrain_2x else "single stage", len(ground_names)))
    if ground_names:
        # ground under blend layers, drawn from iwmap2map's layer model:
        # <name>_bsp = the lightmapped world shader again (q3map2 -rename),
        # <name>_clip = the patch left behind for collision only
        items = {it[1].lower(): it for it in V._split_shader_text(text) if it[0] == "shader"}
        extra = []
        for name, img_path, info in ground_names:
            it = items.get(name.lower())
            if it:
                form = (V._terrain_vertex_form(it[2], double=a.terrain_2x)
                        if vl_terrain else it[2])
                extra.append(V._join_shader(name + "_bsp", form))
            sn = surf_name(info)
            mmat = COD_TO_SOF2_MATERIAL.get(sn)
            extra.append("\n".join([name + "_clip", "{", "\t// collision of a ground patch drawn by the layer model",
                                     "\tqer_editorimage %s" % (img_path or "textures/tools/_clip"), "\tqer_trans 0.4"] +
                                    (["\tq3map_material %s" % mmat] if mmat and mmat != "None" else []) +
                                    ["\tsurfaceparm nodraw", "\tsurfaceparm nolightmap", "\tsurfaceparm nomarks", "}"]))
        text += "\n\n" + "\n\n".join(extra)

    out_sh = os.path.join(sh_dir, base + ".shader")
    with open(out_sh, "w", newline="\n") as fh:
        fh.write("// generated by cod4mat2shader.py from CoD4 compiled materials\n")
        fh.write("// COMPILE: q3map2 -bsp ... -rename  (baked props use the <name>_bsp shaders)\n\n")
        fh.write(text + "\n")

    # textures
    idir = os.path.join(a.assets, "images")
    done = bad = 0

    def placeholder(img):
        """small neutral grey image under the expected name: SOF2 then has
        nothing to warn about ("R_FindImageFile could not find ...", "stage
        with no image") and the surface draws plain instead of white/black"""
        try:
            from PIL import Image
            Image.new("RGB", (8, 8), (128, 128, 128)).save(
                os.path.join(tx_dir, os.path.basename(img_rel(img)) + ".tga"), compression="tga_rle")
            return True
        except Exception:
            return False

    for img, role in sorted(images_needed):
        src = os.path.join(idir, img + ".iwi")
        if not os.path.exists(src):
            warnings.append("image missing: %s%s" % (img, " (grey placeholder written)" if placeholder(img) else ""))
            continue
        try:
            hd, ims = iwi2tga.decode(open(src, "rb").read())
        except iwi2tga.IWIError as e:
            warnings.append("image %s: %s%s" % (img, e, " (grey placeholder written)" if placeholder(img) else ""))
            bad += 1
            continue
        except Exception:
            err("image " + img)
            bad += 1
            continue
        try:
            stem = os.path.basename(img_rel(img))
            if role == "sky" and len(ims) == 6:
                for suffix, im in cubemap_to_q3_sky(ims):
                    im.save(os.path.join(tx_dir, stem + suffix + ".tga"), compression="tga_rle")
            else:
                im = ims[0]
                if not iwi2tga.has_alpha(im):
                    im = im.convert("RGB")
                im.save(os.path.join(tx_dir, stem + ".tga"), compression="tga_rle")
            done += 1
        except Exception:
            err("writing image " + img)
            bad += 1

    print("shaders: %d world materials, %d model materials -> %s" % (
        len(man["materials"]), len(mm), out_sh))
    print("kinds:", dict(kinds))
    print("decals (polygonOffset): %d - %d from CoD's own depth bias, %d from the decal sortKey "
          "band; nonsolid copies: %d (%d inherit an offset from their base), sky: %s" % (
              len(decal_names), len(decal_bias), len(decal_sort),
              len(nonsolid_names), n_ns_po, sky_written))
    if decal_bias:
        print("  depth bias set by CoD on:", ", ".join(sorted(decal_bias)[:12]))
    if overlay_names:
        # Cross-check on the new signal: the flat-patch finder in iwmap2map
        # spots planes lying in a wall face from the geometry alone, which is
        # independent of anything in the material. The two should mostly
        # agree - if this reads 0 of N, or if the bias count above is nearly
        # every material in the map, the state-bit mask is wrong and
        # is_decal() should go back to sortKey only.
        print("  cross-check: %d of %d materials found lying flat on a wall also carry CoD's "
              "depth bias" % (sum(1 for n in overlay_names if n in bias_shaders), len(overlay_names)))
    print("flat patches on walls (polygonOffset): %d, blend layers (vertex alpha): %d shaders, "
          "light beams/glare made additive: %d" % (len(overlay_names), len(layer_blocks) // 2, len(effects_add)))
    print("textures written: %d (%d failed) -> %s" % (done, bad, tx_dir))
    if unmapped_mat:
        print("unmapped surface types:", dict(unmapped_mat))
    for w in warnings[:40]:
        print("  warn:", w)
    for e in errors:
        print("  ERROR:", e)
    if errors:
        print("%d error(s) above - conversion continued past them" % len(errors))


if __name__ == "__main__":
    main()
