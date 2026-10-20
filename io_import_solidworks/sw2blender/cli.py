# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Command line: convert SolidWorks parts to mesh files without Blender.

    python3 sw2blender/cli.py part.SLDPRT [-o part.obj] [--detail fine]

Output formats: .obj (with colours in a .mtl), .stl, .ply. Pure Python.
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "sw2blender"

from .loader import ConversionError, check  # noqa: E402
from .mesh import mesh_part  # noqa: E402

DETAIL = {"coarse": 0.002, "normal": 0.0005, "fine": 0.0001}
FORMATS = (".obj", ".stl", ".ply")


def write_obj(path, bodies):
    base = os.path.splitext(path)[0]
    mtl = base + ".mtl"
    names = {}
    with open(mtl, "w") as fh:
        for b in bodies:
            for rgb in b.colours:
                key = tuple(round(c, 4) for c in rgb) + (round(b.transparency, 3),)
                if key in names:
                    continue
                name = "SW_%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)
                if b.transparency:
                    name += "_t%02d" % int(round(b.transparency * 100))
                names[key] = name
                fh.write("newmtl %s\nKd %.4f %.4f %.4f\nd %.3f\n\n"
                         % (name, rgb[0], rgb[1], rgb[2], 1.0 - b.transparency))
    with open(path, "w") as fh:
        fh.write("# SolidWorks Import for Blender\nmtllib %s\n" % os.path.basename(mtl))
        base_v = 1
        for b in bodies:
            fh.write("o %s\n" % b.label.replace(" ", "_"))
            v = b.verts
            for i in range(0, len(v), 3):
                fh.write("v %.6f %.6f %.6f\n" % (v[i], v[i + 1], v[i + 2]))
            cur = None
            t = b.tris
            for k in range(len(b.tri_colour)):
                c = b.tri_colour[k]
                if c != cur:
                    rgb = b.colours[c]
                    key = tuple(round(x, 4) for x in rgb) + (round(b.transparency, 3),)
                    fh.write("usemtl %s\n" % names[key])
                    cur = c
                fh.write("f %d %d %d\n" % (t[3 * k] + base_v, t[3 * k + 1] + base_v,
                                           t[3 * k + 2] + base_v))
            base_v += len(v) // 3


def write_stl(path, bodies):
    import struct
    tris = []
    for b in bodies:
        v, t = b.verts, b.tris
        for k in range(0, len(t), 3):
            tris.append([tuple(v[3 * t[k + j]:3 * t[k + j] + 3]) for j in range(3)])
    with open(path, "wb") as fh:
        fh.write(b"SolidWorks Import for Blender".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for p, q, r in tris:
            u = (q[0] - p[0], q[1] - p[1], q[2] - p[2])
            w = (r[0] - p[0], r[1] - p[1], r[2] - p[2])
            n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
            ln = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5 or 1.0
            fh.write(struct.pack("<12fH", n[0] / ln, n[1] / ln, n[2] / ln,
                                 *(p + q + r), 0))


def write_ply(path, bodies):
    nv = sum(len(b.verts) // 3 for b in bodies)
    nf = sum(len(b.tris) // 3 for b in bodies)
    with open(path, "w") as fh:
        fh.write("ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\n"
                 "property float z\nelement face %d\nproperty list uchar int vertex_indices\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
                 % (nv, nf))
        for b in bodies:
            v = b.verts
            for i in range(0, len(v), 3):
                fh.write("%.6f %.6f %.6f\n" % (v[i], v[i + 1], v[i + 2]))
        base = 0
        for b in bodies:
            t = b.tris
            for k in range(len(b.tri_colour)):
                r, g, bl = (int(round(c * 255)) for c in b.colours[b.tri_colour[k]])
                fh.write("3 %d %d %d %d %d %d\n" % (t[3 * k] + base, t[3 * k + 1] + base,
                                                     t[3 * k + 2] + base, r, g, bl))
            base += len(b.verts) // 3


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="sw2blender",
        description="Convert SolidWorks parts (.SLDPRT, 2011 and later) or Parasolid files "
                    "to triangle meshes. Lengths are written in millimetres.")
    p.add_argument("inputs", nargs="+", help=".SLDPRT, .x_t or .x_b files")
    p.add_argument("-o", "--output", help="output file (%s) or folder; default: "
                   "next to the input as .obj" % ", ".join(FORMATS))
    p.add_argument("--format", choices=[f[1:] for f in FORMATS], default="obj",
                   help="format when -o is a folder or not given (default obj)")
    p.add_argument("--detail", choices=sorted(DETAIL), default="normal",
                   help="how closely the triangles follow curved surfaces (default normal)")
    p.add_argument("--keep-duplicates", action="store_true",
                   help="keep bodies lying exactly on top of another (dropped by default)")
    p.add_argument("--check", action="store_true",
                   help="only check that the inputs can be read; prints JSON")
    p.add_argument("-v", "--verbose", action="store_true", help="print warnings")
    args = p.parse_args(argv)
    if args.check:
        res = {f: check(f) for f in args.inputs}
        print(json.dumps(res))
        return 0 if all(r["ok"] for r in res.values()) else 1
    failures = 0
    for inp in args.inputs:
        out = args.output
        name = os.path.splitext(os.path.basename(inp))[0] + "." + args.format
        if not out:
            out = os.path.join(os.path.dirname(os.path.abspath(inp)), name)
        elif os.path.isdir(out) or len(args.inputs) > 1 or out.endswith(os.sep):
            os.makedirs(out, exist_ok=True)
            out = os.path.join(out, name)
        ext = os.path.splitext(out)[1].lower()
        if ext not in FORMATS:
            print("FAILED  %s: unsupported output type %s" % (inp, ext))
            failures += 1
            continue
        try:
            bodies, log = mesh_part(inp, detail=DETAIL[args.detail],
                                    keep_duplicates=args.keep_duplicates, verbose=args.verbose)
        except (ConversionError, OSError) as exc:
            print("FAILED  %s: %s" % (inp, exc))
            failures += 1
            continue
        {".obj": write_obj, ".stl": write_stl, ".ply": write_ply}[ext](out, bodies)
        ntri = sum(len(b.tris) // 3 for b in bodies)
        st = getattr(log, "stats", {})
        extra = ""
        if st.get("failed_faces"):
            extra = "  [%d of %d faces could not be meshed]" % (st["failed_faces"], st["faces"])
        print("OK      %s -> %s  (%d bod%s, %d triangles)%s" % (
            inp, out, len(bodies), "y" if len(bodies) == 1 else "ies", ntri, extra))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
