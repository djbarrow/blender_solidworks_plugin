# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Triangle meshes straight from Parasolid XT bodies. Pure Python.

For each body:

1. Every edge is sampled once into a polyline (shared by the two faces that
   meet there, so the mesh has no cracks).
2. For each face, the boundary loops are mapped into the surface's (u, v)
   parameters, interior points are added where the surface curves, and the
   points are triangulated with the loops as constraints (cdt.py).
3. Triangles inside the face are kept: by crossing parity when the face sits
   in one window of its surface, or by growing from the material side of the
   boundary when the face wraps around (cylinder bands, sphere caps, torus
   pieces, cones up to the apex).
4. Faces are given consistent orientations and solids are turned outward.

Lengths are metres inside; output vertices are millimetres.
"""

import math

from . import cdt
from . import surfaces as S
from . import xtgeom as G

TWO_PI = 2.0 * math.pi
MAX_GRID = 100000         # interior points per face
MAX_GRID_SLOW = 6000      # ... with the pure-Python triangulation (Blender 2.79, tests)


class MeshError(RuntimeError):
    pass


class Log:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.warnings = []

    def warn(self, msg):
        self.warnings.append(msg)
        if self.verbose:
            print("sw2blender: %s" % msg)

    def info(self, msg):
        if self.verbose:
            print("sw2blender: %s" % msg)


class BodyMesh:
    def __init__(self):
        self.verts = []           # [x, y, z] metres
        self.tris = []            # (a, b, c, face_index)
        self.face_colour = {}     # face index -> rgb or None
        self.weld = set()         # vertices that may sit on another one (seams, poles)
        self.extra_boundary = set()   # vertices added on edges (where seams cross them)
        self.splits = {}          # (a, b) edge segment -> [(fraction from a, vertex)]


def _wrap(d, period):
    return d - period * math.floor(d / period + 0.5)


class Mesher:
    def __init__(self, transmit, detail=0.0005, log=None):
        self.t = transmit
        self.detail = detail
        self.max_angle = min(0.4, max(0.08, 8.0 * math.sqrt(detail)))
        self.log = log or Log()
        self._surf = {}
        self._curve = {}
        self.stats = {"faces": 0, "failed_faces": 0, "approx_faces": 0}

    # ============================================================ topology
    def node(self, idx):
        return self.t[idx]

    def bodies(self):
        """(BODY node or None, [faces]) for every body that owns faces."""
        out = {}
        for f in self.t.of_type(14):
            shell = self.node(f["shell"]) or self.node(f.get("front_shell"))
            body = self.node(shell["body"]) if shell else None
            if body is None and shell is not None:
                region = self.node(shell["region"])
                body = self.node(region["body"]) if region else None
            out.setdefault(body.index if body else 0, []).append(f)
        return [(self.node(b) if b else None, faces) for b, faces in sorted(out.items())]

    def point_of(self, vertex_idx):
        vx = self.node(vertex_idx)
        if vx is None:
            return None
        pt = self.node(vx["point"])
        return tuple(pt["pvec"]) if pt else None

    def loop_fins(self, loop):
        fins = []
        start = loop["fin"]
        f = start
        guard = 0
        while f:
            fins.append(self.node(f))
            f = self.node(f)["forward"]
            guard += 1
            if f == start or guard > 100000:
                break
        return fins

    def face_loops(self, face):
        loops = []
        li = face["loop"]
        guard = 0
        while li and guard < 100000:
            loop = self.node(li)
            loops.append(loop)
            li = loop["next"]
            guard += 1
        return loops

    def edge_vertices(self, edge):
        """(start vertex, end vertex) indices of an edge, None for a ring."""
        fin = self.node(edge["fin"])
        if fin is None:
            return None, None
        other = self.node(fin["other"])
        if fin["sense"] == "+":
            return (other["vertex"] if other else None), fin["vertex"]
        return fin["vertex"], (other["vertex"] if other else None)

    def edge_curve_node(self, edge):
        c = self.node(edge["curve"])
        if c is not None:
            return c
        fin = self.node(edge["fin"])
        seen = set()
        while fin is not None and fin.index not in seen:
            seen.add(fin.index)
            c = self.node(fin["curve"])
            if c is not None:
                return c
            fin = self.node(fin["other"])
        return None

    # ============================================================ geometry
    def surface(self, s):
        """Surface object for a surface node, or None if it cannot be evaluated."""
        if s is None:
            return None
        if s.index in self._surf:
            return self._surf[s.index]
        try:
            res = self._make_surface(s)
        except Exception as exc:
            self.log.info("surface %s %d: %s" % (s.name, s.index, exc))
            res = None
        if res is not None:
            res.sense_minus = s["sense"] == "-"
            res.node = s
        self._surf[s.index] = res
        return res

    def _make_surface(self, s):
        ty = s.type
        if ty == 50:
            return S.Plane(s)
        if ty == 51:
            return S.Cylinder(s)
        if ty == 52:
            return S.Cone(s)
        if ty == 53:
            return S.Sphere(s)
        if ty == 54:
            return S.Torus(s)
        if ty == 124:
            return S.BSurface(self.t, s)
        if ty == 67:
            return S.Swept(self.t, s, self._closed_range_curve(self.node(s["section"])))
        if ty == 68:
            return S.Spun(self.t, s, self._closed_range_curve(self.node(s["profile"])))
        if ty == 60:
            base = self.surface(self.node(s["surface"]))
            if base is None:
                return None
            return S.Offset(s, base)
        if ty == 56:
            return self._blend(s)
        return None

    def _closed_range_curve(self, c):
        """A curve with a finite parameter range (for sweeps and spins)."""
        cv = self.curve(c)
        if cv is None:
            raise S.SurfaceError("cannot evaluate %s" % c.name)
        if isinstance(cv, S.Trimmed):
            lo, hi = sorted((cv.t0, cv.t1))
            cv.t0, cv.t1 = lo, hi
            return cv
        if cv.t0 is None:
            raise S.SurfaceError("unbounded profile curve")
        return cv

    def curve(self, c):
        if c is None:
            return None
        if c.index in self._curve:
            return self._curve[c.index]
        try:
            res = self._make_curve(c)
        except Exception as exc:
            self.log.info("curve %s %d: %s" % (c.name, c.index, exc))
            res = None
        self._curve[c.index] = res
        return res

    def _make_curve(self, c):
        ty = c.type
        if ty == 30:
            return S.Line(c)
        if ty == 31:
            return S.Circle(c)
        if ty == 32:
            return S.Circle(c, ellipse=True)
        if ty == 134:
            return S.BCurve(self.t, c)
        if ty == 137:
            surf = self.surface(self.node(c["surface"]))
            if surf is None:
                return None
            if isinstance(surf, S.Blend):
                # its (u, v) are Parasolid's blend parameters, not ours
                return S.OnSurface(surf)
            return S.SPCurve(self.t, c, surf)
        if ty == 133:
            basis = self.curve(self.node(c["basis_curve"]))
            if basis is None:
                return None
            if isinstance(basis, (S.Polyline, S.OnSurface)):
                return basis
            return S.Trimmed(c, basis)
        if ty == 38:
            return self._intersection(c)
        return None

    # ------------------------------------------------------------ intersections
    def _intersection(self, c):
        chart = self.node(c["chart"])
        pts = [tuple(h) for h in chart["hvec"] if h]
        start, end = self.node(c["start"]), self.node(c["end"])
        if start is not None and start["type"] == "T" and start["hvec"]:
            pts.insert(0, tuple(start["hvec"][0]))
        if end is not None and end["type"] == "T" and end["hvec"]:
            pts.append(tuple(end["hvec"][0]))
        dedup = [pts[0]]
        for p in pts[1:]:
            if G.dist(p, dedup[-1]) > 1e-12:
                dedup.append(p)
        closed = len(dedup) > 2 and G.dist(dedup[0], dedup[-1]) < 1e-9
        if closed:
            dedup = dedup[:-1]
        if start is not None and start["type"] == "H":
            closed = True
        return S.Polyline(dedup, closed, self._snapper(c), chart.get("chordal_error"))

    def _snapper(self, c):
        """Function moving a point onto intersection curve `c` (or None)."""
        nodes = [self.node(i) for i in c["surface"]]
        for k, s in enumerate(nodes):
            if s is not None and s.type == 59:        # BLEND_BOUND: edge of a blend
                return self._blend_bound_snapper(s, nodes[1 - k] if len(nodes) == 2 else None)
        surfs = [self.surface(s) for s in nodes]
        if len(surfs) != 2 or surfs[0] is None or surfs[1] is None:
            real = [x for x in surfs if x is not None]
            if len(real) == 1:
                one = real[0]

                def snap_one(p, tdir):
                    u, v = one.inverse(p)
                    return one.eval(u, v)
                return snap_one
            return None
        g1, g2 = surfs

        def snap(p, tdir):
            x = p
            h1 = h2 = None
            for _ in range(10):
                h1 = g1.inverse(x, h1)
                h2 = g2.inverse(x, h2)
                p1, p2 = g1.eval(*h1), g2.eval(*h2)
                n1, n2 = g1.normal(*h1), g2.normal(*h2)
                if n1 is None or n2 is None:
                    return x
                t = G.cross(n1, n2)
                if G.norm(t) < 1e-9:
                    return G.mul(G.add(p1, p2), 0.5)
                rhs = (G.dot(n1, p1), G.dot(n2, p2), G.dot(t, x))
                det = G.dot(n1, G.cross(n2, t))
                y = G.mul(G.add(G.add(G.mul(G.cross(n2, t), rhs[0]), G.mul(G.cross(t, n1), rhs[1])),
                                G.mul(G.cross(n1, n2), rhs[2])), 1.0 / det)
                step = G.dist(x, y)
                x = y
                if step < 1e-11:
                    break
            if G.dist(x, p) > 0.5 * G.norm(tdir) + 1e-6:
                return p                               # wandered off: keep the chart point
            return x
        return snap

    def _blend_bound_snapper(self, bound, other):
        """Exact edge of a rolling-ball blend on one of its supports: the
        ball's centre lies on the spine, one radius off the support."""
        blend = self.node(bound["blend"])
        if blend is None:
            return None
        if other is None or other.type == 59:
            other = self.node(blend["surface"][1 - int(bound["boundary"])])
        support = self.surface(other)
        spine = self.curve(self.node(blend["spine"]))
        r = blend["range"]
        radius = abs(r[0] or r[1] or 0.0)
        if support is None or spine is None or radius <= 0:
            return None
        if isinstance(spine, S.Polyline):
            chart = spine.pts

            def on_spine(q, tdir):
                return spine.snap(q, tdir) if spine.snap else q

            def spine_dist(q):
                return min(G.dist(q, x) for x in chart)
        else:
            def on_spine(q, tdir):
                return spine.eval(spine.param(q))

            def spine_dist(q):
                return G.dist(q, on_spine(q, None))

        def snap(p, tdir):
            h = support.inverse(p)
            n = support.normal(*h)
            if n is None:
                return p
            cands = [G.add(p, G.mul(n, radius)), G.add(p, G.mul(n, -radius))]
            c = min(cands, key=spine_dist)
            c = on_spine(c, tdir)
            h2 = support.inverse(c, h)
            return support.eval(*h2)
        return snap

    # ------------------------------------------------------------ blends
    def _blend(self, s, depth=0):
        """Blend surface for a BLENDED_EDGE node (rolling ball)."""
        r = s["range"]
        radius = abs(r[0] or r[1] or 0.0)
        if radius <= 0:
            raise S.SurfaceError("blend with zero radius (cliff edge)")
        if depth > 4:
            raise S.SurfaceError("blend chain too deep")
        supports = []
        for si in s["surface"]:
            sn = self.node(si)
            if sn is not None and sn.type == 56 and not (sn["range"][0] or sn["range"][1]):
                # cliff edge: the ball rests on the edge curve (that blend's spine)
                supports.append(self._curve_support(self.node(sn["spine"])))
                continue
            sup = self.surface(sn)
            if sup is None:
                raise S.SurfaceError("blend support %s cannot be evaluated" % (sn.name if sn else "?"))
            supports.append(sup)
        spine = self.node(s["spine"])
        pts, closed = self._spine_points(spine, radius)
        c0, d0, d1 = [], [], []
        h0 = h1 = None
        for c in pts:
            h0 = supports[0].inverse(c, h0)
            h1 = supports[1].inverse(c, h1)
            q0, q1 = supports[0].eval(*h0), supports[1].eval(*h1)
            try:
                a, b = G.unit(G.sub(q0, c)), G.unit(G.sub(q1, c))
            except G.GeomError:
                continue
            c0.append(c)
            d0.append(a)
            d1.append(b)
        if len(c0) < 2:
            raise S.SurfaceError("blend spine too short")
        return S.Blend(c0, d0, d1, radius, closed)

    def _curve_support(self, cnode):
        cv = self.curve(cnode)
        if cv is None:
            raise S.SurfaceError("cannot evaluate cliff edge %s" % (cnode.name if cnode else "?"))
        if isinstance(cv, S.Polyline):
            pts = self._refine_curve(cv, max(self.tol * 0.5, 1e-7))
            return S.PolylineSupport(pts, cv.closed)
        return S.CurveSupport(cv)

    def _spine_points(self, spine, radius):
        cv = self.curve(spine)
        if cv is None:
            raise S.SurfaceError("cannot evaluate blend spine %s" % spine.name)
        tol = max(self.tol * 0.5, 1e-7)
        if isinstance(cv, S.Polyline):
            pts = list(self._refine_curve(cv, tol))
            closed = cv.closed
        else:
            if cv.t0 is None:
                raise S.SurfaceError("unbounded blend spine")
            lo, hi = cv.t0, cv.t1
            if isinstance(cv, S.Circle):
                lo, hi = 0.0, TWO_PI
            closed = cv.period is not None and abs((hi - lo) - cv.period) < 1e-9
            pts = self._sample_param(cv, lo, hi, tol, closed=closed)
            if closed:
                pts = pts[:-1]
        if not closed and len(pts) >= 2:
            # reach a little past both ends so the surface covers its face
            a0, a1, b0, b1 = pts[0], pts[1], pts[-1], pts[-2]
            ext = radius
            pts = ([G.add(a0, G.mul(G.unit(G.sub(a0, a1)), ext))] + pts +
                   [G.add(b0, G.mul(G.unit(G.sub(b0, b1)), ext))])
        return pts, closed

    # ============================================================ curve sampling
    def _sample_param(self, cv, t0, t1, tol, closed=False):
        """Points from t0 to t1 (inclusive) within `tol` of the curve."""
        if isinstance(cv, S.Line) or (isinstance(cv, S.Trimmed) and isinstance(cv.b, S.Line)):
            return [cv.eval(t0), cv.eval(t1)]
        if isinstance(cv, S.Circle) or (isinstance(cv, S.Trimmed) and isinstance(cv.b, S.Circle)):
            circ = cv if isinstance(cv, S.Circle) else cv.b
            r = max(circ.radius, 1e-12)
            step = self.max_angle
            if tol < r:
                step = min(step, 2.0 * math.acos(1.0 - tol / r))
            n = max(1, int(math.ceil(abs(t1 - t0) / step - 1e-9)))
            if closed:
                n = max(n, 3)
            return [cv.eval(t0 + (t1 - t0) * k / n) for k in range(n + 1)]
        n0 = 8 if closed else 4
        ts = [t0 + (t1 - t0) * k / n0 for k in range(n0 + 1)]
        pts = [cv.eval(t) for t in ts]
        out = [pts[0]]
        cos_max = math.cos(self.max_angle)

        def rec(ta, pa, tb, pb, depth):
            tm = 0.5 * (ta + tb)
            pm = cv.eval(tm)
            chord = G.sub(pb, pa)
            l2 = G.dot(chord, chord)
            if l2 > 0:
                f = G.dot(G.sub(pm, pa), chord) / l2
                dev = G.dist(pm, G.add(pa, G.mul(chord, f)))
            else:
                dev = G.dist(pm, pa)
            bend = False
            d1, d2 = G.sub(pm, pa), G.sub(pb, pm)
            n1, n2 = G.norm(d1), G.norm(d2)
            if n1 > 0 and n2 > 0 and G.dot(d1, d2) / (n1 * n2) < cos_max:
                bend = True
            if depth < 14 and (dev > tol or bend):
                rec(ta, pa, tm, pm, depth + 1)
                rec(tm, pm, tb, pb, depth + 1)
            else:
                out.append(pb)
        for k in range(n0):
            rec(ts[k], pts[k], ts[k + 1], pts[k + 1], 0)
        return out

    def _refine_curve(self, cv, tol):
        """Points of a chart curve, with points added until it follows the curve."""
        key = round(tol, 12)
        if key not in cv.refined:
            cv.refined[key] = self._refine(list(cv.pts), cv.closed, cv.snap, tol, cv.chord_err)
        return cv.refined[key]

    def _refine(self, pts, closed, snap, tol, chord_err=None):
        """Add points between chart points until the polyline follows the curve.
        A chart's chords stray at most `chord_err` from the curve, so a snap
        that moves a point further than that went wrong and is ignored."""
        if snap is None or len(pts) < 2:
            return pts
        if chord_err is not None and chord_err <= tol:
            return pts                    # already close enough
        out = [pts[0]]
        n = len(pts)
        segs = n if closed else n - 1

        def rec(a, b, depth):
            m = G.mul(G.add(a, b), 0.5)
            try:
                q = snap(m, G.sub(b, a))
            except Exception:
                q = m
            d = G.dist(q, m)
            limit = 0.5 * G.dist(a, b) + tol
            if chord_err is not None:
                limit = min(limit, 1.5 * chord_err + 0.1 * tol)
            if d > limit:
                q, d = m, 0.0                 # the snap went somewhere else: ignore it
            if depth < 6 and d > tol:
                rec(a, q, depth + 1)
                rec(q, b, depth + 1)
            else:
                out.append(b)
        for i in range(segs):
            rec(pts[i], pts[(i + 1) % n], 0)
        if closed:
            out.pop()                 # the first point again
        return out

    @staticmethod
    def _locate(pts, closed, p):
        best = (float("inf"), 0.0)
        n = len(pts)
        segs = n if closed else n - 1
        for i in range(segs):
            a, b = pts[i], pts[(i + 1) % n]
            ab = G.sub(b, a)
            l2 = G.dot(ab, ab)
            s = 0.0 if l2 == 0 else max(0.0, min(1.0, G.dot(G.sub(p, a), ab) / l2))
            d = G.dist(p, G.add(a, G.mul(ab, s)))
            if d < best[0]:
                best = (d, i + s)
        return best[1]

    def _polyline_piece(self, cv, sense, p0, p1, ring):
        """Points of a chart curve from p0 to p1 following the curve sense."""
        pts = self._refine_curve(cv, self.tol)
        n = len(pts)
        if ring:
            return list(reversed(pts)) if sense == "-" else pts
        s0 = self._locate(pts, cv.closed, p0)
        s1 = self._locate(pts, cv.closed, p1)
        same = G.dist(p0, p1) < 1e-9
        if sense == "-":
            a, b = s1, s0
        else:
            a, b = s0, s1
        if cv.closed:
            if b <= a + 1e-9 or same:
                b += n
        elif b < a:
            a, b = b, a
            sense = "-" if sense == "+" else "+"
        inner = []
        k = int(math.floor(a)) + 1
        while k < b - 1e-6:
            inner.append(pts[k % n])
            k += 1
        seq = inner
        if sense == "-":
            seq = list(reversed(seq))
        return [p0] + seq + [p1]

    # ============================================================ edges
    def vertex_gid(self, vidx):
        g = self.vgid.get(vidx)
        if g is None:
            g = len(self.mesh.verts)
            self.mesh.verts.append(self.point_of(vidx))
            self.vgid[vidx] = g
        return g

    def edge_gids(self, edge_idx):
        """Vertex ids along an edge from its start to its end vertex (a ring
        gives its points once round, without repeating the first)."""
        if edge_idx in self.egid:
            return self.egid[edge_idx]
        edge = self.node(edge_idx)
        v0, v1 = self.edge_vertices(edge)
        ring = not v0 or not v1
        p0 = self.point_of(v0) if v0 else None
        p1 = self.point_of(v1) if v1 else None
        cnode = self.edge_curve_node(edge)
        pts = None
        try:
            pts = self._edge_points(edge, cnode, p0, p1, ring)
        except Exception as exc:
            if ring:
                raise MeshError("edge %d (%s): %s" % (edge_idx, cnode.name if cnode else "?", exc))
            self.log.info("edge %d: %s; straight line used" % (edge_idx, exc))
        if pts is None:
            if ring:
                raise MeshError("ring edge %d has no usable curve" % edge_idx)
            pts = [p0, p1]
        verts = self.mesh.verts
        if ring:
            ids = []
            for p in pts:
                ids.append(len(verts))
                verts.append(p)
        else:
            ids = [self.vertex_gid(v0)]
            for p in pts[1:-1]:
                ids.append(len(verts))
                verts.append(p)
            ids.append(self.vertex_gid(v1))
        self.egid[edge_idx] = ids
        return ids

    def _edge_points(self, edge, cnode, p0, p1, ring):
        if cnode is None:
            return None
        cv = self.curve(cnode)
        if cv is None:
            return None
        sense = cnode["sense"]
        tol = self.tol
        if isinstance(cv, S.Polyline):
            if cnode.type == 133:
                # trimmed chart: cut by the trim points, then run p0 -> p1
                basis = self.node(cnode["basis_curve"])
                q0, q1 = tuple(cnode["point_1"]), tuple(cnode["point_2"])
                pts = self._polyline_piece(cv, basis["sense"], q0, q1, False)
                if not ring and G.dist(pts[0], p0) > G.dist(pts[0], p1):
                    pts.reverse()
            else:
                pts = self._polyline_piece(cv, sense, p0, p1, ring)
            if not ring:
                pts[0], pts[-1] = p0, p1
            if ring and len(pts) > 2 and G.dist(pts[0], pts[-1]) < 1e-12:
                pts.pop()
            return pts
        if isinstance(cv, S.OnSurface):
            if ring:
                raise MeshError("ring edge on a rebuilt blend")
            return self._on_surface_points(cv.s, p0, p1)
        if isinstance(cv, S.Line):
            if ring:
                raise MeshError("ring edge on a line")
            return self._line_points(edge, p0, p1)
        if ring:
            if isinstance(cv, S.Trimmed):
                lo, hi = cv.t0, cv.t1
            else:
                lo, hi = cv.t0, (cv.t0 + cv.period if cv.period else cv.t1)
            pts = self._sample_param(cv, lo, hi, tol, closed=True)[:-1]
            if sense == "-":
                pts.reverse()
            return pts
        if isinstance(cv, S.Trimmed):
            # the trim parameters belong to the trim points
            ta = cv.t0 if G.dist(cv.p1, p0) <= G.dist(cv.p2, p0) else cv.t1
            tb = cv.t1 if ta == cv.t0 else cv.t0
            if isinstance(cv.b, S.Line):
                return self._line_points(edge, p0, p1)
            pts = self._sample_param(cv, ta, tb, tol)
        else:
            ta = cv.param(p0)
            tb = cv.param(p1, ta)
            per = cv.period
            same = G.dist(p0, p1) < 1e-9
            if per:
                if sense == "+":
                    while tb <= ta + 1e-12:
                        tb += per
                    if same:
                        tb = ta + per
                else:
                    while tb >= ta - 1e-12:
                        tb -= per
                    if same:
                        tb = ta - per
            pts = self._sample_param(cv, ta, tb, tol)
        pts[0], pts[-1] = p0, p1
        return pts

    def _on_surface_points(self, surf, p0, p1):
        """A curve on a surface known only by its ends: straight in the
        surface's (u, v) between them (a blend's cross-section is then exactly
        the arc of the rolling ball)."""
        a = surf.inverse(p0)
        b = surf.inverse(p1, a)
        if surf.pu:
            b = (a[0] + _wrap(b[0] - a[0], surf.pu), b[1])
        f = lambda k: surf.eval(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        pts = self._sample_param(_FuncCurve(f), 0.0, 1.0, self.tol)
        pts[0], pts[-1] = p0, p1
        return pts

    def _line_points(self, edge, p0, p1):
        """Straight edges are split up when they border a curved face, so
        that face's triangles can follow its surface."""
        length = G.dist(p0, p1)
        if length <= self.line_seg or not self._edge_on_curved(edge):
            return [p0, p1]
        n = int(math.ceil(length / self.line_seg))
        return [G.add(p0, G.mul(G.sub(p1, p0), k / float(n))) for k in range(n + 1)]

    def _edge_on_curved(self, edge):
        fin = self.node(edge["fin"])
        seen = set()
        while fin is not None and fin.index not in seen:
            seen.add(fin.index)
            loop = self.node(fin["loop"])
            face = self.node(loop["face"]) if loop else None
            s = self.node(face["surface"]) if face else None
            if s is not None and s.type != 50:
                return True
            fin = self.node(fin["other"])
        return False

    # ============================================================ faces
    def face_loops_gids(self, face):
        loops = []
        for loop in self.face_loops(face):
            pts = []
            for fin in self.loop_fins(loop):
                if not fin.get("edge"):
                    continue
                ids = self.edge_gids(fin["edge"])
                seq = ids if fin["sense"] == "+" else list(reversed(ids))
                for g in seq:
                    if not pts or pts[-1] != g:
                        pts.append(g)
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts.pop()
            if len(pts) >= 3 or (len(pts) == 2 and False):
                loops.append(pts)
        return loops

    def mesh_face(self, face):
        snode = self.node(face["surface"])
        loops = self.face_loops_gids(face)
        surf = self.surface(snode)
        if surf is None:
            raise MeshError("unsupported surface %s" % (snode.name if snode else "none"))
        out_sign = -1 if (face["sense"] == "-") != (snode["sense"] == "-") else 1
        return FaceMesher(self, surf, loops, out_sign).run()

    def flat_fill(self, face):
        """Fallback: fill the face's boundary as if it were flat."""
        loops = self.face_loops_gids(face)
        if not loops:
            raise MeshError("face has no boundary")
        verts = self.mesh.verts
        # Newell normal of the longest loop
        big = max(loops, key=len)
        n = [0.0, 0.0, 0.0]
        for i in range(len(big)):
            a, b = verts[big[i]], verts[big[(i + 1) % len(big)]]
            n[0] += (a[1] - b[1]) * (a[2] + b[2])
            n[1] += (a[2] - b[2]) * (a[0] + b[0])
            n[2] += (a[0] - b[0]) * (a[1] + b[1])
        try:
            nz = G.unit(n)
        except G.GeomError:
            raise MeshError("degenerate boundary")
        x = G.perpendicular(nz)
        y = G.cross(nz, x)
        pts, segs, gids = [], [], []
        for lp in loops:
            base = len(pts)
            for k, g in enumerate(lp):
                p = verts[g]
                pts.append((G.dot(p, x), G.dot(p, y)))
                gids.append(g)
                segs.append((base + k, base + (k + 1) % len(lp)))
        tris, fixed, alias, failed, extra = cdt.fast_triangulate(pts, segs)
        inside = _parity_region(tris, fixed)
        out = []
        for k in inside:
            a, b, c = tris[k]
            if a >= len(gids) or b >= len(gids) or c >= len(gids):
                continue
            out.append((gids[a], gids[b], gids[c]))
        return out

    # ============================================================ bodies
    def body_tolerance(self, faces):
        pts = []
        for f in faces:
            for loop in self.face_loops(f):
                for fin in self.loop_fins(loop):
                    p = self.point_of(fin["vertex"]) if fin.get("vertex") else None
                    if p is not None:
                        pts.append(p)
        if len(pts) < 2:
            for p in self.t.of_type(29):
                pts.append(tuple(p["pvec"]))
        if len(pts) < 2:
            return 1.0
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        diag = G.dist(lo, hi)
        if diag <= 0:
            # e.g. a sphere: one vertex or none; use the surfaces' sizes
            sizes = [self.surface(self.node(f["surface"])) for f in faces]
            diag = max([2.0 * s.size for s in sizes if s is not None] or [1.0])
        return diag

    def mesh_body(self, body, faces):
        self.mesh = BodyMesh()
        self.vgid = {}
        self.egid = {}
        diag = self.body_tolerance(faces)
        self.diag = diag
        self.tol = max(diag * self.detail, 1e-7)
        self.line_seg = max(diag / 25.0, 40.0 * self.tol)
        colours, _ = self.colour_attributes()
        body_col = colours.get(body.index) if body is not None else None
        face_tris = []
        for k, f in enumerate(faces):
            self.stats["faces"] += 1
            tris = None
            try:
                tris = self.mesh_face(f)
            except Exception as exc:
                self.log.info("face %s: %s; filling it flat" % (f["node_id"], exc))
                try:
                    tris = self.flat_fill(f)
                    self.stats["approx_faces"] += 1
                except Exception as exc2:
                    self.stats["failed_faces"] += 1
                    self.log.warn("face %s skipped: %s" % (f["node_id"], exc2 or exc))
                    continue
            face_tris.append((f, tris))
            self.mesh.face_colour[k] = colours.get(f.index, body_col)
            for a, b, c in tris:
                self.mesh.tris.append((a, b, c, k))
        if not self.mesh.tris:
            raise MeshError("no faces could be meshed")
        b = set(self.vgid.values())
        for ids in self.egid.values():
            b.update(ids)
        b.update(self.mesh.extra_boundary)
        self.mesh.boundary = b
        return self.mesh

    # ============================================================ colours
    def colour_attributes(self):
        """{owner index: (r, g, b)} and {owner index: translucency} from the
        colour attributes SolidWorks writes (SDL/TYSA_COLOUR[_2],
        SDL/TYSA_TRANSLUCENCY[_2]); values are 0..1."""
        if hasattr(self, "_colours"):
            return self._colours
        names = {}
        for d in self.t.of_type(80):
            ident = self.node(d["identifier"])
            names[d.index] = ident["string"] if ident else ""
        colours, trans = {}, {}
        for a in self.t.of_type(81):
            name = names.get(a["definition"], "")
            if not name.startswith(("SDL/TYSA_COLOUR", "SDL/TYSA_TRANSLUCENCY")):
                continue
            vals = []
            for fi in a["fields"] or []:
                v = self.node(fi)
                if v is not None and v.type == 83 and v["values"]:
                    vals.extend(v["values"])
            if name.startswith("SDL/TYSA_COLOUR") and len(vals) >= 3:
                colours[a["owner"]] = tuple(max(0.0, min(1.0, float(x))) for x in vals[:3])
            elif name.startswith("SDL/TYSA_TRANSLUCENCY") and vals:
                trans[a["owner"]] = max(0.0, min(1.0, float(vals[0])))
        self._colours = (colours, trans)
        return self._colours


# ================================================================ region finding
def _adjacency(tris):
    adj = {}
    for k, (a, b, c) in enumerate(tris):
        for u, v in ((a, b), (b, c), (c, a)):
            adj.setdefault((min(u, v), max(u, v)), []).append(k)
    return adj


def _parity_region(tris, fixed, adj=None):
    """Triangles an odd number of constraint edges in from the hull."""
    adj = adj or _adjacency(tris)
    parity = {}
    queue = []
    for e, ks in adj.items():
        if len(ks) == 1:
            k = ks[0]
            if k not in parity:
                parity[k] = 1 if e in fixed else 0
                queue.append(k)
    while queue:
        k = queue.pop()
        a, b, c = tris[k]
        for u, v in ((a, b), (b, c), (c, a)):
            e = (min(u, v), max(u, v))
            for j in adj[e]:
                if j != k and j not in parity:
                    parity[j] = parity[k] ^ (1 if e in fixed else 0)
                    queue.append(j)
    return [k for k, p in parity.items() if p]


def _inside_region(tris, fixed, adj, xy, segments):
    """Triangles inside the loops: the triangles fall into pieces separated
    by the boundary edges, and each piece is in or out as a whole, decided by
    an even-odd test of one point. A boundary that crosses itself a little
    then only affects the piece it touches."""
    comp = {}
    pieces = []
    for k0 in range(len(tris)):
        if k0 in comp:
            continue
        cid = len(pieces)
        comp[k0] = cid
        members = [k0]
        queue = [k0]
        while queue:
            k = queue.pop()
            a, b, c = tris[k]
            for u, v in ((a, b), (b, c), (c, a)):
                e = (min(u, v), max(u, v))
                if e in fixed:
                    continue
                for j in adj[e]:
                    if j not in comp:
                        comp[j] = cid
                        members.append(j)
                        queue.append(j)
        pieces.append(members)
    if len(pieces) > 500:
        return _parity_region(tris, fixed, adj)
    out = []
    for members in pieces:
        # test the centre of the piece's largest triangle
        best, best_a = members[0], -1.0
        for k in members[:2000]:
            a, b, c = tris[k]
            ar = abs(_tri_area2(xy[a], xy[b], xy[c]))
            if ar > best_a:
                best, best_a = k, ar
        a, b, c = tris[best]
        px = (xy[a][0] + xy[b][0] + xy[c][0]) / 3.0
        py = (xy[a][1] + xy[b][1] + xy[c][1]) / 3.0
        inside = False
        for (x0, y0), (x1, y1) in segments:
            if (y0 > py) != (y1 > py):
                if px < x0 + (py - y0) * (x1 - x0) / (y1 - y0):
                    inside = not inside
        if inside:
            out.extend(members)
    return out


def _tri_area2(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _flood(tris, fixed, adj, seeds):
    region = set(seeds)
    queue = list(seeds)
    while queue:
        k = queue.pop()
        a, b, c = tris[k]
        for u, v in ((a, b), (b, c), (c, a)):
            e = (min(u, v), max(u, v))
            if e in fixed:
                continue
            for j in adj[e]:
                if j not in region:
                    region.add(j)
                    queue.append(j)
    return region


# ================================================================ one face
class FaceMesher:
    """Triangulate one face in its surface's parameter space."""

    def __init__(self, m, surf, loops, out_sign):
        self.m = m
        self.S = surf
        self.loops = loops
        self.out_sign = out_sign
        self.verts = m.mesh.verts
        self.tol = m.tol

    # ------------------------------------------------------------ uv of loops
    def _pole_axis(self, u, v):
        """'u' if u is meaningless at (u, v) (a pole), 'v' if v is, else None."""
        S = self.S
        if not (S.pole_vlo or S.pole_vhi or S.pole_ulo or S.pole_uhi):
            return None
        _, Pu, Pv = S.d1(u, v)
        lu, lv = G.norm(Pu), G.norm(Pv)
        small = 1e-6 * max(S.size, 1e-9)
        if lu < small and lv >= small:
            return "u"
        if lv < small and lu >= small:
            return "v"
        return None

    def _material_left(self):
        return self.out_sign > 0 if self.S.trusted else None

    def loop_uv(self, loop):
        """[(u, v, gid)] for a loop with periodic parameters unwrapped, and its
        winding numbers (wu, wv)."""
        S = self.S
        raw = []
        hint = None
        for g in loop:
            u, v = S.inverse(self.verts[g], hint)
            hint = (u, v)
            pole = self._pole_axis(u, v)
            if pole:
                self.m.mesh.weld.add(g)
            raw.append([u, v, g, pole])
        # start at a regular point
        k0 = next((i for i, r in enumerate(raw) if r[3] is None), None)
        if k0 is None:
            raise MeshError("loop lies entirely on a pole")
        raw = raw[k0:] + raw[:k0]
        out = []
        n = len(raw)
        i = 0
        last = None                                   # last regular (u, v)
        mat_left = self._material_left()
        while i < n:
            u, v, g, pole = raw[i]
            if pole is None:
                if last is not None:
                    if S.pu:
                        u = last[0] + _wrap(u - last[0], S.pu)
                    if S.pv:
                        v = last[1] + _wrap(v - last[1], S.pv)
                out.append((u, v, g))
                last = (u, v)
                i += 1
                continue
            # a run of pole points: cross along the pole line to the next regular point
            j = i
            while j < n and raw[j][3] is not None:
                j += 1
            nxt = raw[j % n] if j < n else None
            if pole == "u":
                pv = self._pole_value(raw[i][1], "v")
                nu = nxt[0] if nxt is not None else out[0][0]
                a = last[0]
                b = a + _wrap(nu - a, S.pu) if S.pu else nu
                b = self._pole_direction(a, b, S.pu, pv == (S.vbounds[0] if S.vbounds else None),
                                         mat_left)
                out.append((a, pv, raw[i][2]))
                if abs(b - a) > 1e-12:
                    out.append((b, pv, raw[i][2]))
                last = (b, pv)
            else:
                pu = self._pole_value(raw[i][0], "u")
                nv = nxt[1] if nxt is not None else out[0][1]
                a = last[1]
                b = a + _wrap(nv - a, S.pv) if S.pv else nv
                b = self._pole_direction(a, b, S.pv, pu != (S.ubounds[0] if S.ubounds else None),
                                         mat_left)
                out.append((pu, a, raw[i][2]))
                if abs(b - a) > 1e-12:
                    out.append((pu, b, raw[i][2]))
                last = (pu, b)
            i = j
        # winding: total change once round, closing back to the start
        wu = wv = 0
        if S.pu:
            tot = out[-1][0] + _wrap(out[0][0] - out[-1][0], S.pu) - out[0][0]
            wu = int(round(tot / S.pu))
        if S.pv:
            tot = out[-1][1] + _wrap(out[0][1] - out[-1][1], S.pv) - out[0][1]
            wv = int(round(tot / S.pv))
        return out, wu, wv

    def _pole_value(self, x, which):
        S = self.S
        b = S.vbounds if which == "v" else S.ubounds
        if b is None:
            return x
        return b[0] if abs(x - b[0]) <= abs(x - b[1]) else b[1]

    @staticmethod
    def _pole_direction(a, b, period, low_side, mat_left):
        """Pick which way round the pole line to go (only matters when periodic):
        the face's material must stay on the inside of the domain."""
        if not period or mat_left is None or abs(b - a) < 1e-12:
            return b
        # along the low edge of the domain the inside is on the left when
        # moving in +direction; along the high edge when moving in -direction
        want_up = (low_side == mat_left)
        if want_up and b < a:
            b += period
        elif not want_up and b > a:
            b -= period
        return b

    # ------------------------------------------------------------ main
    def run(self):
        S = self.S
        uvloops = []
        winding = False
        for lp in self.loops:
            pts, wu, wv = self.loop_uv(lp)
            uvloops.append((pts, wu, wv))
            if wu or wv:
                winding = True
        rect = (not self.loops) or winding or S.closed
        if not rect:
            ok = self._place_free(uvloops)
            if not ok:
                rect = True
        if rect:
            return self._rect(uvloops)
        return self._free(uvloops)

    # ------------------------------------------------------------ free mode
    def _place_free(self, uvloops):
        """Shift each loop by whole periods into one window. False if the loops
        cover every value of a periodic parameter (then rect mode is needed)."""
        S = self.S
        self.window = [None, None]
        for axis, period in ((0, S.pu), (1, S.pv)):
            if not period:
                continue
            vals = sorted((p[axis] % period) for pts, _, _ in uvloops for p in pts)
            if not vals:
                continue
            gaps = [(vals[i + 1] - vals[i], vals[i]) for i in range(len(vals) - 1)]
            gaps.append((vals[0] + period - vals[-1], vals[-1]))
            gap, at = max(gaps)
            if gap < 1e-9 * period:
                return False
            start = at + 0.5 * gap
            self.window[axis] = start
            for idx, (pts, wu, wv) in enumerate(uvloops):
                lo = min(p[axis] for p in pts)
                k = math.floor((lo - start) / period)
                if k:
                    pts[:] = [(p[0] - k * period, p[1], p[2]) if axis == 0 else
                              (p[0], p[1] - k * period, p[2]) for p in pts]
                if max(p[axis] for p in pts) > start + period + 1e-12 * period:
                    return False
        return True

    def _free(self, uvloops):
        pts, segs, chains = [], [], []
        for lp, _, _ in uvloops:
            base = len(pts)
            for u, v, g in lp:
                pts.append([u, v, g])
            n = len(lp)
            chain = [(base + k, base + (k + 1) % n) for k in range(n)]
            segs.extend(chain)
            chains.append(chain)
        us = [p[0] for p in pts]
        vs = [p[1] for p in pts]
        box = (min(us), max(us), min(vs), max(vs))
        return self._triangulate(pts, segs, chains, box, rect=None)

    # ------------------------------------------------------------ rect mode
    def _rect(self, uvloops):
        S = self.S
        allu = [p[0] for pts, _, _ in uvloops for p in pts]
        allv = [p[1] for pts, _, _ in uvloops for p in pts]
        sides = {}
        # u range
        if S.pu:
            us0 = self._seam(allu, S.pu, S.ubounds)
            ulo, uhi = us0, us0 + S.pu
            sides["ulo"] = sides["uhi"] = "seam"
        else:
            ulo, uhi, sides["ulo"], sides["uhi"] = self._extent(allu, S.ubounds, S.pole_ulo,
                                                                 S.pole_uhi)
        if S.pv:
            vs0 = self._seam(allv, S.pv, S.vbounds)
            vlo, vhi = vs0, vs0 + S.pv
            sides["vlo"] = sides["vhi"] = "seam"
        else:
            vlo, vhi, sides["vlo"], sides["vhi"] = self._extent(allv, S.vbounds, S.pole_vlo,
                                                                 S.pole_vhi)
        if not (uhi > ulo and vhi > vlo):
            raise MeshError("empty parameter range")
        # cut the loops along the seams into chains inside the rectangle
        pts = []
        segs = []
        chains = []
        side_pts = {"ulo": [], "uhi": [], "vlo": [], "vhi": []}
        for lp, wu, wv in uvloops:
            for chain in self._cut(lp, ulo, uhi, vlo, vhi, S.pu, S.pv):
                base = len(pts)
                for u, v, g in chain:
                    pts.append([u, v, g])
                closed = chain is not None and len(chain) > 2 and getattr(chain, "closed", False)
                n = len(chain)
                cs = [(base + k, base + k + 1) for k in range(n - 1)]
                if closed:
                    cs.append((base + n - 1, base))
                segs.extend(cs)
                chains.append(cs)
        eps_u = 1e-9 * (uhi - ulo)
        eps_v = 1e-9 * (vhi - vlo)
        for i, (u, v, g) in enumerate(pts):
            if abs(u - ulo) <= eps_u:
                side_pts["ulo"].append(i)
            elif abs(u - uhi) <= eps_u:
                side_pts["uhi"].append(i)
            if abs(v - vlo) <= eps_v:
                side_pts["vlo"].append(i)
            elif abs(v - vhi) <= eps_v:
                side_pts["vhi"].append(i)
        box = (ulo, uhi, vlo, vhi)
        return self._triangulate(pts, segs, chains, box, rect=(sides, side_pts))

    def _seam(self, vals, period, bounds):
        if not vals:
            return bounds[0] if bounds else 0.0
        m = sorted(x % period for x in vals)
        gaps = [(m[i + 1] - m[i], m[i]) for i in range(len(m) - 1)]
        gaps.append((m[0] + period - m[-1], m[-1]))
        gap, at = max(gaps)
        return at + 0.5 * gap

    def _extent(self, vals, bounds, pole_lo, pole_hi):
        """(lo, hi, kind_lo, kind_hi) for a non-periodic parameter."""
        lo, hi = (min(vals), max(vals)) if vals else (None, None)
        if lo is None:
            if bounds and all(math.isfinite(b) for b in bounds):
                return bounds[0], bounds[1], ("pole" if pole_lo else "edge"), \
                    ("pole" if pole_hi else "edge")
            raise MeshError("face without boundary on an unbounded surface")
        span = max(hi - lo, 1e-12)
        kl = kh = "margin"
        lo2, hi2 = lo - 0.05 * span, hi + 0.05 * span
        if bounds is not None:
            if pole_lo and math.isfinite(bounds[0]):
                lo2, kl = bounds[0], "pole"
            elif math.isfinite(bounds[0]) and lo2 < bounds[0]:
                lo2 = bounds[0]
                kl = "edge" if lo - bounds[0] < 1e-9 * span else "margin"
            if pole_hi and math.isfinite(bounds[1]):
                hi2, kh = bounds[1], "pole"
            elif math.isfinite(bounds[1]) and hi2 > bounds[1]:
                hi2 = bounds[1]
                kh = "edge" if bounds[1] - hi < 1e-9 * span else "margin"
        return lo2, hi2, kl, kh

    def _cut(self, lp, ulo, uhi, vlo, vhi, pu, pv):
        """Split an unwrapped loop where it crosses the seam lines, shifting
        each piece into the rectangle. Returns chains of (u, v, gid); a loop
        that crosses nothing comes back as one closed chain."""
        # closed polyline in unwrapped coordinates (closing point shifted by the winding)
        seq = list(lp)
        last = lp[-1]
        first = lp[0]
        fu, fv = first[0], first[1]
        if pu:
            fu = last[0] + _wrap(first[0] - last[0], pu)
        if pv:
            fv = last[1] + _wrap(first[1] - last[1], pv)
        seq.append((fu, fv, first[2]))
        cuts = []
        for axis, period, lo in ((0, pu, ulo), (1, pv, vlo)):
            if period:
                cuts.append((axis, period, lo))
        if not cuts:
            ch = _Chain(lp)
            ch.closed = True
            return [ch]
        # insert crossing points
        pts = [seq[0]]
        verts = self.verts
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            events = []
            for axis, period, lo in cuts:
                x0, x1 = a[axis], b[axis]
                if x0 == x1:
                    continue
                k0 = math.floor((min(x0, x1) - lo) / period)
                k1 = math.floor((max(x0, x1) - lo) / period)
                for k in range(k0, k1 + 1):
                    line = lo + k * period
                    if min(x0, x1) < line < max(x0, x1):
                        events.append(((line - x0) / (x1 - x0), axis, line))
            events.sort()
            for f, axis, line in events:
                u = a[0] + (b[0] - a[0]) * f
                v = a[1] + (b[1] - a[1]) * f
                if axis == 0:
                    u = line
                else:
                    v = line
                # a new mesh vertex on the edge's chord, so it lies on the
                # neighbouring face's edge too
                pa, pb = verts[a[2]], verts[b[2]]
                g = len(verts)
                verts.append(G.add(pa, G.mul(G.sub(pb, pa), f)))
                self.m.mesh.extra_boundary.add(g)
                self.m.mesh.splits.setdefault((a[2], b[2]), []).append((f, g))
                pts.append((u, v, g))
            pts.append(b)

        chains = []
        cur = []
        cur_cell = None
        mid_cells = []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
            c = []
            for axis, period, lo in cuts:
                c.append(math.floor((mid[axis] - lo) / period))
            mid_cells.append(tuple(c))
        if all(c == mid_cells[0] for c in mid_cells):
            shift = self._shift(mid_cells[0], cuts)
            ch = _Chain([self._apply(p, shift) for p in pts[:-1]])
            ch.closed = True
            return [ch]
        # rotate so the walk starts at a cell change
        start = next(i for i in range(len(mid_cells)) if mid_cells[i] != mid_cells[i - 1])
        segs = list(range(start, len(mid_cells))) + list(range(0, start))
        for i in segs:
            a, b = pts[i], pts[i + 1]
            c = mid_cells[i]
            if c != cur_cell:
                if cur:
                    chains.append(_Chain(cur))
                shift = self._shift(c, cuts)
                cur = [self._apply(a, shift)]
                cur_cell = c
            shift = self._shift(c, cuts)
            cur.append(self._apply(b, shift))
        if cur:
            chains.append(_Chain(cur))
        return chains

    @staticmethod
    def _shift(cellkey, cuts):
        du = dv = 0.0
        for (axis, period, lo), k in zip(cuts, cellkey):
            if axis == 0:
                du = -k * period
            else:
                dv = -k * period
        return du, dv

    @staticmethod
    def _apply(p, shift):
        return (p[0] + shift[0], p[1] + shift[1], p[2])

    # ------------------------------------------------------------ spacing
    def _metric(self, box):
        """Scale factors (su, sv) making parameter steps roughly lengths, and
        the step lengths (lx, ly) in those scaled units that the surface's
        curvature calls for (inf where it is straight)."""
        S = self.S
        ulo, uhi, vlo, vhi = box
        if isinstance(S, S_Plane):
            return 1.0, 1.0, float("inf"), float("inf")
        n = 5 if isinstance(S, S_Axial) else 7
        su = sv = 0.0
        hu = hv = float("inf")             # parameter steps needed
        cnt = 0
        # second differences over a sizeable step: smooth surfaces do not
        # mind, and rebuilt blends (straight between spine samples) need it
        du = (uhi - ulo) / 50.0 or 1e-6
        dv = (vhi - vlo) / 50.0 or 1e-6
        tol, amax = self.tol, self.m.max_angle

        def step(k):
            if k <= 1e-9:
                return float("inf")
            return min(math.sqrt(8.0 * tol / k), amax / k)
        for i in range(n):
            for j in range(n):
                u = ulo + (uhi - ulo) * (i + 0.5) / n
                v = vlo + (vhi - vlo) * (j + 0.5) / n
                try:
                    P, Pu, Pv = self._d1(u, v)
                    _, Pu1, Pv1 = self._d1(u + du, v)
                    _, Pu0, Pv0 = self._d1(u - du, v)
                    _, _, Pv2 = self._d1(u, v + dv)
                    _, _, Pv3 = self._d1(u, v - dv)
                except Exception:
                    continue
                lu, lv = G.norm(Pu), G.norm(Pv)
                su = max(su, lu)
                sv = max(sv, lv)
                cnt += 1
                nrm = G.cross(Pu, Pv)
                ln = G.norm(nrm)
                if ln <= 1e-30 or lu <= 1e-15 or lv <= 1e-15:
                    continue
                nrm = G.mul(nrm, 1.0 / ln)
                Puu = G.mul(G.sub(Pu1, Pu0), 0.5 / du)
                Pvv = G.mul(G.sub(Pv2, Pv3), 0.5 / dv)
                Puv = G.mul(G.sub(Pv1, Pv0), 0.5 / du)
                ku = abs(G.dot(Puu, nrm)) / (lu * lu)
                kv = abs(G.dot(Pvv, nrm)) / (lv * lv)
                kuv = abs(G.dot(Puv, nrm)) / (lu * lv)
                # length steps here, as parameter steps (lengths / local speed)
                hu = min(hu, step(ku) / lu, step(kuv) / lu)
                hv = min(hv, step(kv) / lv, step(kuv) / lv)
        if not cnt:
            raise MeshError("surface cannot be evaluated on this face")
        su = max(su, 1e-12)
        sv = max(sv, 1e-12)
        lx, ly = hu * su, hv * sv
        # keep grid cells within 3:1, or triangles fan out from one boundary
        # point across the curved direction
        if math.isfinite(lx) or math.isfinite(ly):
            lx = min(lx, 3.0 * ly)
            ly = min(ly, 3.0 * lx)
        return su, sv, lx, ly

    # ------------------------------------------------------------ triangulation
    def _triangulate(self, pts, segs, chains, box, rect):
        S = self.S
        ulo, uhi, vlo, vhi = box
        if uhi - ulo <= 0 or vhi - vlo <= 0:
            if rect is None:
                # a face with zero width in one direction: nothing to see
                return []
        su, sv, lx, ly = self._metric((ulo, uhi, vlo, vhi) if uhi > ulo and vhi > vlo
                                      else (ulo, ulo + 1e-6, vlo, vlo + 1e-6))
        W, H = (uhi - ulo) * su, (vhi - vlo) * sv
        nx = 1 if not math.isfinite(lx) else max(1, int(math.ceil(W / lx)))
        ny = 1 if not math.isfinite(ly) else max(1, int(math.ceil(H / ly)))
        limit = MAX_GRID if cdt.have_fast() else MAX_GRID_SLOW
        while (nx - 1) * (ny - 1) > limit:
            nx = max(1, int(nx * 0.8))
            ny = max(1, int(ny * 0.8))
        if rect is not None:
            # the sides need points as well, so the seams and poles have vertices
            nx = max(nx, 2 if S.pu else 1)
            ny = max(ny, 2 if S.pv else 1)
        hu = (uhi - ulo) / nx
        hv = (vhi - vlo) / ny
        xy = [(p[0] * su, p[1] * sv) for p in pts]
        gid = [p[2] for p in pts]
        uv = [(p[0], p[1]) for p in pts]
        # ---- rectangle sides
        side_idx = set()
        if rect is not None:
            sides, side_pts = rect

            side_idx = set()

            def add_point(u, v):
                xy.append((u * su, v * sv))
                uv.append((u, v))
                gid.append(None)
                side_idx.add(len(xy) - 1)
                return len(xy) - 1
            c00, c10 = add_point(ulo, vlo), add_point(uhi, vlo)
            c01, c11 = add_point(ulo, vhi), add_point(uhi, vhi)
            line_pts = {"vlo": [c00, c10], "vhi": [c01, c11], "ulo": [c00, c01], "uhi": [c10, c11]}
            # a side the face's own boundary runs along ("edge") gets no
            # points of its own: the neighbouring face only has the edge's
            for i in range(1, nx):
                u = ulo + hu * i
                if sides.get("vlo") != "edge":
                    line_pts["vlo"].append(add_point(u, vlo))
                if sides.get("vhi") != "edge":
                    line_pts["vhi"].append(add_point(u, vhi))
            for j in range(1, ny):
                v = vlo + hv * j
                if sides.get("ulo") != "edge":
                    line_pts["ulo"].append(add_point(ulo, v))
                if sides.get("uhi") != "edge":
                    line_pts["uhi"].append(add_point(uhi, v))
            for side in line_pts:
                line_pts[side] += side_pts[side]
                axis = 0 if side in ("vlo", "vhi") else 1
                line_pts[side].sort(key=lambda i: uv[i][axis])
                lp = line_pts[side]
                for a, b in zip(lp, lp[1:]):
                    segs.append((a, b))
        # ---- interior grid, kept away from the boundary
        cols = [ulo + hu * i for i in range(1, nx)]
        rows = [vlo + hv * j for j in range(1, ny)]
        # a direction needing no subdivision still gets one line down the
        # middle when the other is curved (narrow strips along a curve)
        if not cols and rows:
            cols = [0.5 * (ulo + uhi)]
        if not rows and cols:
            rows = [0.5 * (vlo + vhi)]
        if cols and rows:
            cu = hu * su if nx > 1 else W
            cv = hv * sv if ny > 1 else H
            near = min(cu, cv) * 0.35
            bucket = _SegmentBuckets([xy[i] for i in range(len(xy))], segs, max(near, 1e-12))
            inside_rows = None
            if rect is None:
                inside_rows = _scanline(xy, segs)
            for v in rows:
                y = v * sv
                spans = inside_rows(y) if inside_rows else None
                for u in cols:
                    x = u * su
                    if spans is not None and not any(a < x < b for a, b in spans):
                        continue
                    if bucket.near(x, y, near):
                        continue
                    xy.append((x, y))
                    uv.append((u, v))
                    gid.append(None)
        # ---- triangulate
        tris, fixed, alias, failed, extra = cdt.fast_triangulate(xy, segs)
        for (x, y) in extra:
            uv.append((x / su, y / sv))
            gid.append(None)
        adj = _adjacency(tris)
        if rect is None:
            pxy = xy + [(x, y) for (x, y) in extra] if extra else xy
            region = _inside_region(tris, fixed, adj, pxy, [(xy[a], xy[b]) for a, b in segs])
        else:
            region = self._seeded(tris, fixed, adj, chains, alias, xy, rect, box, su, sv)
        # ---- to mesh vertices
        verts = self.verts
        out = []
        cache = {}
        for k in region:
            ids = []
            for i in tris[k]:
                g = gid[i] if i < len(gid) else None
                if g is None:
                    g = cache.get(i)
                    if g is None:
                        u, v = uv[i]
                        g = len(verts)
                        verts.append(self._eval(u, v))
                        cache[i] = g
                        if rect is not None and i in side_idx:
                            self.m.mesh.weld.add(g)
                ids.append(g)
            a, b, c = ids
            if a == b or b == c or a == c:
                continue
            out.append((a, b, c) if self.out_sign > 0 or not S.trusted else (a, c, b))
        return out

    def _wrapuv(self, u, v):
        """Bring periodic parameters back into the surface's own range (a
        B-spline cannot be evaluated outside its knots)."""
        S = self.S
        if S.pu and S.ubounds is not None and not isinstance(S, S_Axial):
            lo = S.ubounds[0]
            if not (lo <= u <= lo + S.pu):
                u = lo + (u - lo) % S.pu
        if S.pv and S.vbounds is not None and not isinstance(S, S_Axial):
            lo = S.vbounds[0]
            if not (lo <= v <= lo + S.pv):
                v = lo + (v - lo) % S.pv
        return u, v

    def _eval(self, u, v):
        return self.S.eval(*self._wrapuv(u, v))

    def _d1(self, u, v):
        return self.S.d1(*self._wrapuv(u, v))

    def _seeded(self, tris, fixed, adj, chains, alias, xy, rect, box, su, sv):
        """Region grown from the material side of the boundary chains."""
        sides, _ = rect
        if not chains:
            return list(range(len(tris)))             # the whole surface
        edge_tri = {}
        for k, (a, b, c) in enumerate(tris):
            edge_tri[(a, b)] = k
            edge_tri[(b, c)] = k
            edge_tri[(c, a)] = k
        left, right = [], []
        for chain in chains:
            for a, b in chain[len(chain) // 2:] + chain[:len(chain) // 2]:
                a, b = alias[a], alias[b]
                if a == b or (min(a, b), max(a, b)) not in fixed:
                    continue
                kl, kr = edge_tri.get((a, b)), edge_tri.get((b, a))
                if kl is None or kr is None:
                    continue
                left.append(kl)
                right.append(kr)
                break
        if not left:
            raise MeshError("no usable boundary edge")
        ulo, uhi, vlo, vhi = box
        # bands outside the loops on 'margin' sides must stay empty
        allu = [xy[i][0] / su for ch in chains for e in ch for i in e]
        allv = [xy[i][1] / sv for ch in chains for e in ch for i in e]
        lim = (min(allu), max(allu), min(allv), max(allv))

        def leaks(region):
            for k in region:
                a, b, c = tris[k]
                cu = (xy[a][0] + xy[b][0] + xy[c][0]) / (3 * su)
                cv = (xy[a][1] + xy[b][1] + xy[c][1]) / (3 * sv)
                if sides.get("ulo") == "margin" and cu < lim[0]:
                    return True
                if sides.get("uhi") == "margin" and cu > lim[1]:
                    return True
                if sides.get("vlo") == "margin" and cv < lim[2]:
                    return True
                if sides.get("vhi") == "margin" and cv > lim[3]:
                    return True
            return False
        rl = _flood(tris, fixed, adj, left)
        rr = _flood(tris, fixed, adj, right)
        ok_l, ok_r = not leaks(rl), not leaks(rr)
        mat_left = self._material_left()
        if ok_l and not ok_r:
            return list(rl)
        if ok_r and not ok_l:
            return list(rr)
        if mat_left is None:
            # no orientation to go by: the smaller piece is the face
            return list(rl if len(rl) <= len(rr) else rr)
        return list(rl if mat_left else rr)


S_Axial = S._Axial
S_Plane = S.Plane


class _FuncCurve(S.Curve):
    def __init__(self, f):
        self.f = f
        self.t0, self.t1 = 0.0, 1.0

    def eval(self, t):
        return self.f(t)


class _Chain(list):
    closed = False


class _SegmentBuckets:
    """Quick 'is this point near a boundary segment' test."""

    def __init__(self, xy, segs, cell):
        self.cell = cell
        self.xy = xy
        self.b = {}
        for a, b in segs:
            if a >= len(xy) or b >= len(xy):
                continue
            (x0, y0), (x1, y1) = xy[a], xy[b]
            i0, i1 = sorted((int(math.floor(x0 / cell)), int(math.floor(x1 / cell))))
            j0, j1 = sorted((int(math.floor(y0 / cell)), int(math.floor(y1 / cell))))
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 4000:
                # very long segment: only its neighbourhood along the line
                n = int(max(i1 - i0, j1 - j0)) + 1
                for k in range(n + 1):
                    x = x0 + (x1 - x0) * k / n
                    y = y0 + (y1 - y0) * k / n
                    self.b.setdefault((int(math.floor(x / cell)), int(math.floor(y / cell))),
                                      []).append((a, b))
                continue
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    self.b.setdefault((i, j), []).append((a, b))

    def near(self, x, y, r):
        c = self.cell
        i, j = int(math.floor(x / c)), int(math.floor(y / c))
        xy = self.xy
        r2 = r * r
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b in self.b.get((i + di, j + dj), ()):
                    (x0, y0), (x1, y1) = xy[a], xy[b]
                    dx, dy = x1 - x0, y1 - y0
                    l2 = dx * dx + dy * dy
                    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / l2))
                    ex, ey = x0 + dx * t - x, y0 + dy * t - y
                    if ex * ex + ey * ey < r2:
                        return True
        return False


def _scanline(xy, segs):
    """Function y -> list of (x0, x1) spans inside the loops (even-odd)."""
    edges = []
    for a, b in segs:
        (x0, y0), (x1, y1) = xy[a], xy[b]
        if y0 == y1:
            continue
        if y0 > y1:
            x0, y0, x1, y1 = x1, y1, x0, y0
        edges.append((y0, y1, x0, (x1 - x0) / (y1 - y0)))
    edges.sort()
    ys = [e[0] for e in edges]
    import bisect

    def spans(y):
        k = bisect.bisect_right(ys, y)
        xs = [x0 + (y - y0) * slope for (y0, y1, x0, slope) in edges[:k] if y0 <= y < y1]
        xs.sort()
        return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]
    return spans
