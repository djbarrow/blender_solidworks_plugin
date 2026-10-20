# SPDX-License-Identifier: LGPL-2.1-or-later
"""Mesh SolidWorks parts and report each body: triangles, closed or not,
area and volume.   python3 tests/check_parts.py FILE_OR_FOLDER ...

A body reported 'solid' is watertight. Compare volumes with SolidWorks' mass
properties (mm^3) to check a conversion."""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "io_import_solidworks"))
from sw2blender import mesh  # noqa: E402


def measure(b):
    v, t = b.verts, b.tris
    area = vol = 0.0
    for i in range(0, len(t), 3):
        a, c, d = 3 * t[i], 3 * t[i + 1], 3 * t[i + 2]
        p, q, r = v[a:a + 3], v[c:c + 3], v[d:d + 3]
        u = [q[k] - p[k] for k in range(3)]
        w = [r[k] - p[k] for k in range(3)]
        x = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        area += 0.5 * (x[0] ** 2 + x[1] ** 2 + x[2] ** 2) ** 0.5
        vol += (p[0] * (q[1] * r[2] - q[2] * r[1]) - p[1] * (q[0] * r[2] - q[2] * r[0])
                + p[2] * (q[0] * r[1] - q[1] * r[0])) / 6.0
    return area, vol


def main(args):
    files = []
    for a in args:
        if os.path.isdir(a):
            files += sorted(glob.glob(os.path.join(a, "*.[Ss][Ll][Dd][Pp][Rr][Tt]")))
        else:
            files.append(a)
    bad = 0
    for f in files:
        t0 = time.time()
        try:
            bodies, log = mesh.mesh_part(f)
        except Exception as exc:
            print("FAILED %s: %s" % (f, exc))
            bad += 1
            continue
        for b in bodies:
            area, vol = measure(b)
            print("%-32s %-6s tris=%7d area=%12.1f mm2 vol=%14.1f mm3  %.1fs" % (
                b.label[:32], b.kind, len(b.tris) // 3, area, vol, time.time() - t0))
        for w in log.warnings:
            print("    warning: %s" % w)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
