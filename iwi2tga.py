#!/usr/bin/env python3
"""
iwi2tga.py - CoD4 (IW3) .iwi (version 6) -> .tga

Layout (KisakCOD src/gfx_d3d/r_image.h + r_image_load_obj.cpp):
  GfxImageFileHeader (28 bytes): 'IWi', version u8 (6), format u8, flags u8,
  dimensions s16[3], fileSizeForPicmip s32[4].
  Pixel data follows, mips stored SMALLEST FIRST, so the full-size mip is the
  last block in the file. Cubemaps store the 6 faces per mip level, in D3D
  face order (+X, -X, +Y, -Y, +Z, -Z).

Formats handled: DXT1/3/5 (via a DDS wrapper for Pillow), BITMAP RGBA/RGB/
LA/L/A. Wavelet formats (6-10) are rare in stock content and are reported as
unsupported rather than guessed.

Usage:
  python iwi2tga.py file.iwi [more.iwi ...] -o outdir
  python iwi2tga.py images_dir -o outdir
Cubemaps are written as <name>_px/_nx/_py/_ny/_pz/_nz.tga.
"""
import argparse
import io
import os
import struct
import sys

from PIL import Image

FMT_NAMES = {1: "RGBA", 2: "RGB", 3: "LA", 4: "L", 5: "A", 6: "WAVELET_RGBA",
             7: "WAVELET_RGB", 8: "WAVELET_LA", 9: "WAVELET_L", 10: "WAVELET_A",
             11: "DXT1", 12: "DXT3", 13: "DXT5", 14: "DXN"}
DXT = {11: (b"DXT1", 8), 12: (b"DXT3", 16), 13: (b"DXT5", 16)}
BITMAP_BPP = {1: 4, 2: 3, 3: 2, 4: 1, 5: 1}
CUBE_SUFFIX = ("_px", "_nx", "_py", "_ny", "_pz", "_nz")

FLAG_NOMIPMAPS = 0x2
FLAG_CUBEMAP = 0x4


class IWIError(Exception):
    pass


def read_header(data):
    if len(data) < 28 or data[:3] != b"IWi":
        raise IWIError("not an IWi file")
    ver, fmt, flags = data[3], data[4], data[5]
    w, h, d = struct.unpack_from("<3h", data, 6)
    sizes = struct.unpack_from("<4i", data, 12)
    if ver != 6:
        raise IWIError("IWi version %d (CoD4 is 6)" % ver)
    return {"format": fmt, "flags": flags, "width": w, "height": h, "depth": d, "sizes": sizes}


def _level_size(fmt, w, h):
    if fmt in DXT:
        return DXT[fmt][1] * ((w + 3) // 4) * ((h + 3) // 4)
    if fmt in BITMAP_BPP:
        return BITMAP_BPP[fmt] * w * h
    raise IWIError("unsupported format %d (%s)" % (fmt, FMT_NAMES.get(fmt, "?")))


def _dds(fourcc, w, h, payload):
    hdr = struct.pack("<4sI", b"DDS ", 124)
    hdr += struct.pack("<IIIIII", 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000, h, w, len(payload), 0, 1)
    hdr += b"\0" * 44
    hdr += struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    hdr += struct.pack("<IIIII", 0x1000, 0, 0, 0, 0)
    return hdr + payload


def _decode_level(fmt, w, h, raw):
    if fmt in DXT:
        img = Image.open(io.BytesIO(_dds(DXT[fmt][0], w, h, raw)))
        img.load()
        return img.convert("RGBA")
    if fmt == 1:   # D3DFMT_A8R8G8B8 -> bytes B G R A
        return Image.frombuffer("RGBA", (w, h), raw, "raw", "BGRA", 0, 1)
    if fmt == 2:   # stored 3 bytes/pixel B G R
        return Image.frombuffer("RGB", (w, h), raw, "raw", "BGR", 0, 1).convert("RGBA")
    if fmt == 3:   # A8L8 -> L, A
        return Image.frombuffer("LA", (w, h), raw, "raw", "LA", 0, 1).convert("RGBA")
    if fmt == 4:
        return Image.frombuffer("L", (w, h), raw, "raw", "L", 0, 1).convert("RGBA")
    if fmt == 5:
        a = Image.frombuffer("L", (w, h), raw, "raw", "L", 0, 1)
        img = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        img.putalpha(a)
        return img
    raise IWIError("unsupported format %d" % fmt)


def decode(data):
    """Returns (header, [PIL images]) - one image, or 6 for a cubemap."""
    hd = read_header(data)
    fmt, w, h = hd["format"], hd["width"], hd["height"]
    faces = 6 if hd["flags"] & FLAG_CUBEMAP else 1
    lvl = _level_size(fmt, w, h)
    need = lvl * faces
    if len(data) - 28 < need:
        raise IWIError("truncated: need %d bytes for mip0, have %d" % (need, len(data) - 28))
    top = data[len(data) - need:]        # largest mip is the last block
    imgs = [_decode_level(fmt, w, h, top[i * lvl:(i + 1) * lvl]) for i in range(faces)]
    return hd, imgs


def has_alpha(img):
    lo, _ = img.getchannel("A").getextrema()
    return lo < 255


def convert_file(path, outdir, force_rgb=False):
    data = open(path, "rb").read()
    hd, imgs = decode(data)
    base = os.path.splitext(os.path.basename(path))[0]
    written = []
    for i, img in enumerate(imgs):
        name = base + (CUBE_SUFFIX[i] if len(imgs) == 6 else "")
        out = os.path.join(outdir, name + ".tga")
        if force_rgb or not has_alpha(img):
            img = img.convert("RGB")
        img.save(out, compress=False)
        written.append(out)
    return hd, written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    files = []
    for p in a.inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.lower().endswith(".iwi")]
        else:
            files.append(p)
    ok = bad = 0
    for f in files:
        try:
            hd, out = convert_file(f, a.out)
            ok += 1
        except IWIError as e:
            bad += 1
            print("SKIP %s: %s" % (os.path.basename(f), e))
    print("converted %d, skipped %d -> %s" % (ok, bad, a.out))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
