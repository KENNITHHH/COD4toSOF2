#!/usr/bin/env python3
"""
gui_smoke_test.py - build the whole GUI against a fake tkinter, headless.

    python gui_smoke_test.py

Catches the mistakes that otherwise only show up as an exe that opens a
console for two seconds and dies: a widget reading an attribute before it is
assigned, a renamed module the GUI still imports, a step the .bat calls that
no longer exists.

The fake widgets answer only the methods in METHODS. That matters: real
tkinter raises AttributeError for an unknown attribute (Tk.__getattr__ tries
to treat it as a Tcl command and fails), so a stub that answers every name
with a no-op callable would hide exactly the bug this is here to find.
"""
import ast
import io
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
CALLS = []

METHODS = frozenset("""
pack pack_forget pack_configure grid grid_forget grid_configure grid_remove
place bind bind_all unbind configure config cget after after_cancel
columnconfigure rowconfigure grid_columnconfigure grid_rowconfigure
title geometry minsize maxsize resizable iconbitmap iconphoto protocol
destroy quit update update_idletasks mainloop focus focus_set lift
insert delete get set see yview xview index search tag_add tag_config
tag_configure state instate selection_set current invoke wait_window
winfo_children winfo_width winfo_height winfo_exists attributes deiconify
withdraw option_add bell clipboard_clear clipboard_append nametowidget
""".split())


class W:
    def __init__(self, *a, **kw):
        CALLS.append((type(self).__name__, kw))

    def __getattr__(self, name):
        if name in METHODS:
            return lambda *a, **kw: self
        raise AttributeError("%r object has no attribute %r" % (type(self).__name__, name))


class Var:
    def __init__(self, value=None, **kw):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v

    def trace_add(self, *a, **kw):
        pass


def fake_tkinter():
    tk = types.ModuleType("tkinter")
    for n in ("Tk", "Frame", "Label", "Button", "Entry", "Text", "Scrollbar", "Toplevel"):
        setattr(tk, n, type(n, (W,), {}))
    tk.StringVar = tk.BooleanVar = tk.IntVar = Var
    tk.END = "end"
    ttk = types.ModuleType("tkinter.ttk")
    for n in ("Frame", "Label", "Entry", "Button", "Combobox", "Checkbutton",
              "Progressbar", "Scrollbar", "Style", "Notebook"):
        setattr(ttk, n, type(n, (W,), {}))
    fd = types.ModuleType("tkinter.filedialog")
    fd.askopenfilename = fd.askdirectory = lambda *a, **kw: ""
    mb = types.ModuleType("tkinter.messagebox")
    mb.showerror = mb.showinfo = mb.showwarning = lambda *a, **kw: None
    mb.askyesno = lambda *a, **kw: True
    tk.ttk, tk.filedialog, tk.messagebox = ttk, fd, mb
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": fd,
                        "tkinter.messagebox": mb,
                        "tkinter.font": types.ModuleType("tkinter.font")})
    # a real customtkinter would need a display; the GUI falls back to ttk
    sys.modules.setdefault("customtkinter", None)


def check(name, bad):
    print("%-34s %s" % (name, "ok" if not bad else "FAIL  %s" % (bad,)))
    return not bad


