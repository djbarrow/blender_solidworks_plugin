# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Constrained Delaunay triangulation in the plane. Pure Python.

triangulate(points, segments) returns every triangle of the convex hull of
`points`, with each segment present as a triangle edge where possible. Blender
2.81 and later have a much faster version of this in C
(mathutils.geometry.delaunay_2d_cdt); fast_triangulate() uses it when it can.

Algorithm: Bowyer-Watson insertion with a walking point location, then each
segment is forced in by removing the triangles it crosses and filling the two
sides again (Anglada 1997).
"""

import random


def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _incircle(ax, ay, bx, by, cx, cy, dx, dy):
    """> 0 if d lies inside the circle through a, b, c (counter-clockwise)."""
    adx, ady = ax - dx, ay - dy
    bdx, bdy = bx - dx, by - dy
    cdx, cdy = cx - dx, cy - dy
    ad = adx * adx + ady * ady
    bd = bdx * bdx + bdy * bdy
    cd = cdx * cdx + cdy * cdy
    return (adx * (bdy * cd - bd * cdy) - ady * (bdx * cd - bd * cdx) + ad * (bdx * cdy - bdy * cdx))


class Triangulation:
    """Triangles are kept as directed edges: edge[(a, b)] = c means a, b, c is
    a counter-clockwise triangle. A triangle appears three times, once per edge."""

    def __init__(self, points):
        xs = [p[0] for p in points] or [0.0]
        ys = [p[1] for p in points] or [0.0]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        self.scale = s = max(x1 - x0, y1 - y0, 1e-300)
        self.ox, self.oy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        # work in a unit-sized frame so the tolerances below mean something
        self.x = [(p[0] - self.ox) / s for p in points]
        self.y = [(p[1] - self.oy) / s for p in points]
        self.n = len(points)
        big = 100.0
        self.x += [-big, big, 0.0]
        self.y += [-big, -big, big]
        n = self.n
        self.edge = {}
        self.vtri = {}                    # vertex -> some vertex it shares a triangle edge with
        self._add(n, n + 1, n + 2)
        self.sup = (n, n + 1, n + 2)
        self.alias = list(range(n))       # duplicate points map to the first copy
        self.extra = []                   # points added where segments cross
        self.eps = 1e-12

    # ------------------------------------------------------------ basics
    def _add(self, a, b, c):
        e = self.edge
        e[(a, b)] = c
        e[(b, c)] = a
        e[(c, a)] = b
        v = self.vtri
        v[a] = b
        v[b] = c
        v[c] = a

    def _remove(self, a, b, c):
        e = self.edge
        e.pop((a, b), None)
        e.pop((b, c), None)
        e.pop((c, a), None)

    def _o(self, a, b, c):
        x, y = self.x, self.y
        return _orient(x[a], y[a], x[b], y[b], x[c], y[c])

    # ------------------------------------------------------------ insertion
    def _locate(self, px, py, start):
        """A triangle (a, b, c) containing the point, by walking from edge `start`."""
        x, y, e = self.x, self.y, self.edge
        a, b = start
        c = e[(a, b)]
        rnd = random.random
        for _ in range(4 * int(len(e) ** 0.5) + 2000):
            k = int(rnd() * 3)
            tri = ((a, b, c), (b, c, a), (c, a, b))
            moved = False
            for j in range(3):
                u, v, w = tri[(k + j) % 3]
                if _orient(x[u], y[u], x[v], y[v], px, py) < 0.0:
                    nxt = e.get((v, u))
                    if nxt is None:
                        return (u, v, w)
                    a, b, c = v, u, nxt
                    moved = True
                    break
            if not moved:
                return (a, b, c)
        # the walk went round in circles (nearly flat triangles): search them all
        best, best_v = None, -float("inf")
        for (u, v), w in e.items():
            m = min(_orient(x[u], y[u], x[v], y[v], px, py),
                    _orient(x[v], y[v], x[w], y[w], px, py),
                    _orient(x[w], y[w], x[u], y[u], px, py))
            if m > best_v:
                best, best_v = (u, v, w), m
                if m >= 0.0:
                    break
        return best

    def insert(self, i, start):
        """Insert point i. Returns an edge of a new triangle (for the next walk)."""
        x, y, e = self.x, self.y, self.edge
        px, py = x[i], y[i]
        a, b, c = self._locate(px, py, start)
        for q in (a, b, c):
            if q < self.n and abs(x[q] - px) <= 1e-11 and abs(y[q] - py) <= 1e-11:
                self.alias[i] = self.alias[q]
                return (a, b)
        # cavity: triangles whose circumcircle holds the point
        key = {self._key(a, b, c)}
        stack = [(a, b, c)]
        while stack:
            t = stack.pop()
            for u, v in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                w = e.get((v, u))
                if w is None:
                    continue
                k = self._key(v, u, w)
                if k in key:
                    continue
                if (_incircle(x[v], y[v], x[u], y[u], x[w], y[w], px, py) > 0.0
                        or _orient(x[u], y[u], x[v], y[v], px, py) <= 1e-15):
                    key.add(k)
                    stack.append((v, u, w))
        boundary = []
        for t in key:
            for u, v in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                w = e.get((v, u))
                if w is None or self._key(v, u, w) not in key:
                    boundary.append((u, v))
        for t in key:
            self._remove(*t)
        for u, v in boundary:
            self._add(u, v, i)
        return boundary[0] if boundary else (a, b)

    @staticmethod
    def _key(a, b, c):
        if a < b and a < c:
            return (a, b, c)
        if b < c:
            return (b, c, a)
        return (c, a, b)

    # ------------------------------------------------------------ constraints
    def _fan(self, a):
        """Counter-clockwise triangles (a, u, v) around vertex a."""
        e = self.edge
        out = []
        start = self.vtri.get(a)
        if start is None or (a, start) not in e:
            # vtri can go stale after re-triangulation: search
            start = None
            for (p, q) in e:
                if p == a:
                    start = q
                    break
            if start is None:
                return out
        u = start
        seen = set()
        # rotate clockwise to the first triangle of an open fan
        while True:
            w = e.get((u, a))                 # triangle (u, a, w) lies clockwise of (a, u, .)
            if w is None or w == start or w in seen:
                break
            seen.add(w)
            u = w
        first = u
        seen = set()
        while u is not None and u not in seen:
            seen.add(u)
            v = e.get((a, u))
            if v is None:
                break
            out.append((a, u, v))
            u = v
            if u == first:
                break
        return out

    def force(self, a, b, depth=0):
        """Make segment a-b an edge. Returns False if it crosses another
        forced segment (or something degenerate stops it)."""
        if a == b or depth > 50:
            return a == b
        e = self.edge
        if (a, b) in e or (b, a) in e:
            self.fixed.add((min(a, b), max(a, b)))
            return True
        o = self._o
        # first triangle around a that the segment enters
        start = None
        for (_, u, v) in self._fan(a):
            ou, ov = o(a, b, u), o(a, b, v)
            if abs(ou) <= self.eps and self._between(a, u, b):
                ok = self.force(a, u, depth + 1)
                return self.force(u, b, depth + 1) and ok
            if abs(ov) <= self.eps and self._between(a, v, b):
                ok = self.force(a, v, depth + 1)
                return self.force(v, b, depth + 1) and ok
            if ou < 0.0 < ov:
                start = (u, v)
                break
        if start is None:
            return False
        crossed = []
        upper, lower = [], []                  # left / right of a->b
        u, v = start                           # u right, v left
        while True:
            if (min(u, v), max(u, v)) in self.fixed:
                # crosses another segment: split both where they meet
                p = self._split_at_crossing(a, b, u, v)
                if p is None:
                    return False
                ok = self.force(a, p, depth + 1)
                return self.force(p, b, depth + 1) and ok
            crossed.append((u, v))
            if not lower or lower[-1] != u:
                lower.append(u)
            if not upper or upper[-1] != v:
                upper.append(v)
            w = e.get((v, u))
            if w is None:
                return False
            if w == b:
                break
            ow = o(a, b, w)
            if abs(ow) <= self.eps:
                # w lies on the segment: stop here, continue from w
                self._fill(a, w, crossed, upper, lower, w)
                return self.force(w, b, depth + 1)
            if ow < 0.0:
                u = w
            else:
                v = w
        self._fill(a, b, crossed, upper, lower, b)
        return True

    def _split_at_crossing(self, a, b, u, v):
        """Add the point where segment a-b crosses forced edge u-v, splitting
        that edge and its two triangles. Returns the new point's index."""
        x, y = self.x, self.y
        d1x, d1y = x[b] - x[a], y[b] - y[a]
        d2x, d2y = x[v] - x[u], y[v] - y[u]
        den = d1x * d2y - d1y * d2x
        if abs(den) < 1e-300:
            return None
        t = ((x[u] - x[a]) * d1y - (y[u] - y[a]) * d1x) / den
        if not (0.0 < t < 1.0):
            return None
        px, py = x[u] + d2x * t, y[u] + d2y * t
        e = self.edge
        w1, w2 = e.get((u, v)), e.get((v, u))
        if w1 is None or w2 is None:
            return None
        p = len(x)
        x.append(px)
        y.append(py)
        self.alias.append(p)
        self.extra.append(p)
        self._remove(u, v, w1)
        self._remove(v, u, w2)
        self._add(u, p, w1)
        self._add(p, v, w1)
        self._add(v, p, w2)
        self._add(p, u, w2)
        self.fixed.discard((min(u, v), max(u, v)))
        self.fixed.add((min(u, p), max(u, p)))
        self.fixed.add((min(p, v), max(p, v)))
        return p

    def _between(self, a, m, b):
        x, y = self.x, self.y
        dx, dy = x[b] - x[a], y[b] - y[a]
        t = (x[m] - x[a]) * dx + (y[m] - y[a]) * dy
        return 0.0 < t < dx * dx + dy * dy

    def _fill(self, a, b, crossed, upper, lower, end):
        e = self.edge
        # remove every triangle the segment crosses (each crossed edge's two sides)
        dead = set()
        for u, v in crossed:
            for p, q in ((u, v), (v, u)):
                w = e.get((p, q))
                if w is not None:
                    dead.add(self._key(p, q, w))
        for t in dead:
            self._remove(*t)
        self._pseudo(a, b, upper)                     # left side: a, b, c counter-clockwise
        self._pseudo(b, a, list(reversed(lower)))     # right side, seen from b
        self.fixed.add((min(a, b), max(a, b)))

    def _pseudo(self, a, b, chain):
        """Triangulate the polygon a -> b -> chain[-1] ... chain[0] -> a where
        every chain vertex is left of a->b."""
        if not chain:
            return
        x, y = self.x, self.y
        c = 0
        for i in range(1, len(chain)):
            p, q = chain[c], chain[i]
            if _incircle(x[a], y[a], x[b], y[b], x[p], y[p], x[q], y[q]) > 0.0:
                c = i
        cv = chain[c]
        self._pseudo(a, cv, chain[:c])
        self._pseudo(cv, b, chain[c + 1:])
        self._add(a, b, cv)

    # ------------------------------------------------------------ output
    def triangles(self):
        sup = set(self.sup)
        seen = set()
        out = []
        for (a, b), c in self.edge.items():
            if a in sup or b in sup or c in sup:
                continue
            k = self._key(a, b, c)
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
        return out


