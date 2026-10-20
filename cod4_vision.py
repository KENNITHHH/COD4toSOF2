"""CoD4 vision files - the colour grading a map runs over the finished frame.

A map's art script calls VisionSetNaked("<name>"), and raw/vision/<name>.vision
holds a filmic grade: a tint mixed into the highlights, another into the
shadows, a desaturation, a contrast curve and a brightness offset.

This matters for the conversion because the grade is NOT in the map's lighting
data. mp_strike's worldspawn sun is "0.5 0.6 1.0" - a blue sun with a blue
ambient ("_color" "0.5 0.5 0.7") - and the warm image CoD shows is that blue
light run through the grade afterwards. SOF2 draws the lightmap straight, so
without the grade a faithful conversion of Strike comes out blue.

Since a lit surface is albedo * light and a tint is a per-channel multiply,
(albedo * light) * tint == albedo * (light * tint): the tint folds into the
light that gets baked, exactly rather than approximately. What cannot fold in
is the part that is not a per-channel multiply of the light - the contrast
curve, the brightness offset, and the share of the desaturation that applies
to the albedo rather than to the light. Those stay in the tone curve knobs
(COD4TOSOF2_GAMMA / _EXPOSURE).

r_primaryLightTweakDiffuseStrength is different again: it scales the sun's
diffuse contribution, so it is real lighting rather than grading and is
applied to the sun's intensity. mp_crash sets it to 1.4.

STOCK holds the stock grades, because CoD4 ships these inside its shared
fastfiles rather than under raw/ - so a stock map almost never has a .vision
file to read. The values are transcribed from the vision files in
https://github.com/louvenarde/iw4scr/tree/master/vision , keeping only the
ones in CoD4's two-point form (LightTint + DarkTint). That repository is IW4
(MW2), which re-graded its remakes of Strike, Derail, Overgrown and Vacant
with a three-point form (LightTint + MediumTint + DarkTint) under the same
file names, so those four are left out here and the three CoD4 ones that are
published come from
https://wiki.zeroy.com/index.php?title=Call_of_Duty_4:_Stock_.vision_files
instead. CoD4's own mp_strike_art.gsc names the vision it wants, which is
read first - "strike" and "mp_strike" are both real and are not the same
grade.
"""
import re

