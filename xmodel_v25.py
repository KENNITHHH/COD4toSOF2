"""CoD4 (IW3) raw xmodel / xmodelsurfs / xmodelparts v25 reader.
Layout follows KisakCOD src/xanim/xmodel_load_obj.cpp."""
import struct, math

class R:
    def __init__(s, b): s.b, s.p = b, 0
    def u8(s): v = s.b[s.p]; s.p += 1; return v
    def u16(s): v = struct.unpack_from('<H', s.b, s.p)[0]; s.p += 2; return v
    def s16(s): v = struct.unpack_from('<h', s.b, s.p)[0]; s.p += 2; return v
    def i32(s): v = struct.unpack_from('<i', s.b, s.p)[0]; s.p += 4; return v
    def f(s, n=1):
        v = struct.unpack_from('<%df' % n, s.b, s.p); s.p += 4 * n
        return v if n > 1 else v[0]
    def cstr(s):
        e = s.b.index(b'\0', s.p); v = s.b[s.p:e].decode('latin-1'); s.p = e + 1; return v

def read_xmodel(data):
    r = R(data)
    ver = r.s16()
    if ver != 25: raise ValueError('xmodel version %d' % ver)
    m = {'flags': r.u8(), 'mins': r.f(3), 'maxs': r.f(3), 'physpreset': r.cstr()}
    lods = []
    for i in range(4):
        d = r.f(); fn = r.cstr(); lods.append({'dist': d, 'file': fn})
    m['collLod'] = r.i32()
    ncs = r.i32()
    colls = []
    for _ in range(ncs):
        nt = r.i32()
        tris = []
        for _ in range(nt):
            plane = r.f(4); svec = r.f(4); tvec = r.f(4)   # plane + barycentric edge functions
            tris.append((plane, svec, tvec))
        mins, maxs = r.f(3), r.f(3)
        bone, contents, sflags = r.i32(), r.i32(), r.i32()
        colls.append({'planes': [p for p, _, _ in tris], 'tris': tris, 'mins': mins, 'maxs': maxs, 'bone': bone,
                      'contents': contents & 0xDFFFFFFB, 'surfFlags': sflags})
    m['numCollSurfs'] = ncs
    m['collSurfs'] = colls
    for L in lods:
        if not L['file']: continue
        n = r.u16(); L['materials'] = [r.cstr() for _ in range(n)]
    m['lods'] = [L for L in lods if L['file']]
    m['parts'] = lods[0]['file']
    return m

def read_parts(data):
    r = R(data)
    if r.u16() != 25: raise ValueError('parts version')
    nchild = r.u16(); nroot = r.u16(); n = nroot + nchild
    bones = [{'parent': -1, 'trans': (0, 0, 0), 'quat': (0, 0, 0, 1)} for _ in range(n)]
    for i in range(nroot, n):
        idx = r.u8()
        t = r.f(3)
        qx, qy, qz = r.s16(), r.s16(), r.s16()   # ConsumeQuatNoSwap: 3 shorts, w derived
        q = [qx / 32767.0, qy / 32767.0, qz / 32767.0]
        w = 1.0 - (q[0] ** 2 + q[1] ** 2 + q[2] ** 2)
        q.append(math.sqrt(w) if w > 0 else 0.0)
        bones[i].update(parent=i - (i - idx) if False else idx, trans=t, quat=tuple(q))
    for b in bones: b['name'] = r.cstr()
    return {'numRoot': nroot, 'bones': bones}

def read_surfs(data):
    r = R(data)
    if r.s16() != 25: raise ValueError('surfs version')
    ns = r.s16(); surfs = []
    for _ in range(ns):
        tile = r.u8(); r.u16(); vc = r.u16(); tc = r.u16()
        vlists = []
        while True:
            c = r.u16()
            if not c: break
            vlists.append((c, r.u16()))
        rigid = sum(c for c, _ in vlists)
        deformed = rigid != vc
        if deformed: vlists = []
        if len(vlists) != 1: r.u16()  # numblends
        V = []
        for _ in range(vc):
            n = r.f(3); col = r.b[r.p:r.p + 4]; r.p += 4
            uv = r.f(2); r.p += 24  # binormal, tangent
            if len(vlists) == 1:
                o = r.f(3); bone = vlists[0][1]; w = []
            else:
                nw = r.u8(); bone = r.u16(); o = r.f(3)
                w = [(r.s16(), r.u16()) for _ in range(nw)]
            V.append({'n': n, 'c': bytes(col), 'uv': uv, 'p': o, 'bone': bone, 'w': w})
        T = [struct.unpack_from('<3H', r.b, r.p + 6 * i) for i in range(tc)]; r.p += 6 * tc
        surfs.append({'tile': tile, 'verts': V, 'tris': T, 'vlists': vlists, 'deformed': deformed})
    return surfs, r.p == len(r.b)