def main():
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    fake_tkinter()
    ok = True

    for f in sorted(os.listdir(HERE)):
        if f.endswith(".py"):
            ast.parse(io.open(f, encoding="utf-8").read(), f)
    ok &= check("every module parses", None)

    import cod4tosof2_run as R
    ok &= check("step modules exist",
                [m for m in R.STEPS.values() if not os.path.exists(m + ".py")])

    import cod4tosof2_gui as G
    src = io.open("cod4tosof2_gui.py", encoding="utf-8").read()
    req = ast.literal_eval(src.split("REQUIRED = ")[1].split("]")[0] + "]")
    ok &= check("REQUIRED files exist", [r for r in req if not os.path.exists(r)])

    spec = io.open("CoD4toSOF2.spec", encoding="utf-8").read()
    mods = (ast.literal_eval(spec.split("STEP_MODULES = ")[1].split("]")[0] + "]")
            + ast.literal_eval(spec.split("LIB_MODULES = ")[1].split("]")[0] + "]"))
    ok &= check("spec hidden imports exist",
                [m for m in mods if not os.path.exists(m + ".py")])
    ok &= check("spec data files exist",
                [f for f in ("compile_cod4_map.bat", "paths.cmd.example", "README.md")
                 if not os.path.exists(f)])

    bat = io.open("compile_cod4_map.bat", encoding="utf-8", errors="replace").read()
    labels = set(re.findall(r"^:(\w+)", bat, re.M))
    gotos = set(re.findall(r"goto (\w+)", bat)) | set(re.findall(r"call :(\w+)", bat))
    ok &= check("every goto has a label", sorted(gotos - labels))
    ok &= check("every .bat step is known",
                sorted(set(re.findall(r"%PYCMD% (\w+)", bat)) - set(R.STEPS)))

    # cmd parses a parenthesised block whole when it reaches the "if", before
    # it ever tests the condition, so ANY unescaped paren inside one breaks
    # every run - balanced or not. "echo ... (see the top of this file)."
    # shipped once and reported ". was unexpected at this time." every time:
    # the "(" opened a nested group, the ")" closed it, and "." was left over.
    depth, bad = 0, []
    for i, line in enumerate(bat.splitlines(), 1):
        t = line.strip()
        if depth and not t.lower().startswith("rem"):
            body = t
            if body.endswith("("):                 # this line opens a nested block
                body = body[:-1]
            while body.startswith(")"):            # ")" or ") else ("
                body = body[1:].lstrip()
                if body.lower().startswith("else"):
                    body = body[4:].lstrip().rstrip("(").rstrip()
            if re.search(r"(?<!\^)[()]", body):
                bad.append("line %d: %s" % (i, t[:60]))
        depth = max(depth + line.count("(") - line.count(")"), 0)
    ok &= check("no unescaped parens in .bat blocks", bad)

    # the real test: construct every widget
    app = G.App()
    ok &= check("App() builds (%d widgets)" % len(CALLS), None)
    # the prop budget is measured, never typed - nothing in the GUI sets it
    gsrc = io.open("cod4tosof2_gui.py", encoding="utf-8").read()
    ok &= check("GUI sets no prop budget by hand",
                [t for t in ("--prop-vertex-budget", "COD4TOSOF2_BSPMAX") if t in gsrc])
    ok &= check("bsp_budget is a registered step",
                [] if "bsp_budget" in R.STEPS else ["missing from STEPS"])
    import bsp_budget
    d, why = bsp_budget.suggest(os.path.join(HERE, "no_such_output"), "nope", 60000)
    ok &= check("budget falls back without a measurement",
                [] if d == 60000 and "no compile measured yet" in why else [why])
    bsp_budget.FILE = "_smoke_budget.json"
    tmp = os.path.join(HERE, "_smoke_budget_dir")
    os.makedirs(tmp, exist_ok=True)
    try:
        bsp_budget.record(tmp, "m", int(52.3 * 1048576), 63586)
        got, why2 = bsp_budget.suggest(tmp, "m", 60000)
        # 52.3 MB from 63,586 verts, aiming at 62 MB -> more, but not unbounded
        ok &= check("budget grows toward the target (%d)" % got,
                    [] if 63586 < got <= 63586 * bsp_budget.MAX_STEP else [why2])
        bsp_budget.record(tmp, "m", int(70.0 * 1048576), 200000)
        shrunk, _ = bsp_budget.suggest(tmp, "m", 60000)
        ok &= check("budget shrinks after an oversized .bsp (%d)" % shrunk,
                    [] if shrunk < 200000 else ["did not shrink"])
        # the terrain decision must not oscillate: once a map is over the
        # shader budget it stays vertex-lit, because the next compile measures
        # it UNDER budget precisely because the switch worked
        bsp_budget.record(tmp, "osc", 86000000, 151498, 2085)
        on1 = bsp_budget.vertex_light_terrain(tmp, "osc")
        bsp_budget.record(tmp, "osc", 73641752, 151498, 1317)
        on2 = bsp_budget.vertex_light_terrain(tmp, "osc")
        ok &= check("terrain decision is sticky",
                    [] if (on1 and on2) else ["over=%s then under=%s" % (on1, on2)])
        bsp_budget.record(tmp, "fine", 50000000, 63586, 1135)
        ok &= check("a map under budget keeps lightmaps",
                    [] if not bsp_budget.vertex_light_terrain(tmp, "fine") else ["switched"])
        # Every rung of the _lightmapscale ladder is a full rebuild, and vis -
        # the expensive part - recomputes something _lightmapscale cannot
        # change. mp_strike paid 4 x 570 s of vis to walk 1, 1.5, 2, 3.
        bad = []
        if bsp_budget.start_lmscale(tmp, "never_built") != "1":
            bad.append("a map with no record should start at 1")
        bsp_budget.record(tmp, "lm", 60000000, 180533, 1192, "3")
        if bsp_budget.start_lmscale(tmp, "lm") != "3":
            bad.append("did not remember 3")
        bsp_budget.record(tmp, "lm", 60000000, 180533, 700, "3")
        if bsp_budget.start_lmscale(tmp, "lm") != "2":
            bad.append("well under target should try one step finer")
        bsp_budget.record(tmp, "lm", 60000000, 180533, 700, "1")
        if bsp_budget.start_lmscale(tmp, "lm") != "1":
            bad.append("cannot go finer than 1")
        ok &= check("the lightmap scale that worked is remembered", bad)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # md3_zbounds feeds the "is this prop raised above the play area or is it
    # real scenery" test. If it silently returned 0,0 every tall prop would be
    # scenery again - which is how mp_strike's one statue got deleted.
    import struct as _st
    import tempfile as _tf
    import iwmap2map
    md3d = _tf.mkdtemp()
    try:
        frame = _st.pack("<3f3f3ff16s", -8.0, -8.0, -140.0, 8.0, 8.0, 260.0,
                         0.0, 0.0, 0.0, 300.0, b"\0" * 16)
        head = _st.pack("<4si64s9i", b"IDP3", 15, b"t".ljust(64, b"\0"),
                        0, 1, 0, 0, 0, 108, 108 + len(frame), 108 + len(frame), 108 + len(frame))
        p = os.path.join(md3d, "tall.md3")
        io.open(p, "wb").write(head + frame)
        zlo, zhi = iwmap2map.md3_zbounds(p)
        ok &= check("md3_zbounds reads the mesh's vertical size (%g)" % (zhi - zlo),
                    [] if abs(zlo + 140.0) < 0.01 and abs(zhi - 260.0) < 0.01
                    else ["got %g..%g" % (zlo, zhi)])
        ok &= check("md3_zbounds survives a file that is not an md3",
                    [] if iwmap2map.md3_zbounds(os.path.join(md3d, "nope.md3")) == (0.0, 0.0)
                    else ["did not return 0,0"])
    finally:
        import shutil as _sh
        _sh.rmtree(md3d, ignore_errors=True)

    # CoD's own depth bias must reach is_decal for every kind of material.
    # Restricting the decal test to blended materials left posters, floor
    # grates and window planes z-fighting with the wall behind them.
    import cod4mat2shader as C
    cases = [({"polygonOffset": 1, "sortKey": 4}, "alphatest", True, "alphatest with CoD bias"),
             ({"polygonOffset": 2, "sortKey": 4}, "opaque", True, "opaque with CoD bias"),
             ({"polygonOffset": 0, "sortKey": 12}, "opaque", True, "decal sortKey, no bias"),
             ({"polygonOffset": 0, "sortKey": 4}, "opaque", False, "plain wall"),
             ({"polygonOffset": 3, "sortKey": 4}, "opaque", False, "shadowmap bias is not a decal"),
             ({"polygonOffset": 1, "sortKey": 5}, "sky", False, "sky is never a decal")]
    bad = ["%s -> %s" % (why, C.is_decal(i, k)) for i, k, want, why in cases
           if C.is_decal(i, k) != want]
    ok &= check("is_decal reads CoD's depth bias", bad)

    # CoD's vision file is what makes mp_strike warm. The sun colour in the
    # worldspawn is blue (0.5 0.6 1.0), so a warm lightTint must come out as a
    # sun whose red beats its blue - otherwise the grade is not reaching the
    # baked light and the map stays blue.
    sun = {"suncolor": "0.5 0.6 1.0", "sunlight": "1.3",
           "sundirection": "-25 315 0", "ambient": "0.1", "_color": "0.5 0.5 0.7"}
    vis = {"r_filmusetweaks": "1", "r_filmtweaklighttint": "1.35 1.02 0.62",
           "r_filmtweakdarktint": "0.7 0.82 1.2", "r_filmtweakdesaturation": "0.1"}
    def sun_rgb(v):
        for l in C.sky_shader("textures/x", "y", sun, vision=v):
            m = re.match(r"\s*q3map_sun\s+(\S+)\s+(\S+)\s+(\S+)", l)
            if m:
                return [float(g) for g in m.groups()]
        return [0.0, 0.0, 0.0]
    cold, warm = sun_rgb(None), sun_rgb(vis)
    ok &= check("CoD's film tint warms the baked sun (%.2f -> %.2f red:blue)"
                % (cold[0] / cold[2], warm[0] / warm[2]),
                [] if warm[0] / warm[2] > cold[0] / cold[2] * 1.5 else ["tint did not reach the sun"])
    # CoD4 keeps the stock maps' vision inside its shared fastfiles, not under
    # raw/, so the published table is the normal source for a stock map and
    # has to parse and reach the sun exactly like a file would.
    import cod4_vision
    st = cod4_vision.stock("mp_strike")
    ok &= check("stock vision table covers the stock maps",
                [] if st and C.film_tint(st) else ["mp_strike missing or unusable"])
    ok &= check("stock grade warms Strike's blue sun (%.2f red:blue)"
                % (sun_rgb(st)[0] / sun_rgb(st)[2]),
                [] if sun_rgb(st)[0] / sun_rgb(st)[2] > cold[0] / cold[2] * 1.3
                else ["the published grade did not move the sun"])
    ok &= check("both spellings of the film keys parse",
                [] if cod4_vision.parse('r_filmEnable "1"\nr_filmLightTint "1.2 0.9 0.9"'
                                        ) == cod4_vision.parse(
                                            'r_filmUseTweaks "1"\n'
                                            'r_filmTweakLightTint "1.2 0.9 0.9"')
                else ["r_film* and r_filmTweak* disagree"])
    ok &= check("a map with no published grade is left alone",
                [] if cod4_vision.stock("mp_not_a_real_map") is None else ["invented a grade"])
    # "strike" and "mp_strike" are different grades, so the name has to come
    # from the art script's VisionSetNaked() rather than from the map name
    ok &= check("the vision name is read from the art script",
                [] if cod4_vision.names_in_script(
                    'main(){ visionSetNaked( "strike", 0 ); }') == ["strike"]
                else ["did not read VisionSetNaked"])
    ok &= check("the two Strike grades are not confused for each other",
                [] if cod4_vision.stock("strike") != cod4_vision.stock("mp_strike")
                else ["same grade for both names"])
    # r_primaryLightTweakDiffuseStrength is lighting, not grading: it scales
    # the sun's intensity rather than its colour (mp_crash sets 1.4)
    def sun_i(v):
        for l in C.sky_shader("textures/x", "y", sun, vision=v):
            m = re.match(r"\s*q3map_sun\s+\S+\s+\S+\s+\S+\s+(\S+)", l)
            if m:
                return float(m.group(1))
        return 0.0
    ok &= check("primaryLightDiffuseStrength scales the sun (%g -> %g)"
                % (sun_i(None), sun_i(cod4_vision.stock("mp_crash"))),
                [] if sun_i(cod4_vision.stock("mp_crash")) > sun_i(None) * 1.3
                else ["not applied"])
    ok &= check("film tint is skipped when CoD has it switched off",
                [] if C.film_tint({"r_filmusetweaks": "0",
                                   "r_filmtweaklighttint": "1.35 1 0.6"}) is None
                else ["applied a disabled grade"])
    # the sky light shares the sun's scale: 5x was what made maps look bland
    ok &= check("sky light and sun share one scale (%d)" % C.SKY_SCALE,
                [] if C.SKY_SCALE == C.SUN_SCALE else ["%d vs %d" % (C.SKY_SCALE, C.SUN_SCALE)])
    # q3map2's own radiosity is unaffordable here (one -bounce pass on
    # mp_strike made 122,201 diffuse lights and never finished), so CoD's
    # radiosity numbers have to reach the sky light instead
    ok &= check("CoD's sun radiosity reaches the sky light",
                [] if cod4_vision.radiosity_scalar(
                    {"ambient": "0.1", "sunradiosity": "0.45"})[0] > 0.5
                else ["radiosity ignored"])
    ok &= check("the shadow floor takes the graded ambient colour",
                [] if cod4_vision.ambient_colour({"_color": "0.5 0.5 0.7"},
                                                 cod4_vision.stock("mp_strike"))[0] > 0.6
                else ["floor stayed blue"])
    bounce = io.open("compile_cod4_map.bat", encoding="latin-1").read()
    ok &= check("q3map2 radiosity is off by default",
                [] if 'COD4TOSOF2_BOUNCE=0"' in bounce else ["-bounce is on by default"])
    # Every log must name the build that wrote it, and the .bat's stamp has to
    # match the module's - that mismatch is how an old exe gives itself away
    import cod4tosof2_run
    # A CoD light has a radius it dies at; q3map2's default inverse-square has
    # no radius and runs to infinity at the source. Feeding CoD's radius in as
    # the light key made a 400-unit light 732 luxels on a wall 64 units away.
    bad = []
    lt, fade = cod4_vision.point_light(400, 1.0)
    peak = lt * cod4_vision.POINT_SCALE * cod4_vision.LINEAR_SCALE
    if abs(peak - cod4_vision.SUN_SCALE) > 0.5:
        bad.append("peak is %g, not one unit of intensity on the sun's scale" % peak)
    if abs(peak / fade - 400) > 1:
        bad.append("dies at %g units, not CoD's 400" % (peak / fade))
    if lt * cod4_vision.POINT_SCALE / (64.0 ** 2) < 100:
        pass          # inverse-square would be this hot; linear is what we want
    lt2, fade2 = cod4_vision.point_light(400, 2.0)
    if abs(lt2 - 2 * lt) > 1e-6 or abs(peak * 2 / fade2 - 400) > 1:
        bad.append("intensity should scale brightness, not reach")
    ok &= check("a light's reach is CoD's radius and its peak is one anchor", bad)
    src = io.open("iwmap2map.py", encoding="utf-8").read()
    ok &= check("light entities get linear falloff with the Lambert term back",
                [] if ('"spawnflags" "1"' in src and '"_anglescale" "1"' in src
                       and '"fade"' in src)
                else ["spawnflags/_anglescale/fade not written"])
    # Fog distance is CoD's own unless someone asks for the cap. mp_strike's
    # fog is opaque at 17,500 units and that is how far you can see on it.
    import iwmap2map
    conv = iwmap2map.Converter.__new__(iwmap2map.Converter)
    conv.scale, conv.fog_max = 1.25, iwmap2map.Converter.FOG_MAX_OPAQUE
    f = conv.make_fog([0, 7000, 0.91, 0.81, 0.62], "x")
    bad = []
    if iwmap2map.Converter.FOG_MAX_OPAQUE != 0:
        bad.append("the fog cap is on by default")
    if f["opaque"] != f["cod_opaque"]:
        bad.append("CoD's %d became %d" % (f["cod_opaque"], f["opaque"]))
    conv.fog_max = 8000
    if conv.make_fog([0, 7000, 0.91, 0.81, 0.62], "x")["opaque"] != 8000:
        bad.append("--fog-max no longer caps")
    ok &= check("fog keeps CoD's distance (%d units on Strike)" % f["opaque"], bad)
    # A map with no fastfile of its own must not take the collect down with
    # it: mp_derail hit os.path.dirname(None) and the whole step reported
    # "could not read map scripts", which hid the real reason.
    src = io.open("cod4_collect_assets.py", encoding="utf-8").read()
    ok &= check("a map with no fastfile does not crash the collect",
                [] if "if ff and not any(n.endswith" in src
                else ["os.path.dirname(ff) is still unguarded"])

    # The exe bundles Pillow, numpy, customtkinter and the Python runtime, and
    # each of those licences asks that its text travels with the binary.
    bad = []
    if not os.path.exists("LICENSE"):
        bad.append("no LICENSE")
    elif "MIT License" not in io.open("LICENSE", encoding="utf-8").read():
        bad.append("LICENSE is not the MIT text")
    bld = io.open("build_exe.bat", encoding="latin-1").read()
    if "collect_licenses.py" not in bld:
        bad.append("build_exe.bat does not collect the third-party licences")
    if 'copy /y "LICENSE" "dist\\LICENSE"' not in bld:
        bad.append("the licences are not copied next to the exe")
    spec = io.open("CoD4toSOF2.spec", encoding="utf-8").read()
    if "THIRD-PARTY-LICENSES.txt" not in spec:
        bad.append("the licences are not bundled inside the exe")
    ok &= check("the licences ship with the exe", bad)
    import collect_licenses
    blocks, _ = collect_licenses.collect()
    ok &= check("every bundled package has a licence text (%d)" % len(blocks),
                [] if len(blocks) >= len(collect_licenses.BUNDLED) + 1
                else ["only %d of %d components" % (len(blocks),
                                                    len(collect_licenses.BUNDLED) + 1)])
    ok &= check("the compile starts from the remembered lightmap scale",
                [] if "bsp_budget lmscale" in bounce else ["the ladder still starts at 1"])
    ok &= check("the .bat and the module agree on the build (%s)" % cod4tosof2_run.VERSION,
                [] if ("echo compile_cod4_map.bat " + cod4tosof2_run.VERSION) in bounce
                else ["the .bat stamp is not %s" % cod4tosof2_run.VERSION])

    # a run's log must hold the LAST line too. Writing it from pump() instead
    # of the worker truncated it: pump drains the queue on the GUI thread,
    # after the worker has already closed the file.
    import tempfile
    out = tempfile.mkdtemp()
    app.vars["output"].set(out)
    app.vars["map"].set("t.map")
    app.worker([("Echo", [sys.executable, "-c", "print('first'); print('LAST')"])],
               os.path.join(out, "t"), True)
    try:
        txt = io.open(os.path.join(out, "t_convert.log"), encoding="utf-8").read()
    except Exception as e:
        txt = "(no log: %s)" % e
    ok &= check("the run log keeps the last line",
                [] if ("first" in txt and "LAST" in txt) else [txt[:70]])
    ok &= check("the run log opens with the build that wrote it",
                [] if txt.startswith("CoD4toSOF2 " + cod4tosof2_run.VERSION)
                else [txt.split("\n")[0][:70]])
    import shutil
    shutil.rmtree(out, ignore_errors=True)

    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