# vision name -> (contrast, brightness, desaturation, lightTint, darkTint
#                 [, primaryLightDiffuseStrength])
STOCK = {
    "ac130":                     (1.9, 0.5, 1, "1 1 1", "1 1 1"),
    "ac130_inverted":            (1.55, 0.13, 1, "1 1 1", "1 1 1"),
    "af_caves_indoors":          (1.2, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_indoors_breachroom": (1.2, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_indoors_overlook": (1.2, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_indoors_skylight": (1.2, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_indoors_steamroom": (1.12348, 0.0834, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_indoors_steamroom_dark": (1.12348, 0.0834, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "af_caves_outdoors":         (1.5, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "aftermath":                 (1.1, 0.05, 0, "1 0.969 0.9", "0.7 0.3 0.2"),
    "airplane":                  (1.3, 0.19, 0.2, "1 1 1", "1 1 1"),
    "ambush":                    (1.3, 0.19, 0.2, "1 1 1", "1 1 1"),
    "armada":                    (1.893, 0.2455, 0.265, "1.179 1.086 1.024", "0.7 0.959 1.265"),
    "armada_ground":             (1.5805, 0.2298, 0.265, "1.179 1.086 1.024", "0.7 1.0215 1.265"),
    "armada_tvs":                (1.5, 0.2298, 0.1, "1.179 1.086 1.024", "0.7 1.0215 1.265"),
    "armada_water":              (1.5, 0.134, 0.265, "1.179 1.086 1.024", "0.7 1.0215 1.265"),
    "bigcity_destroyed":         (1.3, 0.14, 0.2, "1.07 1.05 0.85", "0.7 0.85 1"),
    "black_bw":                  (0, -1, 1, "1 1 1", "1 1 1"),
    "blackout":                  (0.98, 0.057, 0.3, "1.04 1 1.2", "0.909 0.899 1"),
    "blackout_darkness":         (0.98, 0, 0.3, "1.04 1 1.2", "0.909 0.899 1"),
    "blackout_nvg":              (1.758, 0.47, 1, "1.5797 1.9992 2", "0 1.999 0.000226783"),
    "blacktest":                 (0, -1, 0.2, "1 1 1", "1 1 1"),
    "bog":                       (1.447, 0.145, 0.393, "0.802 0.774 0.884", "0.854 0.802 0.854"),
    "bog_a":                     (0.817949, -0.0720581, 0.348496, "1.54514 1.27109 1.20341", "0.797897 0.758245 0.711313"),
    "bog_a_sunrise":             (0.817949, 0.0104163, 0.348496, "1.37604 1.36347 1.32133", "0.853149 0.782718 1.0015"),
    "bog_b":                     (1.74548, 0.187093, 0.45, "1.0209 1.05 1.11", "1.08003 1.08 1.13691"),
    "bridge":                    (1.21845, 0.134626, 0.2, "1.15625 1.07813 1", "0.84375 1 1"),
    "cargoship":                 (1.615, 0.25, 0.6, "1.141 1.275 1.36", "1 1.158 1.169"),
    "cargoship_blast":           (1.45, 0.17, 0.785, "1.99 0.798 0", "1.99 1.32 0"),
    "cargoship_indoor":          (1.4, 0.19, 0.6, "1.0375 1.05 1.17813", "0.7 0.85 1"),
    "cargoship_indoor2":         (1.4, 0.24, 0.6, "1.0375 1.05 1.17813", "0.7 0.85 1"),
    "cheat_bw":                  (1, 0, 1, "1 1 1", "1 1 1"),
    "cheat_bw_contrast":         (3.2, 0.5, 1, "1 1 1", "1 1 1"),
    "cheat_bw_invert":           (1, 0, 1, "1 1 1", "1 1 1"),
    "cheat_bw_invert_contrast":  (3, 1, 1, "1 1 1", "1 1 1"),
    "cheat_chaplinnight":        (1.43801, 0.1443, 0.9525, "1.63834 0.47453 1.48423", "2 2 2"),
    "cheat_contrast":            (3.2, 0.5, 0, "1 1 1", "1 1 1"),
    "cheat_invert":              (1, 0, 0, "1 1 1", "1 1 1"),
    "cheat_invert_contrast":     (3, 1, 0, "1 1 1", "1 1 1"),
    "cliffhanger":               (0.9, 0.054, 0.2, "0.999828 1.08911 1.09274", "0.999973 0.99995 1.22267", 1.5),
    "co_break":                  (1.23, 0.02923, 0.065, "0.94 1.106 1.194", "0.7 1.0215 1.265"),
    "co_hunted2":                (1.26, 0.15, 0.3, "0.94 1 1.2", "0.9 0.89 0.95"),
    "co_overgrown":              (1.15595, -0.0015, 0.35, "1.03 0.9535 0.889", "1 1 1.158"),
    "cobra_down":                (1, 0, 0.48, "1.4 1.07 0.82", "0.7 0.95 0.92"),
    "cobra_sunset1":             (1.2, 0, 0.48, "1.2 0.84 0.97", "0.72 0.95 1.02"),
    "cobra_sunset2":             (1.2, 0, 0.48, "1.1 0.96 1.16", "0.7 0.87 1.25"),
    "cobra_sunset3":             (1.2, 0, 0.48, "0.7 0.85 1", "0.5 0.75 1.08"),
    "cobrapilot":                (1.44153, 0.150485, 0.0794931, "1.08396 1.237 1.39108", "0.563352 0.615843 0.678493"),
    "contingency_thermal_inverted": (1.25, 0.3, 1, "1 1 1", "1 1 1"),
    "coup":                      (1.07497, -0.0195861, 0.363524, "1 1 1", "1 1 1"),
    "coup_hit":                  (2.5, 1, 1, "1 1 1", "1 1 1"),
    "coup_sunblind":             (0.4, 0.15, 0.75, "1 1 1", "1 1 1"),
    "dcemp":                     (1, 0, 0.328125, "1 1 1", "1 1 1"),
    "dcemp_iss":                 (1.27, 0, 0, "1 1 1", "1 1 1"),
    "dcemp_iss_death":           (1.65, 0.025, 0, "1.25 1.25 1.25", "1 1 1"),
    "default_night":             (1.26527, 0.353663, 1, "1.5797 1.9992 2", "0 1.54321 0.000226783"),
    "default_night_mp":          (1.26527, 0.15, 1, "1.5797 1.9992 2", "0 1.54321 0.000226783"),
    "end_game2":                 (1.4, 1, 0.2, "1.1 1.05 0.85", "1 0.85 0.85"),
    "exterior_concept":          (1.5, 0.2, 0.2, "1 1 1", "1 1 1"),
    "firingrange":               (1.0035, -0.00534387, 0.0299174, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "grayscale":                 (1, 0, 1, "1 1 1", "1 1 1"),
    "helicopter_ride":           (1, 0, 0.48, "1.4 1.07 0.82", "0.7 0.95 0.92"),
    "hunted":                    (1.26, 0.15, 0.3, "0.94 1 1.2", "0.9 0.89 0.95"),
    "hunted_crash":              (1.25, 0.02, 0.6, "0.94 1 1.2", "0.91 0.9 0.85"),
    "icbm":                      (1.2, -0.0577349, 0, "0.7 0.85 1", "0.5 0.75 0.97"),
    "icbm_interior":             (1.2, 0.128, 0.2, "0.8 1 1", "0.9 0.95 1.05"),
    "icbm_launch":               (1.85, 0.33, 0, "1.1 1 0.94", "1.17 1.01 0.81"),
    "icbm_sunrise0":             (1.2, 0, 0, "0.7 0.85 1", "0.5 0.75 0.97"),
    "icbm_sunrise1":             (1.2, 0, 0, "0.8 0.95 1.1", "0.85 0.93 1.05"),
    "icbm_sunrise2":             (1.2, 0, 0.2, "0.8 1 1", "0.9 0.95 1.05"),
    "icbm_sunrise3":             (1.2, 0, 0.34, "1 1 1", "1 1 1.05"),
    "icbm_sunrise4":             (1.2, 0, 0.28, "1 1 1", "1 1 1"),
    "introscreen":               (1.11965, 0.014806, 1, "1.13173 1.16271 1.45521", "0.700005 0.973834 0.826321"),
    "jeepride":                  (1.0035, -0.00534387, 0.0299174, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "jeepride_cobra":            (1.05441, -0.00261099, 0.0299174, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "jeepride_flyaway":          (1.0618, 1.18453, 0.0284852, "1.0991 1.10082 1.10134", "0.946567 1.04024 1.11933"),
    "jeepride_tunnel":           (1.37781, 0.118425, 0.0298658, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "jeepride_zak":              (1.264, 0.000346, 0.152183, "0.972077 0.934609 0.839432", "0.946567 1.04024 1.11933"),
    "killhouse":                 (1.02186, 0.0122496, 0.2, "1.1 1.05 0.85", "0.956127 0.854841 0.777573"),
    "launchfacility":            (1.123, 0.0002, 0.168, "1.4 1.27 1.011", "1 1 1.168"),
    "launchfacility_a":          (1.15843, 0.0355033, 0.402416, "1.4 1.27 1.011", "1 1 1.168"),
    "launchfacility_b":          (1.123, 0.0292009, 0.168, "1.4 1.27 1.011", "1 1 1.168"),
    "missilecam":                (3.7, 1, 1, "0 0 0", "1.21215 1.21039 1.31139"),
    "mp_backlot":                (1.471, 0.198, 0.26, "1.051 0.966 0.821", "0.71 0.63 0.56"),
    "mp_bloc":                   (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "mp_bog":                    (0.817949, -0.03, 0.348496, "1.23787 1.22658 1.15435", "0.787442 0.771667 0.736767"),
    "mp_broadcast":              (1.5, 0.2298, 0.1, "1.179 1.086 1.024", "0.7 1.0215 1.265"),
    "mp_carentan":               (1.13, 0.04, 0.14, "0.792 1 1.1", "1.11 1.07 0.76"),
    "mp_cargoship":              (1.615, 0.25, 0.6, "1.141 1.275 1.36", "1 1.158 1.169"),
    "mp_citystreets":            (1.205, 0.123, 0.33, "1.25 1.08 0.71", "0.49 0.47 0.465"),
    "mp_convoy":                 (0.817949, 0.0104163, 0.348496, "1.37604 1.36347 1.32133", "0.853149 0.782718 1.0015"),
    "mp_countdown":              (0.9, -0.055, 0.63, "1.545 1.394 1.315", "1.184 1.492 1.381"),
    "mp_crash":                  (1.4, 0.134, 0.265, "1.179 1.086 1.024", "0.7 1.0215 1.265", 1.4),
    "mp_crash_damage":           (0.66, 0.153, 0.22, "1.179 1.086 1.024", "0.7 1.0215 1.265"),
    "mp_creek":                  (1.15595, 0.134626, 0, "1 1 1", "0.787 0.81 1"),
    "mp_creek_ss":               (1.15595, 0.134626, 0, "1 1 1", "0.787 0.81 1"),
    "mp_crossfire":              (1.55798, 0.265218, 0.45, "1.0209 1.05 1.11", "1.08003 1.08 1.13691"),
    "mp_dusk":                   (1.2, 0, 0, "1 1 1", "1 1 1"),
    "mp_farm":                   (1, 0.047, 0.4225, "0.8 0.8 0.8", "0.986425 1.10315 1.00362"),
    "mp_firingrange":            (1.0035, -0.00534387, 0.0299174, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "mp_hill":                   (1.27, 0, 0.36, "1 1.17 1.25", "1.0958 1.15 0.988"),
    "mp_killhouse":              (0.985, 0.0346, 0.2, "1.1 1.05 0.85", "0.956127 0.854841 0.777573"),
    "mp_oilrig":                 (1.615, 0.25, 0.6, "1.141 1.275 1.36", "1 1.158 1.169"),
    "mp_pipeline":               (1.3, 0.194425, 0.313334, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "mp_shipment":               (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "mp_showdown":               (0.87, 0.032, 0.348496, "1.37604 1.36347 1.32133", "0.853149 0.782718 1.0015"),
    "mp_skidrow":                (1.58, 0.232, 0.579, "1.08 1.069 0.988", "1.17 1.1 0.963"),
    "mp_suburbia":               (1.3, 0.14, 0.2, "1.07 1.05 0.85", "0.7 0.85 1"),
    "mp_trailer":                (1.02186, 0.0122496, 0.2, "1.1 1.05 0.85", "0.956127 0.854841 0.777573"),
    "mp_verdict":                (0.817949, -0.03, 0.348496, "1.23787 1.22658 1.15435", "0.787442 0.771667 0.736767"),
    "mpintro":                   (1.8, 0.05, 1, "1 1 1", "1 1 1"),
    "mpnuke":                    (1.67, 0.0189287, 0.48, "2 1.3053 0.672409", "0.749927 0.909935 1.08943"),
    "mpnuke_aftermath":          (1.8, 0.05, 0.58, "1 0.969 0.9", "0.7 0.3 0.2"),
    "mpoutro":                   (1.8, 0.05, 1, "1 1 1", "1 1 1"),
    "nate_test":                 (1.05441, -0.00261099, 0.0299174, "1.09913 1.0521 1.10145", "0.946567 1.04024 1.11933"),
    "overwatch_nv":              (1.3, 0.25, 1, "2 2 1", "0 1.54321 0.000226783"),
    "school":                    (1.66202, 0.182207, 0.336962, "1.02075 0.938641 0.682454", "0.91845 0.94375 1"),
    "scoutsniper":               (1.25, 0.03915, 0.6, "0.94 1 1.2", "0.91 0.9 0.85"),
    "seaknight_assault":         (0.969167, -0.0760276, 0.154472, "1.23184 1.20033 1.094", "0.943224 0.939271 1.11673"),
    "sepia":                     (1.43801, 0.1443, 0.9525, "1.0074 0.6901 0.3281", "1.0707 1.0679 0.9181"),
    "sniperescape":              (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "sniperescape_glow_off":     (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "sniperescape_outside":      (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
    "strike":                    (1.109, 0.102, 0.4, "1.179 1.086 1.024", "0.7 0.959 1.265"),
    "thermal_mp":                (1, 0, 1, "1 1 1", "1 1 1"),
    "thermal_snowlevel_mp":      (1, 0, 1, "1 1 1", "1 1 1"),
    "trainer_start":             (1.5, 0.2, 0.2, "1 1 1", "1 1 1"),
    "tulsa":                     (1.30362, 0.0886548, 0.319005, "0.931849 0.835072 0.972073", "0.829399 0.871889 0.35258"),
    "village_assault":           (1.246, 0.129, 0.407, "1.1 1.05 0.85", "0.7 0.85 1"),
    "village_defend":            (1.15595, 0.134626, 0.2, "1 1 1", "1 1 1"),
    "wetwork":                   (1.55378, 0.127757, 0.0446647, "1.13486 1.05167 1.03766", "0.7 0.85 1"),
}

# The four names IW4 overwrote with its own remakes. Published CoD4 values;
# mp_derail's are not published anywhere I could find, so a derail conversion
# keeps CoD's raw light and says so in the log.
STOCK.update({
    "mp_strike":    (1.6, 0.187093, 0.368, "1.2 0.942 0.903", "1.08003 1.08 1.13691"),
    "mp_overgrown": (1.4, 0.125, 0.386669, "1.35 1.43 1.55", "0.9 0.82 0.74"),
    "mp_vacant":    (0.87104, 0, 0.352396, "1.10838 1.10717 1.15409", "0.7 0.928125 1"),
})

# The same settings are spelled both ways in the wild - r_filmTweak* in some
# CoD4 files, r_film* in others and in the later IW games. Both normalise to
# the first spelling. r_filmMediumTint is IW4 only and is read so that an IW4
# file can be recognised, not used.
ALIASES = {
    "r_filmusetweaks": "r_filmusetweaks", "r_filmenable": "r_filmusetweaks",
    "r_filmtweakenable": "r_filmusetweaks",
    "r_filmtweaklighttint": "r_filmtweaklighttint", "r_filmlighttint": "r_filmtweaklighttint",
    "r_filmtweakdarktint": "r_filmtweakdarktint", "r_filmdarktint": "r_filmtweakdarktint",
    "r_filmtweakmediumtint": "r_filmtweakmediumtint",
    "r_filmmediumtint": "r_filmtweakmediumtint",
    "r_filmtweakdesaturation": "r_filmtweakdesaturation",
    "r_filmdesaturation": "r_filmtweakdesaturation",
    "r_filmtweakcontrast": "r_filmtweakcontrast", "r_filmcontrast": "r_filmtweakcontrast",
    "r_filmtweakbrightness": "r_filmtweakbrightness",
    "r_filmbrightness": "r_filmtweakbrightness",
    "r_filmtweakinvert": "r_filmtweakinvert", "r_filminvert": "r_filmtweakinvert",
    "r_primarylighttweakdiffusestrength": "r_primarylightdiffuse",
    "r_primarylightdiffusestrength": "r_primarylightdiffuse",
}

# VisionSetNaked( "mp_strike", 0 ) / VisionSetNakedForPlayer( player, "x", 0 )
VISION_CALL = re.compile(r'(?i)\bvisionset\w*\s*\(\s*(?:[^,()"]+,\s*)?"([^"]+)"')


def names_in_script(text):
    """Vision names a .gsc asks for, first use first."""
    out = []
    for m in VISION_CALL.finditer(text or ""):
        n = m.group(1).strip()
        if n and n.lower() not in [o.lower() for o in out]:
            out.append(n)
    return out


def parse(text):
    """{normalised key: value} for the grading settings in a .vision file.
    First occurrence wins: mp_cargoship.vision carries the block twice."""
    out = {}
    for k, v in re.findall(r'^\s*(\w+)\s*"?([^"\n]*?)"?\s*$', text, re.M):
        key = ALIASES.get(k.lower())
        if key and key not in out:
            out[key] = v.strip()
    return out


def stock(name):
    """The stock grade for a vision name, shaped like parse()'s result."""
    row = STOCK.get((name or "").lower())
    if not row:
        return None
    contrast, bright, desat, light, dark = row[:5]
    out = {"r_filmusetweaks": "1", "r_filmtweakcontrast": "%g" % contrast,
           "r_filmtweakbrightness": "%g" % bright,
           "r_filmtweakdesaturation": "%g" % desat,
           "r_filmtweaklighttint": light, "r_filmtweakdarktint": dark}
    if len(row) > 5 and abs(row[5] - 1.0) > 1e-9:
        out["r_primarylightdiffuse"] = "%g" % row[5]
    return out


def vec3(s, default=(1.0, 1.0, 1.0)):
    try:
        p = [float(x) for x in str(s).replace(",", " ").split()[:3]]
    except (TypeError, ValueError):
        return list(default)
    return (p + p[-1:] * 3)[:3] if p else list(default)


def graded(col, tint, desat):
    """CoD's grade applied to one light colour, then renormalised so only the
    hue changes - the intensity is derived separately and must not move."""
    c = [max(0.0, col[k]) * max(0.0, tint[k]) for k in range(3)]
    if desat > 0:
        luma = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        c = [v + (luma - v) * min(1.0, desat) for v in c]
    top = max(c)
    return [v / top for v in c] if top > 0 else list(col)


def film_tint(vision):
    """(lightTint, darkTint, desaturation) from a grade, or None when there is
    no colour change in it.

    CoD mixes darkTint into the shadows and lightTint into the highlights, so
    folded into the baked light the sun carries the highlight tint and the sky
    light carries the shadow tint.
    """
    if not vision:
        return None
    for key in ("r_filmusetweaks", "r_filmtweakenable"):
        if str(vision.get(key, "")).strip().lower() in ("0", "false", "off"):
            return None
    lt = vec3(vision.get("r_filmtweaklighttint"))
    dt = vec3(vision.get("r_filmtweakdarktint"))
    try:
        desat = float(vision.get("r_filmtweakdesaturation") or 0.0)
    except ValueError:
        desat = 0.0
    if lt == [1.0, 1.0, 1.0] and dt == [1.0, 1.0, 1.0] and desat <= 0:
        return None
    return lt, dt, desat


def ambient_colour(sun, vision):
    """The colour a shadow should be: CoD's own ambient colour run through the
    grade's dark tint. q3map2 uses worldspawn "_color" for all three min-light
    floors, and mp_strike's raw ambient is blue (0.5 0.5 0.7), so without this
    the floor fights the light instead of matching it."""
    ac = vec3((sun or {}).get("_color") or "1 1 1")
    tint = film_tint(vision)
    return graded(ac, tint[1], tint[2]) if tint else ac


def radiosity_scalar(sun):
    """CoD's flat ambient plus its sun radiosity, as one hemisphere-light
    scalar. CoD bakes radiosity separately from the ambient term; q3map2 can
    too (-bounce) but on a map the size of mp_strike one bounce turned 76
    lights into 122,201 diffuse lights and the pass does not finish in hours.
    So CoD's own radiosity numbers go into the sky light instead, which costs
    nothing."""
    def num(k, d=0.0):
        try:
            return float((sun or {}).get(k) or d)
        except ValueError:
            return d
    rad = num("sunradiosity") * (num("radiosityscale", 1.0) or 1.0)
    return max(0.0, num("ambient")) + max(0.0, rad), num("ambient"), rad


# ---------------------------------------------------------------- light scale
# One anchor for every light in the map, so a CoD "intensity" of 1 lands at the
# same luxel value whether it came from the sun, the sky or a light entity.
#
# q3map2 normalises q3map_sun's colour and uses the intensity token directly as
# the luxel a sun-facing surface receives, 0-255 - before ColorToBytes applies
# the gamma. So the token that puts a surface at SUN_TARGET_LUXEL after gamma
# is 255*(target/255)**gamma, and CoD's own sunlight multiplier (1.2 on
# mp_derail) divides out of it.
LIGHT_GAMMA = 1.6            # must match COD4TOSOF2_GAMMA in compile_cod4_map.bat
SUN_TARGET_LUXEL = 140.0     # what a sun-facing surface should reach, after gamma
SUN_SCALE = round((SUN_TARGET_LUXEL / 255.0) ** LIGHT_GAMMA * 255.0 / 1.2)
SKY_SCALE = SUN_SCALE

# q3map2's own numbers, read from light.cpp / q3map2.h in NetRadiant-custom:
#   photons = <light key> * pointScale          pointScale  = 7500
#   linear:  add = angle * photons * linearScale - dist * fade
#   square:  add = angle * photons / (dist * dist), dist clamped to >= 16
POINT_SCALE = 7500.0
LINEAR_SCALE = 1.0 / 8000.0


def point_light(radius, intensity, scale=1.0):
    """(light, fade) for a CoD point light, as q3map2 linear-falloff keys.

    CoD's light has a radius it dies at and an intensity; q3map2's default is
    inverse-square, which has no radius at all and goes to infinity at the
    source. Feeding CoD's radius in as the light key - which is what this
    converter did at first - is wrong twice over: a radius of 400 became
    photons of 3,000,000, which is 732 luxels on a wall 64 units away (255 is
    white) and still above the cull threshold 1,732 units out. That is the
    saturated pool of colour with no edge.

    Linear falloff has the shape CoD's does. With spawnflags 1 the light is
    linear, and "_anglescale" "1" puts the Lambert term back, which spawnflags
    1 removes on a Quake 3 game:

        add  = angle * light * POINT_SCALE * LINEAR_SCALE - dist * fade
        peak = 0.9375 * light          (at the light, angle 1)
        zero = peak / fade

    so a peak of SUN_SCALE per unit of CoD intensity, dying at CoD's radius,
    is one light key and one fade key. Lights also stop costing what they used
    to: the Fast flag culls each one at its own radius instead of carrying it
    across the map.
    """
    peak = max(0.0, intensity) * SUN_SCALE
    reach = max(1.0, radius * scale)
    return peak / (POINT_SCALE * LINEAR_SCALE), peak / reach
