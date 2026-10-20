"""CoD4 (IW3) raw materials/<name> reader (MaterialRaw, KisakCOD r_material.h)."""
import struct

GFXS_BLEND = {0: None, 1: 'GL_ZERO', 2: 'GL_ONE', 3: 'GL_SRC_COLOR', 4: 'GL_ONE_MINUS_SRC_COLOR',
              5: 'GL_SRC_ALPHA', 6: 'GL_ONE_MINUS_SRC_ALPHA', 7: 'GL_DST_ALPHA',
              8: 'GL_ONE_MINUS_DST_ALPHA', 9: 'GL_DST_COLOR', 10: 'GL_ONE_MINUS_DST_COLOR'}
ATEST = {0x0000: None, 0x0800: None, 0x1000: 'GT0', 0x2000: 'LT128', 0x3000: 'GE128'}
CULL = {0: 'back', 0x4000: 'none', 0x8000: 'back', 0xC000: 'front'}


def _cstr(b, off):
    if not off or off >= len(b):
        return ''
    e = b.index(b'\0', off)
    return b[off:e].decode('latin-1')


def read_material(data):
    (nameOff, refOff, gameFlags, sortKey, atlR, atlC, maxDeform, deformFlags, usage,
     toolFlags, locale, atsW, atsH, tess, surfFlags, contents) = struct.unpack_from('<IIBBBBfBBHIHHfii', data, 0)
    sb0, sb1, ntex, nconst, techOff, texOff, constOff = struct.unpack_from('<IIHHIII', data, 0x28)
    textures = []
    for i in range(ntex):
        tnOff, samp, sem, _, _, imgOff = struct.unpack_from('<IBBBBI', data, texOff + 12 * i)
        textures.append({'slot': _cstr(data, tnOff), 'semantic': sem, 'sampler': samp,
                         'image': _cstr(data, imgOff) if sem != 11 else None})
    consts = []
    for i in range(nconst):
        cOff = struct.unpack_from('<I', data, constOff + 20 * i)[0]
        consts.append((_cstr(data, cOff), struct.unpack_from('<4f', data, constOff + 20 * i + 4)))
    return {
        'name': _cstr(data, nameOff), 'refImage': _cstr(data, refOff), 'techset': _cstr(data, techOff),
        'sortKey': sortKey, 'gameFlags': gameFlags, 'surfaceFlags': surfFlags & 0xFFFFFFFF,
        'contents': contents & 0xFFFFFFFF, 'toolFlags': toolFlags,
        'srcBlend': GFXS_BLEND.get(sb0 & 0xF), 'dstBlend': GFXS_BLEND.get((sb0 >> 4) & 0xF),
        'alphaTest': ATEST.get(sb0 & 0x3800 if sb0 & 0x800 == 0 else 0x800),
        'cull': CULL.get(sb0 & 0xC000), 'depthWrite': bool(sb1 & 1), 'stateBits': (sb0, sb1),
        # GFXS1 bits 4-5 (GFXS1_POLYGON_OFFSET_MASK 0x30): CoD's own depth
        # bias, 0 = none, 1 and 2 = pushed towards the camera, 3 = shadowmap.
        # This is the material flag that keeps a decal from z-fighting with
        # the surface it lies on - exactly what polygonOffset does in a SOF2
        # shader, so it is a direct translation rather than a guess from the
        # draw order (sortKey) or the geometry.
        'polygonOffset': (sb1 >> 4) & 3,
        'textures': textures, 'constants': consts, 'atlas': (atlR, atlC),
    }
