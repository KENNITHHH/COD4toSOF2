"""
bsp_static_pvs.py - how many model_static entities can be potentially visible
at once in a compiled SOF2 (RBSP) map.

idTech3 sends each client at most MAX_SNAPSHOT_ENTITIES (256) entities per
snapshot - players, items, effects AND every model_static whose origin is in a
cluster the player's cluster can see (PVS). Past that the server silently drops
the rest: props just don't show (mp_strike with ~728 model_statics: cars missing).

This reads the .bsp (after -vis), finds each model_static's origin cluster and,
for every cluster, counts the model_statics in its PVS. Run it after compiling:

  python bsp_static_pvs.py <map.bsp> [--limit 180]

Fast vis (-vis -fast) marks most of the map visible from everywhere, so its
numbers are close to "all of them"; a full -vis is what a release build uses.
"""
import argparse
import re
import struct
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bsp")
    ap.add_argument("--limit", type=int, default=180,
                    help="warn above this many (256 minus players and game entities); default %(default)s")
    a = ap.parse_args()
    d = open(a.bsp, "rb").read()
    if d[:4] != b"RBSP":
        sys.exit("not an RBSP (SOF2) .bsp")
    L = [struct.unpack_from("<ii", d, 8 + i * 8) for i in range(18)]
    ents = d[L[0][0]:L[0][0] + L[0][1]].decode("latin-1")
    po, no, lo = L[2][0], L[3][0], L[4][0]
    vo, vl = L[16]
    if vl < 8:
        sys.exit("no vis data - run -vis first")
    ncl, rowb = struct.unpack_from("<2i", d, vo)

    def leaf_of(p):
        n = 0
        while n >= 0:
            pl, c0, c1 = struct.unpack_from("<3i", d, no + n * 36)
            nx, ny, nz, dist = struct.unpack_from("<4f", d, po + pl * 16)
            n = c0 if nx * p[0] + ny * p[1] + nz * p[2] - dist >= 0 else c1
        return -n - 1

    statics = []
    in_solid = 0
    for block in re.findall(r"\{([^{}]*)\}", ents):
        kv = dict(re.findall(r'"([^"]*)" "([^"]*)"', block))
        if kv.get("classname") != "model_static" or "origin" not in kv:
            continue
        p = [float(v) for v in kv["origin"].split()]
        c = struct.unpack_from("<i", d, lo + leaf_of(p) * 48)[0]
        if c < 0:
            in_solid += 1                 # origin in solid: never drawn at all
            continue
        statics.append((c, kv.get("model", "")))
    if not statics:
        print("no model_static entities")
        return
    per_cluster = [0] * ncl
    for c, _ in statics:
        per_cluster[c] += 1
    occupied = [c for c in range(ncl) if per_cluster[c]]
    worst, worst_c, over = 0, -1, 0
    for v in range(ncl):
        row = vo + 8 + v * rowb
        n = sum(per_cluster[c] for c in occupied if d[row + (c >> 3)] & (1 << (c & 7)))
        if n > worst:
            worst, worst_c = n, v
        if n > a.limit:
            over += 1
    print("model_static: %d (origin in solid, never drawn: %d)" % (len(statics), in_solid))
    print("clusters: %d; most model_statics potentially visible from one cluster: %d (cluster %d)"
          % (ncl, worst, worst_c))
    if over:
        print("WARNING: %d of %d clusters see more than %d - props will go missing there "
              "(snapshot limit 256 incl. players)" % (over, ncl, a.limit))
    else:
        print("OK: no cluster sees more than %d" % a.limit)


if __name__ == "__main__":
    main()