def _spatial_order(tri, seed):
    """Insertion order: rounds of growing size (biased random), each round
    swept row by row in a snake, so each point is found near the last one."""
    n = tri.n
    rng = random.Random(seed)
    idx = list(range(n))
    rng.shuffle(idx)
    out = []
    start = 0
    size = 64
    x, y = tri.x, tri.y
    while start < n:
        chunk = idx[start:start + size]
        start += size
        size *= 4
        k = max(1, int(len(chunk) ** 0.5 / 2))

        def key(i):
            row = int((y[i] + 0.5) * k)
            col = (x[i] + 0.5)
            return (row, col if row % 2 == 0 else -col)
        chunk.sort(key=key)
        out.extend(chunk)
    return out


def triangulate(points, segments, seed=1):
    """Constrained Delaunay triangulation.

    Returns (triangles, fixed, alias, failed, extra): counter-clockwise index
    triples over the convex hull, the set of (i, j) i<j segment edges that are
    present, a map from each point to the point it duplicates (itself if
    unique), how many segments could not be inserted, and the points added
    where segments cross (point len(points) + k is extra[k])."""
    tri = Triangulation(points)
    tri.fixed = set()
    order = _spatial_order(tri, seed)
    random.seed(seed)
    last = (tri.n, tri.n + 1)
    for i in order:
        last = tri.insert(i, last)
        if last not in tri.edge:
            last = next(iter(tri.edge))
    alias = tri.alias
    for i in range(len(alias)):          # resolve chains of duplicates
        j = alias[i]
        while alias[j] != j:
            j = alias[j]
        alias[i] = j
    failed = 0
    for a, b in segments:
        a, b = alias[a], alias[b]
        if a != b and not tri.force(a, b):
            failed += 1
    # renumber the added points to follow the input points
    n = len(points)
    extra = []
    tris = tri.triangles()
    if tri.extra:
        ren = {}
        for k, p in enumerate(tri.extra):
            ren[p] = n + k
            extra.append((tri.x[p] * tri.scale + tri.ox, tri.y[p] * tri.scale + tri.oy))

        def r(i):
            return ren.get(i, i)
        tris = [(r(a), r(b), r(c)) for a, b, c in tris]
        tri.fixed = {(min(r(a), r(b)), max(r(a), r(b))) for a, b in tri.fixed}
        alias = alias[:n] + [n + k for k in range(len(tri.extra))]
    return tris, tri.fixed, alias, failed, extra


