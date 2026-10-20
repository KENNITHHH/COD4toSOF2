#!/usr/bin/env python3
"""
cod4tosof2.py - one-shot CoD4 (IW3) Radiant map -> SOF2 conversion.

Runs, in order:
  1. iwmap2map.py      (first pass: manifest of the materials + models used)
  2. xmodel2md3.py     models/cod4/*.md3 + cod4_md3_offsets.json
  3. iwmap2map.py      final maps/<name>.map, big models placed by their offsets
  4. cod4mat2shader.py shaders/cod4_<name>.shader + textures/cod4/*.tga

Everything is derived from the CoD4 data; there is nothing to configure.

Usage:
  python cod4tosof2.py --map <mp_x.map> --assets <collected assets> --out <dir>
                       [--prefabs <folder containing prefabs/>]

The assets folder is what cod4_collect_assets.py writes. The GUI
(cod4tosof2_gui.py) runs that step and this one for you.

Result layout under --out (install into SOF2's base folder, or a pk3):
  maps/<name>.map   models/cod4/   shaders/cod4_<name>.shader   textures/cod4/

Compile it with compile_cod4_map.bat, or by hand with q3map2 -bsp -meta
-rename (baked props use the <shader>_bsp forms).
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cod4tosof2_run import NO_WINDOW, child_env, step_cmd  # noqa: E402


def run(step, args):
    cmd = step_cmd(step, args)
    print("\n>> %s %s" % (step, " ".join(str(a) for a in args)), flush=True)
    # Relay the step's output explicitly. With CREATE_NO_WINDOW and no std
    # handles given, Windows gives the child NO stdout at all (it does not
    # inherit the GUI's pipe), so every step's report was silently lost.
    p = subprocess.Popen(cmd, creationflags=NO_WINDOW, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=child_env())
    for raw in p.stdout:
        sys.stdout.write(raw.decode("utf-8", "replace"))
        sys.stdout.flush()
    if p.wait() != 0:
        sys.exit("step failed: %s" % step)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", required=True)
    ap.add_argument("--assets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefabs")
    # passed straight through to the map step: how much of the .bsp the baked
    # props may use, and props to leave out entirely. What the .bsp can afford
    # depends on the player's com_hunkMegs - see "Known limits" in README.md.
    ap.add_argument("--prop-vertex-budget", type=int)
    ap.add_argument("--drop-model", action="append", default=[])
    a = ap.parse_args()

    name = os.path.splitext(os.path.basename(a.map))[0]
    out = os.path.abspath(a.out)
    map_out = os.path.join(out, "maps", name + ".map")
    manifest = os.path.join(out, name + "_cod4_manifest.json")
    offsets = os.path.join(out, "models", "cod4", "cod4_md3_offsets.json")
    pf = ["--prefabs", a.prefabs] if a.prefabs else []
    if a.prop_vertex_budget:
        pf += ["--prop-vertex-budget", str(a.prop_vertex_budget)]
    for m in a.drop_model:
        pf += ["--drop-model", m]

    run("map",     [a.map, map_out, "--assets", a.assets, "--manifest", manifest] + pf)
    run("models",  ["--assets", a.assets, "--out", out, "--manifest", manifest])
    run("map",     [a.map, map_out, "--assets", a.assets, "--manifest", manifest,
                    "--model-offsets", offsets] + pf)
    run("shaders", [manifest, "--assets", a.assets, "--out", out, "--name", "cod4_" + name])
    print("\ndone ->", out)


if __name__ == "__main__":
    main()
