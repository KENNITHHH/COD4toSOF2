# CoD4toSOF2

Converts a **Call of Duty 4 (IW3) Radiant source map** into a **Soldier of
Fortune II: Double Helix** map you can open in Radiant, compile with q3map2
and play.

It is not a rip of a finished level: it reads the CoD4 `.map` source and the
game's own assets, and rebuilds the level as ordinary SOF2 brushes, patches,
models, shaders and textures. The result stays editable.

Sibling project: **SourceToSOF2** (Counter-Strike: Source → SOF2).

---

## What it does

| CoD4 | becomes |
|---|---|
| brushes, patches, prefabs (inlined recursively) | SOF2 brushes and `patchDef2` |
| `xmodel` props | `.md3` models, with CoD4's own LOD chain as `_1` / `_2` |
| materials (`.iwi` images, techsets, blend modes) | a `.shader` file + `.tga` textures |
| xmodel collision meshes | clip brushes |
| `setExpFog` from the map's GSC (also out of the fastfile) | SOF2 `fogparms` + `distancecull` |
| vertex-alpha ground layers | one blended `.lwo` layer model |
| sun / lighting keys | worldspawn light keys for `-light` |

It also works around the engine's limits for you: `MAX_MAP_ENTITIES`,
`MAX_MODELS`, the 256-entity snapshot cap, `MAX_SHADERS` (shader × lightmap
pairs) and the single-allocation `.bsp` size. The conversion log's
`prop budget:` lines say exactly what it spent and what it left out.

---

## Install

### Ready-made (no Python)

1. Download `CoD4toSOF2-vX.Y.zip` from Releases.
2. Unzip anywhere.
3. Run `CoD4toSOF2.exe`.

### From source

Needs **Python 3.8 or newer**.

```
pip install pillow numpy
pip install customtkinter      # optional, nicer looking GUI
python cod4tosof2_gui.py
```

All of the `.py` files must stay in one folder.

---

## What you need

| | |
|---|---|
| **CoD4** | an installed copy — the `.iwd` files hold the stock textures and models |
| **CoD4 Mod Tools** | for `map_source\` (the `.map` sources and `prefabs\`) and `raw\` |
| **SOF2** | a `base\` folder to install into |
| **q3map2** | from NetRadiant or GtkRadiant, for compiling |

---

## Using it

1. **CoD4 folder** – the install (holds `main\*.iwd`).
2. **ModTools folder** – holds `map_source\` and `raw\`.
3. **Map** – picked from `map_source\`; MP maps are listed first.
4. **Output folder** – where the converted maps go.
5. **SOF2 base folder** – for compiling, e.g. `...\Soldier of Fortune II - Double Helix\base`.
6. **q3map2.exe** – for compiling.

Press **Convert**, then **Compile**. Tick **fast** for a quick test build
(`-vis -fast`); untick it for the real one.

Output per map:

```
<output>\<mapname>\
    maps\<mapname>.map
    models\cod4\            .md3 props, LODs, cells, blend\<tag>_layers.lwo
    shaders\cod4_<mapname>.shader
    textures\cod4\          .tga
<output>\_cache\<mapname>\  the CoD4 assets it pulled out, kept for re-runs
```

**Compile** copies that into `base\`, adds the shader to `shaderlist.txt` and
runs q3map2 `-bsp -vis -light`. If the map ends up with too many
shader × lightmap pairs it builds again with coarser lightmaps by itself
(`_lightmapscale` 1 → 1.5 → 2 → 3) and tells you where it landed.

---

## Command line

The GUI is a front end; every step also runs on its own.

```
python cod4_collect_assets.py --map <mp_x.map> --root <CoD4> --root <ModTools> ^
                              --prefabs <map_source> --out <cache> --extract

python cod4tosof2.py --map <mp_x.map> --assets <cache> --prefabs <map_source> ^
                     --out <outdir>

