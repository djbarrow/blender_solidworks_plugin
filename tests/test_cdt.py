# SPDX-License-Identifier: LGPL-2.1-or-later
"""Tests of the pure-Python triangulation.   python3 -m unittest discover tests"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "io_import_solidworks"))
from sw2blender import cdt  # noqa: E402


def area(pts, tris):
    return sum(cdt._orient(*pts[a], *pts[b], *pts[c]) * 0.5 for a, b, c in tris)


def check(test, pts, segs):
    tris, fixed, alias, failed, extra = cdt.triangulate(pts, segs)
    allp = list(pts) + list(extra)
    for a, b, c in tris:
        test.assertGreater(cdt._orient(*allp[a], *allp[b], *allp[c]), 0.0)
    return tris, fixed, alias, failed, allp


class TriangulationTest(unittest.TestCase):
    def test_random_points(self):
        rnd = random.Random(3)
        pts = [(rnd.random(), rnd.random()) for _ in range(500)] + [(0, 0), (1, 0), (0, 1), (1, 1)]
        tris, _, _, failed, allp = check(self, pts, [])
        self.assertEqual(failed, 0)
        self.assertAlmostEqual(area(allp, tris), 1.0, delta=1e-3)

    def test_disk_with_hole(self):
        pts, segs = [], []

        def ring(cx, cy, r, n):
            base = len(pts)
            for k in range(n):
                pts.append((cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)))
                segs.append((base + k, base + (k + 1) % n))
        ring(0, 0, 1, 120)
        ring(0.3, 0, 0.2, 40)
        tris, fixed, alias, failed, allp = check(self, pts, segs)
        self.assertEqual(failed, 0)
        for a, b in segs:
            self.assertIn((min(a, b), max(a, b)), fixed)

    def test_crossing_segments_are_split(self):
        pts = [(i / 10.0, j / 10.0) for i in range(11) for j in range(11)]
        n = len(pts)
        pts += [(0.013, 0.0071), (0.987, 0.9933), (0.0123, 0.9811), (0.991, 0.017)]
        tris, fixed, alias, failed, allp = check(self, pts, [(n, n + 1), (n + 2, n + 3)])
        self.assertEqual(failed, 0)
        self.assertEqual(len(allp), len(pts) + 1)
        self.assertAlmostEqual(area(allp, tris), 1.0, places=9)

    def test_collinear_and_duplicate_points(self):
        pts = [(i * 0.1, 0.0) for i in range(11)] + [(i * 0.1, 1.0) for i in range(11)]
        pts += [(0.5, 0.0), (0.5, 0.5)]
        tris, fixed, alias, failed, allp = check(self, pts, [(0, 10), (11, 21), (0, 11), (10, 21)])
        self.assertEqual(failed, 0)
        self.assertEqual(alias[22], alias[5])          # duplicate of (0.5, 0)
        self.assertAlmostEqual(area(allp, tris), 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