_FAST = None


def have_fast():
    """True if Blender's C triangulation (2.81+) is available."""
    global _FAST
    if _FAST is None:
        try:
            from mathutils.geometry import delaunay_2d_cdt  # noqa: F401
            _FAST = True
        except ImportError:
            _FAST = False
    return _FAST


def fast_triangulate(points, segments):
    """Same result as triangulate(), using Blender's C implementation when it
    is available (Blender 2.81+). Points the C code adds where segments cross
    are returned too: (triangles, fixed, alias, failed, extra_points), where
    extra point k has index len(points) + k."""
    if not have_fast():
        return triangulate(points, segments)
    from mathutils.geometry import delaunay_2d_cdt  # noqa: F811
    from mathutils import Vector
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ox, oy = 0.5 * (min(xs) + max(xs)), 0.5 * (min(ys) + max(ys))
    s = max(max(xs) - min(xs), max(ys) - min(ys), 1e-300)
    verts = [Vector(((p[0] - ox) / s, (p[1] - oy) / s)) for p in points]
    res = delaunay_2d_cdt(verts, [tuple(sg) for sg in segments], [], 0, 1e-11, True)
    vout, eout, fout, orig_v, orig_e = res[0], res[1], res[2], res[3], res[4]
    n = len(points)
    remap = []
    extra = []
    alias = list(range(n))
    for k, ov in enumerate(orig_v):
        if ov:
            first = min(ov)
            for j in ov:
                alias[j] = first
            remap.append(first)
        else:
            extra.append((vout[k][0] * s + ox, vout[k][1] * s + oy))
            remap.append(n + len(extra) - 1)
    tris = []
    for f in fout:
        f = [remap[i] for i in f]
        for k in range(1, len(f) - 1):        # output faces are triangles already
            tris.append((f[0], f[k], f[k + 1]))
    fixed = set()
    for (i, j), oe in zip(eout, orig_e):
        if oe:
            a, b = remap[i], remap[j]
            fixed.add((min(a, b), max(a, b)))
    return tris, fixed, alias, 0, extra