compile_cod4_map.bat <mapname> [fast]
```

`cod4tosof2.py` runs the four conversion stages in order:
`iwmap2map` → `xmodel2md3` → `iwmap2map` (again, with model offsets) →
`cod4mat2shader`. Each takes `--help`.

Useful `iwmap2map.py` switches when a map needs a nudge:

| switch | what for |
|---|---|
| `--prop-vertex-budget N` | how many prop vertices may be baked into the `.bsp` (default 60000) |
| `--clutter-radius N` | props smaller than this are left out (default 15) |
| `--drop-model RE` | leave out every model matching a regex, e.g. `foliage_` |
| `--fog s,half,r,g,b` / `--no-fog` / `--fog-max N` | override the fog taken from the GSC |
| `--no-auto-detail` | keep every brush structural (much slower `-vis`) |
| `--no-model-static` | bake every prop into the `.bsp` instead |

Running `compile_cod4_map.bat` by hand needs its three paths. Copy
`paths.cmd.example` to `paths.cmd` and edit it, or set `Q3MAP2`, `GAMEROOT`
and `OUTDIR` in the environment. The GUI passes them itself.

If a compile ever stops with a batch error, set `COD4TOSOF2_DEBUG=1` first
and the .bat echoes every line it runs, so the failing one is the last shown.

---

## Building the exe

On Windows, from the source folder:

```
build_exe.bat
```

That installs PyInstaller, Pillow, numpy and CustomTkinter, then writes
a single `dist\CoD4toSOF2.exe`. That one file is the release - the `.bat`,
`paths.cmd.example` and this README are inside it.

`.github/workflows/build-exe.yml` does the same on GitHub's runners and
attaches the zip to any tag starting with `v`.

Two things about the build, both deliberate:

* It is a **console** app whose console is hidden at startup. Each conversion
  step runs as its own process and reports back over a pipe, which needs a
  real stdout; a windowed build has none.
* If the exe **will not start**, run it from a command prompt rather than by
  double-clicking, so the output stays on screen. `CoD4toSOF2.exe --diag`
  prints what is bundled, what imports and whether Tk can open a window;
  `--console` starts the GUI with the console left visible. A startup failure
  is also written to `cod4tosof2_crash.log` next to the exe. For a failure
  before Python even starts, rebuild with `set COD4TOSOF2_BOOTDEBUG=1` first
  and the bootloader will log every file it unpacks.
* It is **onefile**, and the exe starting copies of itself for each step does
  *not* unpack the archive again every time. PyInstaller's bootloader passes
  `_PYI_APPLICATION_HOME_DIR` down to the child, and only the first process
  (`PYI_PROCESS_LEVEL_PARENT`) extracts, so the steps reuse what the parent
  already unpacked. The cost is a slower startup, once per launch.

---

## Files

**Entry points**

| | |
|---|---|
| `cod4tosof2_gui.py` | the GUI (this is what the exe runs) |
| `cod4tosof2.py` | runs the four conversion stages |
| `cod4_collect_assets.py` | pulls just the assets a map needs out of `.iwd` / `raw` / fastfiles |
| `cod4tosof2_run.py` | how a step is started, from source or from the exe |

**Conversion stages**

| | |
|---|---|
| `iwmap2map.py` | CoD4 `.map` → SOF2 `.map` (the big one) |
| `xmodel2md3.py` | `xmodel` → `.md3` (+ LODs, collision boxes) |
| `cod4mat2shader.py` | materials → `.shader` + `.tga` |

**Readers and writers**

| | |
|---|---|
| `xmodel_v25.py`, `material_iw3.py`, `iwi2tga.py`, `cod4_ff.py` | CoD4 formats: xmodel, material, image, fastfile |
| `cod4_collision.py` | collision mesh → boxes |
| `cod4_names.py` | per-map shader naming |
| `sof2_shader.py`, `sof2_md3.py` | SOF2 `.shader` and `.md3` writers |

`sof2_shader.py` and `sof2_md3.py` are shared with **SourceToSOF2**, where
they are called `vmt2shader.py` and `mdl_to_md3.py`. Only the name differs,
so the two projects can stay in sync. CoD4toSOF2 uses their SOF2-output half;
the Source `.vmt` / `.mdl` reading half comes along unused.

**Compiling**

`compile_cod4_map.bat`, `bsp_fix_leafbounds.py`, `map_set_key.py`,
`bsp_static_pvs.py`, `run_tee.py`

**Extras**

`lod_test.py` (colour each LOD level to see when SOF2 switches),
`lightmap_pages.py`

---

## Known limits

* **Scripted gameplay does not convert.** CoD4 GSC scripts, destructible
  pieces, moving platforms and bomb sites are not SOF2 entities. Spawns,
  lights and triggers are; the rest is left out and listed in the log.
* **Players and weapons are untouched** — this converts the level only.
* **Big maps spend a budget, and it sets itself.** SOF2 is a 32-bit process
  and reads the whole `.bsp` in one allocation, so there is a size past which
  a map will not load. The converter stays under it by leaving out distant
  scenery and stepping props down to lower LODs.

  Nothing to set. A `.map` cannot predict its own `.bsp` - the lightmaps and
  vis that dominate the file do not exist yet - so instead the compile step
  records what the `.bsp` actually weighed and how many baked prop vertices
  went into it, into `cod4tosof2_budget.json` in your output folder. The next
  Convert of that map reads those two numbers and aims at 62 MB. A map you
  have never compiled starts at 60,000 vertices; one compile later the figure
  is measured, and an overshoot pulls itself back on the run after. Each step
  is capped at double or half, so a bad reading cannot produce a map that is
  empty or that will not load.

  The size that fits depends on **`com_hunkMegs`**, not on the engine alone.
  The hunk is reserved as one block at startup, so a large hunk eats the
  contiguous address space the `.bsp` load then needs: at `com_hunkMegs 1024`
  a map can die in `Z_Malloc` that loads fine at the stock `512`/`256`. 62 MB
  is aimed at the stock setting. If you want a different target, change
  `BSP_TARGET_MB` in `bsp_budget.py`; `--prop-vertex-budget` still overrides
  the whole mechanism for one run, and `--drop-model` drops clutter by name.
* **MAX_SHADERS is counted from the .bsp, not from q3map2.** The renderer
  registers one shader for every (material, lightmap page) pair a surface
  uses, and all of them share the 2048 pool with the game's own menu, weapon,
  player and effect shaders. q3map2's "unique lightmap/shader combinations"
  line is about half the real figure (mp_strike 628 vs 1135, mp_derail 1030 vs
  2057), so the compile step counts it out of the finished .bsp instead and
  rebuilds with coarser lightmaps (`_lightmapscale` 1.5, 2, 3, 4, 6) until it
  is under 1400. `bsp_budget.py pairs <bsp> -v` prints the count and the
  materials spread over the most pages.
* **Baked terrain is vertex-lit once a map goes over MAX_SHADERS**, because
  its lightmaps are the biggest single consumer of the 2048 pool: on mp_derail
  the `_bsp` ground shaders held 669 of 2262 pairs and touched 322 of 359
  lightmap pages. It is drawn as one stage rather than the doubled form baked
  props use - vertex lighting has no per-luxel shadowing, so doubling it
  saturates. `--terrain-2x` restores the doubled form if a map comes out dark.
  Note that `_lightmapscale` cannot substitute for this: worldspawn's value
  does not reach misc_model surfaces, so coarsening it barely moves a map
  whose lightmaps are mostly on baked geometry.

  The decision is sticky, recorded as `vl_terrain` in
  `cod4tosof2_budget.json`. It has to be: once the terrain is vertex-lit the
  next compile measures the map *under* the budget, so reading the live count
  would switch it back and the setting would oscillate. For a record written
  before this was stored, `bsp_budget.py mark <output folder> <map>` sets it
  without waiting for another compile.
* **Patches are split for SOF2's collision, not just q3map2's.** The engine
  builds one or two collision facets per patch grid cell plus a plane per
  facet face and border, and aborts the map load with `MAX_PATCH_PLANES` once
  one patch needs too many. Control grids coming out of a CoD mesh are dense
  enough that the engine never subdivides them further, so the converter caps
  each patch at `MAX_PATCH_CELLS` cells (`iwmap2map.py`) and cuts the longer
  axis first. On mp_derail that is 21 of 10,889 patches and 41 extra pieces.
* **Lighting is tuned in `paths.cmd`.** q3map2 bakes it as
  `pow(v/255, 1/gamma)*255`, and the default gamma of 2.2 turns a mid-grey of
  128 into 186 and squeezes everything above 192 into the last few values
  before white - on a bright map that reads as flat, blown-out white.
  `COD4TOSOF2_GAMMA`, `COD4TOSOF2_EXPOSURE` and `COD4TOSOF2_GRIDSCALE` change
  it without touching the code; `paths.cmd.example` explains each one.
* **Lighting is not CoD4's.** CoD4's baked light does not transfer; the map is
  lit again with q3map2 from the sun and lights in the source map.
* **CoD4 grades the finished frame, and SOF2 cannot.** A map's `.vision` file
  tints the highlights one way and the shadows another, so a map's raw light
  is often nothing like the image CoD shows: mp_strike's worldspawn sun is
  `0.5 0.6 1.0`, a blue sun behind a warm picture. The tint is a per-channel
  multiply, so it folds into the baked light exactly, and `cod4_vision.py`
  carries the stock grades for when the `.vision` file is not in the map's
  fastfile. What cannot fold in is the contrast curve, the brightness offset
  and the share of the desaturation that lands on the textures rather than on
  the light - those stay in `COD4TOSOF2_GAMMA`.
  The file is looked for in four places, in this order: the map's own
  fastfile, a shared `common*` / `code_post_gfx*` fastfile, `raw/vision/`, and
  the indexed roots - which includes the contents of every `.iwd`, and is
  where a stock install actually keeps them. mp_derail is the case that needs
  the last one: it has no fastfile of its own and is in neither published
  table, but `vision/mp_derail.vision` is sitting inside an `.iwd` and gets
  read from there. The `vision:` line in the convert log names the source, so
  a map that ends up with no grade says so.

### Where this is deliberately not CoD

Everything that can come from the map comes from the map: the sun colour and
angle, the ambient, the light radii and intensities, the fog distance and
colour, the far plane. These are the places where a number was chosen instead,
and why. Each one is a knob.

* **`_minlight` / `_minvertexlight` 24.** CoD has no flat floor under its
  shadows. This one exists because q3map2 samples vertex light at a surface's
  corners, and a decal lying flat on a floor has its corners *in* the floor
  plane, which reads as fully occluded - mp_derail's floor papers and the
  warehouse grating came out as black shapes. It only lifts what is darker, so
  nothing already lit moves. `--min-vertex-light 0` turns it off.
* **`-gridscale 4.59`.** The light grid lights `model_static` props; the
  lightmaps light the world. The two do not come out at the same level in
  SOF2 and this is the ratio that made props sit right against the world
  around them. It was tuned under the old lighting and has not been re-tuned
  since the sun, sky and light entities were all re-derived - if props look
  bright or dark against the walls, this is the number to question first.
  `COD4TOSOF2_GRIDSCALE` in `paths.cmd`.
* **`SUN_TARGET_LUXEL` 140.** Two engines have no shared absolute brightness,
  so one end has to be pinned. A sun-facing surface is aimed at 140 of 255
  after gamma, which leaves headroom under the point where the world shader's
  2x modulate clips (`255 / 2a`, so 150 on snow). Everything else - the sky
  light, every light entity - is derived from that one anchor, so moving it
  moves the whole map together.
* **`COD4TOSOF2_GAMMA` 1.6.** The vision file's tint and desaturation fold
  into the baked light exactly; its contrast curve and brightness offset
  cannot, because they are not a per-channel multiply of the light. This is
  the stand-in for them.
* **Spotlights become point lights.** q3map2 only makes a spotlight from a
  light that targets another entity, and it forces spots back to
  inverse-square where the radius cannot be matched.
* **Props are thinned to fit the `.bsp`.** Clutter under a 15-unit placed
  radius is left out, scenery outside the play area steps down its LOD or
  goes, and foliage keeps collision only below 45% of its height. The convert
  log names every prop it dropped. `--clutter-radius 0` and
  `--prop-vertex-budget 0` turn the two biggest off.


### Licence

CoD4toSOF2 is MIT licensed - see `LICENSE`. Same as SourceToSOF2, which it
shares `sof2_shader.py` and `sof2_md3.py` with.

`CoD4toSOF2.exe` additionally contains Pillow, numpy, customtkinter and the
Python runtime, bundled by PyInstaller. All permissive, none of them
restricting what the exe may be used for, but each asks that its licence text
accompanies the binary - so `build_exe.bat` runs `collect_licenses.py`, which
reads those texts out of the packages it just installed and writes
`THIRD-PARTY-LICENSES.txt`. The result goes both inside the exe and next to
it, and the build stops rather than shipping an exe whose licence file is
missing a component.

Not covered by any of that, and deliberately so: q3map2 is run as a separate
program rather than linked, so NetRadiant's GPL does not reach this code; and
no Call of Duty 4 asset ships with the tool.

* Converting a map does not give you the right to redistribute Activision's
  assets. Keep converted stock maps to yourself or to people who own CoD4.
