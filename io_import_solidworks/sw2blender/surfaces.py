# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Parasolid surfaces and curves for meshing. Pure Python.

Every surface can be evaluated, differentiated and inverted (point -> u, v).
Every curve can be evaluated and inverted (point -> t). Parameters follow the
Parasolid definitions, except for rolling-ball blends, which Parasolid does not
store explicitly: those are rebuilt from their spine and supports (see Blend).
Lengths are in metres.
"""

import math

from . import xtgeom as G

TWO_PI = 2.0 * math.pi


class SurfaceError(ValueError):
    pass


def _wrap(d, period):
    """d shifted by whole periods into [-period/2, period/2)."""
    return d - period * math.floor(d / period + 0.5)


# ================================================================ NURBS basis
def _span(U, p, n, t):
    """Knot span index for parameter t (n = number of control points)."""
    if t >= U[n]:
        return n - 1
    if t <= U[p]:
        return p
    lo, hi = p, n
    mid = (lo + hi) // 2
    while t < U[mid] or t >= U[mid + 1]:
        if t < U[mid]:
            hi = mid
        else:
            lo = mid
        mid = (lo + hi) // 2
    return mid


def _basis0(U, p, i, t):
    """Non-zero basis functions N[i-p..i] at t (no derivatives)."""
    N = [1.0] + [0.0] * p
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    for j in range(1, p + 1):
        left[j] = t - U[i + 1 - j]
        right[j] = U[i + j] - t
        saved = 0.0
        for r in range(j):
            den = right[r + 1] + left[j - r]
            temp = N[r] / den if den != 0.0 else 0.0
            N[r] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        N[j] = saved
    return N


def _basis(U, p, i, t):
    """Non-zero basis functions N[i-p..i] and their first derivatives at t."""
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    ndu = [[0.0] * (p + 1) for _ in range(p + 1)]
    ndu[0][0] = 1.0
    for j in range(1, p + 1):
        left[j] = t - U[i + 1 - j]
        right[j] = U[i + j] - t
        saved = 0.0
        for r in range(j):
            ndu[j][r] = right[r + 1] + left[j - r]
            den = ndu[j][r]
            temp = ndu[r][j - 1] / den if den != 0.0 else 0.0
            ndu[r][j] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        ndu[j][j] = saved
    N = [ndu[r][p] for r in range(p + 1)]
    if p == 0:
        return N, [0.0]
    dN = []
    for r in range(p + 1):
        d = 0.0
        if r >= 1:
            den = ndu[p][r - 1]
            if den != 0.0:
                d += ndu[r - 1][p - 1] / den
        if r <= p - 1:
            den = ndu[p][r]
            if den != 0.0:
                d -= ndu[r][p - 1] / den
        dN.append(d * p)
    return N, dN


class _NurbsSurfEval:
    def __init__(self, ns):
        self.grid = ns.grid               # [iu][iv] homogeneous (wx, wy, wz, w)
        self.pu, self.pv = ns.pu, ns.pv
        self.U, self.V = ns.ucurve.U, ns.vcurve.U
        self.nu, self.nv = ns.nu, ns.nv
        self.u0, self.u1 = self.U[self.pu], self.U[self.nu]
        self.v0, self.v1 = self.V[self.pv], self.V[self.nv]

    def eval(self, u, v):
        pu, pv = self.pu, self.pv
        iu = _span(self.U, pu, self.nu, u)
        iv = _span(self.V, pv, self.nv, v)
        Nu = _basis0(self.U, pu, iu, u)
        Nv = _basis0(self.V, pv, iv, v)
        g = self.grid
        x = y = z = w = 0.0
        for a in range(pu + 1):
            row = g[iu - pu + a]
            na = Nu[a]
            if na == 0.0:
                continue
            for b in range(pv + 1):
                c = row[iv - pv + b]
                f = na * Nv[b]
                x += f * c[0]
                y += f * c[1]
                z += f * c[2]
                w += f * c[3]
        return (x / w, y / w, z / w)

    def d1(self, u, v):
        pu, pv = self.pu, self.pv
        iu = _span(self.U, pu, self.nu, u)
        iv = _span(self.V, pv, self.nv, v)
        Nu, dNu = _basis(self.U, pu, iu, u)
        Nv, dNv = _basis(self.V, pv, iv, v)
        s = [0.0] * 4
        su = [0.0] * 4
        sv = [0.0] * 4
        g = self.grid
        for a in range(pu + 1):
            row = g[iu - pu + a]
            na, dna = Nu[a], dNu[a]
            # combine this row in v first
            r0 = [0.0] * 4
            r1 = [0.0] * 4
            for b in range(pv + 1):
                c = row[iv - pv + b]
                nb, dnb = Nv[b], dNv[b]
                r0[0] += nb * c[0]
                r0[1] += nb * c[1]
                r0[2] += nb * c[2]
                r0[3] += nb * c[3]
                r1[0] += dnb * c[0]
                r1[1] += dnb * c[1]
                r1[2] += dnb * c[2]
                r1[3] += dnb * c[3]
            for k in range(4):
                s[k] += na * r0[k]
                su[k] += dna * r0[k]
                sv[k] += na * r1[k]
        w = s[3]
        P = (s[0] / w, s[1] / w, s[2] / w)
        Pu = ((su[0] - su[3] * P[0]) / w, (su[1] - su[3] * P[1]) / w, (su[2] - su[3] * P[2]) / w)
        Pv = ((sv[0] - sv[3] * P[0]) / w, (sv[1] - sv[3] * P[1]) / w, (sv[2] - sv[3] * P[2]) / w)
        return P, Pu, Pv


class _NurbsCurveEval:
    def __init__(self, nb):
        self.nb = nb
        self.p = nb.p
        self.U = nb.U
        self.n = nb.n
        self.cp = nb.cp
        self.dim = nb.dim

    def eval(self, t):
        p = self.p
        i = _span(self.U, p, self.n, t)
        N, _ = _basis(self.U, p, i, t)
        acc = [0.0] * (self.dim + 1)
        for a in range(p + 1):
            c = self.cp[i - p + a]
            na = N[a]
            for k in range(self.dim + 1):
                acc[k] += na * c[k]
        w = acc[-1]
        return tuple(acc[k] / w for k in range(self.dim))


# ================================================================ surfaces
class Surface:
    """Base class. Subclasses set the domain description and eval()."""

    pu = None            # period in u, or None
    pv = None
    ubounds = None       # (lo, hi) limits of the parameter, or None if unbounded
    vbounds = None
    # sides where a whole parameter line collapses to one point (a pole)
    pole_ulo = pole_uhi = pole_vlo = pole_vhi = False
    closed = False       # a face may cover the surface up to its domain boundary
    trusted = True       # u x v follows the Parasolid surface normal
    size = 1.0           # typical size, for tolerances

    def eval(self, u, v):
        raise NotImplementedError

    def d1(self, u, v):
        h = 1e-6 * self._hscale()
        P = self.eval(u, v)
        a, b = self.eval(u + h, v), self.eval(u - h, v)
        c, d = self.eval(u, v + h), self.eval(u, v - h)
        k = 0.5 / h
        Pu = ((a[0] - b[0]) * k, (a[1] - b[1]) * k, (a[2] - b[2]) * k)
        Pv = ((c[0] - d[0]) * k, (c[1] - d[1]) * k, (c[2] - d[2]) * k)
        return P, Pu, Pv

    def _hscale(self):
        return 1.0

    def normal(self, u, v):
        _, Pu, Pv = self.d1(u, v)
        n = G.cross(Pu, Pv)
        ln = G.norm(n)
        return (n[0] / ln, n[1] / ln, n[2] / ln) if ln > 0 else None

    def inverse(self, p, hint=None):
        raise NotImplementedError

    # ---------------------------------------------------------- helpers
    def newton(self, p, u, v, iters=30):
        """Closest point by Gauss-Newton from (u, v). Returns (u, v, distance);
        self.converged says whether the steps died away."""
        ub, vb = self.ubounds, self.vbounds
        self.converged = False
        for _ in range(iters):
            P, Pu, Pv = self.d1(u, v)
            r = (p[0] - P[0], p[1] - P[1], p[2] - P[2])
            a = G.dot(Pu, Pu)
            b = G.dot(Pu, Pv)
            c = G.dot(Pv, Pv)
            ru, rv = G.dot(r, Pu), G.dot(r, Pv)
            det = a * c - b * b
            if det <= 1e-30 * (a * c + 1e-300):
                # degenerate (pole): move along whichever direction is defined
                if a > c and a > 0:
                    du, dv = ru / a, 0.0
                elif c > 0:
                    du, dv = 0.0, rv / c
                else:
                    break
            else:
                du = (c * ru - b * rv) / det
                dv = (a * rv - b * ru) / det
            nu, nv = u + du, v + dv
            if ub is not None:
                if self.pu:
                    nu = ub[0] + (nu - ub[0]) % self.pu if not (ub[0] <= nu <= ub[1]) else nu
                else:
                    nu = min(max(nu, ub[0]), ub[1])
            if vb is not None:
                if self.pv:
                    nv = vb[0] + (nv - vb[0]) % self.pv if not (vb[0] <= nv <= vb[1]) else nv
                else:
                    nv = min(max(nv, vb[0]), vb[1])
            step = abs(nu - u) + abs(nv - v)
            u, v = nu, nv
            if step < 1e-11 * (1.0 + abs(u) + abs(v)):
                self.converged = True
                break
        else:
            self.converged = False
        P = self.eval(u, v)
        return u, v, G.dist(P, p)

    def is_pole(self, u, v, tol):
        """True if (u, v) lies on a pole (where u or v is meaningless)."""
        if self.pole_vlo and self.vbounds and abs(v - self.vbounds[0]) < tol:
            return "v"
        if self.pole_vhi and self.vbounds and abs(v - self.vbounds[1]) < tol:
            return "v"
        if self.pole_ulo and self.ubounds and abs(u - self.ubounds[0]) < tol:
            return "u"
        if self.pole_uhi and self.ubounds and abs(u - self.ubounds[1]) < tol:
            return "u"
        return None


class Plane(Surface):
    def __init__(self, s):
        self.o = tuple(s["pvec"])
        self.n = G.unit(s["normal"])
        x = s["x_axis"]
        self.x = G.unit(G.sub(x, G.mul(self.n, G.dot(x, self.n))))
        self.y = G.cross(self.n, self.x)

    def eval(self, u, v):
        o, x, y = self.o, self.x, self.y
        return (o[0] + x[0] * u + y[0] * v, o[1] + x[1] * u + y[1] * v, o[2] + x[2] * u + y[2] * v)

    def d1(self, u, v):
        return self.eval(u, v), self.x, self.y

    def inverse(self, p, hint=None):
        d = G.sub(p, self.o)
        return G.dot(d, self.x), G.dot(d, self.y)


class _Axial(Surface):
    """Shared frame for surfaces of revolution about an axis."""

    def _frame(self, origin, axis, xaxis):
        self.o = tuple(origin)
        self.a = G.unit(axis)
        self.x = G.unit(G.sub(xaxis, G.mul(self.a, G.dot(xaxis, self.a))))
        self.y = G.cross(self.a, self.x)

    def _local(self, p):
        d = G.sub(p, self.o)
        return G.dot(d, self.x), G.dot(d, self.y), G.dot(d, self.a)

    def _ring(self, u):
        c, s = math.cos(u), math.sin(u)
        x, y = self.x, self.y
        return (x[0] * c + y[0] * s, x[1] * c + y[1] * s, x[2] * c + y[2] * s), \
               (-x[0] * s + y[0] * c, -x[1] * s + y[1] * c, -x[2] * s + y[2] * c)


class Cylinder(_Axial):
    pu = TWO_PI
    ubounds = (0.0, TWO_PI)

    def __init__(self, s):
        self._frame(s["pvec"], s["axis"], s["x_axis"])
        self.r = s["radius"]
        self.size = abs(self.r)

    def eval(self, u, v):
        rg, _ = self._ring(u)
        o, a, r = self.o, self.a, self.r
        return tuple(o[k] + a[k] * v + rg[k] * r for k in range(3))

    def d1(self, u, v):
        rg, tg = self._ring(u)
        r = self.r
        return self.eval(u, v), (tg[0] * r, tg[1] * r, tg[2] * r), self.a

    def inverse(self, p, hint=None):
        lx, ly, lz = self._local(p)
        return math.atan2(ly, lx) % TWO_PI, lz


class Cone(_Axial):
    pu = TWO_PI
    ubounds = (0.0, TWO_PI)

    def __init__(self, s):
        self._frame(s["pvec"], s["axis"], s["x_axis"])
        self.r = s["radius"]
        self.t = s["sin_half_angle"] / s["cos_half_angle"]
        self.size = max(abs(self.r), 1e-3)
        if abs(self.t) > 1e-12:
            apex = -self.r / self.t
            if self.t > 0:
                self.vbounds = (apex, float("inf"))
                self.pole_vlo = True
            else:
                self.vbounds = (float("-inf"), apex)
                self.pole_vhi = True

    def eval(self, u, v):
        rg, _ = self._ring(u)
        o, a = self.o, self.a
        rr = self.r + v * self.t
        return tuple(o[k] + a[k] * v + rg[k] * rr for k in range(3))

    def d1(self, u, v):
        rg, tg = self._ring(u)
        rr = self.r + v * self.t
        a, t = self.a, self.t
        return (self.eval(u, v), (tg[0] * rr, tg[1] * rr, tg[2] * rr),
                (a[0] + rg[0] * t, a[1] + rg[1] * t, a[2] + rg[2] * t))

    def inverse(self, p, hint=None):
        lx, ly, lz = self._local(p)
        rad = math.hypot(lx, ly)
        u = math.atan2(ly, lx) % TWO_PI if rad > 1e-15 else (hint[0] if hint else 0.0)
        t = self.t
        v = (lz + t * (rad - self.r)) / (1.0 + t * t)
        if self.vbounds:
            v = min(max(v, self.vbounds[0]), self.vbounds[1])
        return u, v


class Sphere(_Axial):
    pu = TWO_PI
    ubounds = (0.0, TWO_PI)
    vbounds = (-0.5 * math.pi, 0.5 * math.pi)
    pole_vlo = pole_vhi = True
    closed = True

    def __init__(self, s):
        self._frame(s["centre"], s["axis"], s["x_axis"])
        self.r = s["radius"]
        self.size = abs(self.r)

    def eval(self, u, v):
        rg, _ = self._ring(u)
        o, a, r = self.o, self.a, self.r
        cv, sv = math.cos(v), math.sin(v)
        return tuple(o[k] + rg[k] * r * cv + a[k] * r * sv for k in range(3))

    def d1(self, u, v):
        rg, tg = self._ring(u)
        r, a = self.r, self.a
        cv, sv = math.cos(v), math.sin(v)
        return (self.eval(u, v), tuple(tg[k] * r * cv for k in range(3)),
                tuple(-rg[k] * r * sv + a[k] * r * cv for k in range(3)))

    def inverse(self, p, hint=None):
        lx, ly, lz = self._local(p)
        rad = math.hypot(lx, ly)
        v = math.atan2(lz, rad)
        u = math.atan2(ly, lx) % TWO_PI if rad > 1e-12 * self.size else (hint[0] if hint else 0.0)
        return u, v


class Torus(_Axial):
    pu = TWO_PI
    pv = TWO_PI
    ubounds = (0.0, TWO_PI)
    vbounds = (-math.pi, math.pi)
    closed = True

    def __init__(self, s):
        self._frame(s["centre"], s["axis"], s["x_axis"])
        self.R = s["major_radius"]
        self.r = s["minor_radius"]
        self.size = abs(self.R) + abs(self.r)
        if abs(self.R) <= self.r * (1 + 1e-12):
            # apple / lemon torus: only the part where R + r cos v >= 0 exists
            lim = math.acos(max(-1.0, min(1.0, -self.R / self.r)))
            self.pv = None
            self.vbounds = (-lim, lim)
            self.pole_vlo = self.pole_vhi = True

    def eval(self, u, v):
        rg, _ = self._ring(u)
        o, a = self.o, self.a
        q = self.R + self.r * math.cos(v)
        z = self.r * math.sin(v)
        return tuple(o[k] + rg[k] * q + a[k] * z for k in range(3))

    def d1(self, u, v):
        rg, tg = self._ring(u)
        q = self.R + self.r * math.cos(v)
        r, a = self.r, self.a
        sv, cv = math.sin(v), math.cos(v)
        return (self.eval(u, v), tuple(tg[k] * q for k in range(3)),
                tuple(-rg[k] * r * sv + a[k] * r * cv for k in range(3)))

    def inverse(self, p, hint=None):
        lx, ly, lz = self._local(p)
        rad = math.hypot(lx, ly)
        u = math.atan2(ly, lx) % TWO_PI if rad > 1e-12 * self.size else (hint[0] if hint else 0.0)
        v = math.atan2(lz, rad - self.R)
        if self.pv is None:
            # lemon: the tube centre may be on the far side of the axis
            v2 = math.atan2(lz, -rad - self.R)
            lo, hi = self.vbounds
            if not (lo <= v <= hi) and lo <= v2 <= hi:
                u = (u + math.pi) % TWO_PI
                v = v2
            v = min(max(v, lo), hi)
        return u, v


class _Sampled(Surface):
    """Surfaces inverted by Newton iteration, seeded from a table of samples."""

    def _build_table(self, us, vs):
        self.table = []
        for u in us:
            for v in vs:
                self.table.append((self.eval(u, v), u, v))
        self._grid = None

    def _seed(self, p):
        """Table entry nearest to p (through a coarse 3-D grid)."""
        if self._grid is None:
            pts = [e[0] for e in self.table]
            lo = [min(q[k] for q in pts) for k in range(3)]
            hi = [max(q[k] for q in pts) for k in range(3)]
            cell = max(max(hi[k] - lo[k] for k in range(3)) / 16.0, 1e-12)
            grid = {}
            for e in self.table:
                q = e[0]
                grid.setdefault((int((q[0] - lo[0]) // cell), int((q[1] - lo[1]) // cell),
                                 int((q[2] - lo[2]) // cell)), []).append(e)
            self._grid = (lo, cell, grid)
        lo, cell, grid = self._grid
        c = (int((p[0] - lo[0]) // cell), int((p[1] - lo[1]) // cell), int((p[2] - lo[2]) // cell))
        best, bd = None, float("inf")
        for r in (1, 2, 4):
            for i in range(c[0] - r, c[0] + r + 1):
                for j in range(c[1] - r, c[1] + r + 1):
                    for k in range(c[2] - r, c[2] + r + 1):
                        for e in grid.get((i, j, k), ()):
                            q = e[0]
                            d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2
                            if d < bd:
                                best, bd = e, d
            if best is not None and bd <= (r * cell) ** 2:
                return best
        if best is None:
            best = min(self.table, key=lambda e: (e[0][0] - p[0]) ** 2 + (e[0][1] - p[1]) ** 2
                       + (e[0][2] - p[2]) ** 2)
        return best

    def inverse(self, p, hint=None):
        near = 1e-3 * max(self.size, 1e-9)
        d = float("inf")
        if hint is not None:
            u, v, d = self.newton(p, hint[0], hint[1])
            if d <= 1e-7 * max(self.size, 1e-9) or (self.converged and d <= near):
                return u, v
        best = self._seed(p)
        u2, v2, d2 = self.newton(p, best[1], best[2])
        if hint is None or d2 < d:
            return u2, v2
        return u, v


class BSurface(_Sampled):
    def __init__(self, t, s):
        ns, raw = G.nurbs_surface(t, s)
        self.ev = _NurbsSurfEval(ns)
        e = self.ev
        self.ubounds = (e.u0, e.u1)
        self.vbounds = (e.v0, e.v1)
        nu = max(8, min(48, 4 * (len(ns.uknots) - 1)))
        nv = max(8, min(48, 4 * (len(ns.vknots) - 1)))
        us = [e.u0 + (e.u1 - e.u0) * i / (nu - 1) for i in range(nu)]
        vs = [e.v0 + (e.v1 - e.v0) * j / (nv - 1) for j in range(nv)]
        self._build_table(us, vs)
        pts = [x[0] for x in self.table]
        self.size = max(G.dist(pts[0], q) for q in pts) or 1.0
        tol = 1e-9 * self.size
        # closed / periodic directions and collapsed boundary lines
        if raw["u_periodic"] or raw["u_closed"] or all(
                G.dist(self.eval(e.u0, v), self.eval(e.u1, v)) < tol for v in vs):
            self.pu = e.u1 - e.u0
        if raw["v_periodic"] or raw["v_closed"] or all(
                G.dist(self.eval(u, e.v0), self.eval(u, e.v1)) < tol for u in us):
            self.pv = e.v1 - e.v0

        def collapsed(points):
            return all(G.dist(points[0], q) < tol for q in points)
        self.pole_vlo = collapsed([self.eval(u, e.v0) for u in us])
        self.pole_vhi = collapsed([self.eval(u, e.v1) for u in us])
        self.pole_ulo = collapsed([self.eval(e.u0, v) for v in vs])
        self.pole_uhi = collapsed([self.eval(e.u1, v) for v in vs])
        self.closed = bool((self.pu or self.pole_ulo and self.pole_uhi) and
                           (self.pv or self.pole_vlo and self.pole_vhi)) or \
            bool(self.pu and self.pole_vlo and self.pole_vhi) or \
            bool(self.pv and self.pole_ulo and self.pole_uhi)

    def _hscale(self):
        return max(self.ubounds[1] - self.ubounds[0], self.vbounds[1] - self.vbounds[0])

    def eval(self, u, v):
        return self.ev.eval(u, v)

    def d1(self, u, v):
        return self.ev.d1(u, v)


class Swept(_Sampled):
    """SWEPT_SURF: section curve moved along a straight sweep vector."""

    def __init__(self, t, s, curve):
        self.c = curve
        self.sw = tuple(s["sweep"])
        self.sw2 = G.dot(self.sw, self.sw)
        lo, hi = curve.t0, curve.t1
        self.ubounds = (lo, hi)
        if curve.period:
            self.pu = curve.period
        n = 64
        self.us = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        self.cpts = [curve.eval(u) for u in self.us]
        self.size = max(max(G.dist(self.cpts[0], q) for q in self.cpts), math.sqrt(self.sw2)) or 1.0

    def _hscale(self):
        return max(self.ubounds[1] - self.ubounds[0], 1e-9)

    def eval(self, u, v):
        c = self.c.eval(u)
        w = self.sw
        return (c[0] + w[0] * v, c[1] + w[1] * v, c[2] + w[2] * v)

    def inverse(self, p, hint=None):
        if hint is not None:
            u, v, d = self.newton(p, hint[0], hint[1])
            if d <= 1e-7 * self.size:
                return u, v
        best = None
        for u, c in zip(self.us, self.cpts):
            d = G.sub(p, c)
            v = G.dot(d, self.sw) / self.sw2
            e = G.norm(G.sub(d, G.mul(self.sw, v)))
            if best is None or e < best[0]:
                best = (e, u, v)
        u, v, _ = self.newton(p, best[1], best[2])
        return u, v


class Spun(_Sampled):
    """SPUN_SURF: profile curve (u) rotated about an axis by angle v."""
    pv = TWO_PI

    def __init__(self, t, s, curve):
        self.c = curve
        self.base = tuple(s["base"])
        self.a = G.unit(s["axis"])
        self.ubounds = (curve.t0, curve.t1)
        self.vbounds = (0.0, TWO_PI)
        if curve.period:
            self.pu = curve.period
        n = 64
        lo, hi = curve.t0, curve.t1
        self.us = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        self.cpts = [curve.eval(u) for u in self.us]
        self.size = max(G.dist(self.cpts[0], q) for q in self.cpts) or 1.0
        tol = 1e-9 * self.size
        self.pole_ulo = self._radius(self.cpts[0]) < tol
        self.pole_uhi = self._radius(self.cpts[-1]) < tol
        self.closed = bool(self.pu or (self.pole_ulo and self.pole_uhi))

    def _hscale(self):
        return max(self.ubounds[1] - self.ubounds[0], TWO_PI)

    def _axial(self, p):
        d = G.sub(p, self.base)
        h = G.dot(d, self.a)
        rv = G.sub(d, G.mul(self.a, h))
        return h, rv

    def _radius(self, p):
        return G.norm(self._axial(p)[1])

    def eval(self, u, v):
        c = self.c.eval(u)
        a = self.a
        z = G.add(self.base, G.mul(a, G.dot(G.sub(c, self.base), a)))
        r = G.sub(c, z)
        return G.add(z, G.add(G.mul(r, math.cos(v)), G.mul(G.cross(a, r), math.sin(v))))

    def inverse(self, p, hint=None):
        if hint is not None:
            u, v, d = self.newton(p, hint[0], hint[1])
            if d <= 1e-7 * self.size:
                return u, v
        hp, rp = self._axial(p)
        best = None
        for u, c in zip(self.us, self.cpts):
            hc, rc = self._axial(c)
            e = math.hypot(hp - hc, G.norm(rp) - G.norm(rc))
            if best is None or e < best[0]:
                best = (e, u, rc)
        rc = best[2]
        if G.norm(rc) > 1e-15 and G.norm(rp) > 1e-15:
            ang = math.atan2(G.dot(G.cross(rc, rp), self.a), G.dot(rc, rp)) % TWO_PI
        else:
            ang = 0.0
        u, v, _ = self.newton(p, best[1], ang)
        return u, v


class Offset(Surface):
    def __init__(self, s, base):
        self.b = base
        self.off = s["offset"] * (-1.0 if base.sense_minus else 1.0)
        for k in ("pu", "pv", "ubounds", "vbounds", "pole_ulo", "pole_uhi", "pole_vlo",
                  "pole_vhi", "closed"):
            setattr(self, k, getattr(base, k))
        self.size = base.size + abs(self.off)

    def _hscale(self):
        return self.b._hscale()

    def eval(self, u, v):
        p = self.b.eval(u, v)
        n = self.b.normal(u, v)
        if n is None:
            return p
        return G.add(p, G.mul(n, self.off))

    def inverse(self, p, hint=None):
        return self.b.inverse(p, hint)


class Blend(Surface):
    """Rolling-ball blend rebuilt from its spine (the path of the ball's
    centre) and its two support surfaces. u runs along the spine as a sample
    index, v from 0 at support 0 to 1 at support 1."""
    trusted = False

    def __init__(self, centres, d0, d1, radius, closed):
        self.c = centres
        self.d0 = d0
        self.d1s = d1
        self.r = radius
        n = len(centres)
        self.n = n
        self.size = radius
        self.vbounds = (0.0, 1.0)
        if closed:
            self.pu = float(n)
            self.ubounds = (0.0, float(n))
        else:
            self.ubounds = (0.0, float(n - 1))
        self.closed_spine = closed
        self.ang = [math.acos(max(-1.0, min(1.0, G.dot(a, b)))) for a, b in zip(d0, d1)]

    def _hscale(self):
        return float(self.n)

    def _at(self, u):
        n = self.n
        if self.closed_spine:
            u = u % n
            i = int(math.floor(u))
            j = (i + 1) % n
        else:
            u = min(max(u, 0.0), n - 1.0)
            i = min(int(math.floor(u)), n - 2)
            j = i + 1
        f = u - i
        c = G.add(G.mul(self.c[i], 1 - f), G.mul(self.c[j], f))
        a = G.unit(G.add(G.mul(self.d0[i], 1 - f), G.mul(self.d0[j], f)))
        b = G.unit(G.add(G.mul(self.d1s[i], 1 - f), G.mul(self.d1s[j], f)))
        return c, a, b

    def eval(self, u, v):
        c, a, b = self._at(u)
        ang = math.acos(max(-1.0, min(1.0, G.dot(a, b))))
        if ang < 1e-9:
            d = a
        else:
            s = math.sin(ang)
            d = G.add(G.mul(a, math.sin((1 - v) * ang) / s), G.mul(b, math.sin(v * ang) / s))
        return G.add(c, G.mul(d, self.r))

    def inverse(self, p, hint=None):
        n = self.n
        segs = n if self.closed_spine else n - 1
        best = None
        rng = range(segs)
        if hint is not None:
            i0 = int(hint[0])
            rng = [k % segs if self.closed_spine else k
                   for k in range(i0 - 3, i0 + 4) if self.closed_spine or 0 <= k < segs]
        for attempt in (0, 1):
            for i in rng:
                a, b = self.c[i], self.c[(i + 1) % n]
                ab = G.sub(b, a)
                l2 = G.dot(ab, ab)
                f = 0.0 if l2 == 0 else max(0.0, min(1.0, G.dot(G.sub(p, a), ab) / l2))
                # nearest spine point: the ball centre whose cross-section holds p
                d = G.dist(p, G.add(a, G.mul(ab, f)))
                if best is None or d < best[0]:
                    best = (d, i + f)
            if attempt or (best[0] < 2.0 * self.r and 0 < best[1] < segs):
                break
            best = None
            rng = range(segs)
        u = best[1]
        c, a, b = self._at(u)
        d = G.sub(p, c)
        ang_ab = math.acos(max(-1.0, min(1.0, G.dot(a, b))))
        if ang_ab < 1e-12:
            return u, 0.0
        axis = G.cross(a, b)
        ang = math.atan2(G.dot(G.cross(a, d), axis) / G.norm(axis), G.dot(a, d))
        return u, max(-0.5, min(1.5, ang / ang_ab))


# ================================================================ curves
class Curve:
    t0 = None
    t1 = None
    period = None

    def eval(self, t):
        raise NotImplementedError

    def param(self, p, hint=None):
        raise NotImplementedError

    def _newton1(self, p, t, iters=30):
        h = 1e-7 * max(abs(self.t1 - self.t0), 1e-9) if self.t0 is not None else 1e-7
        for _ in range(iters):
            c = self.eval(t)
            d1 = G.mul(G.sub(self.eval(t + h), self.eval(t - h)), 0.5 / h)
            d2 = G.mul(G.add(G.sub(self.eval(t + h), G.mul(c, 2.0)), self.eval(t - h)), 1.0 / (h * h))
            r = G.sub(c, p)
            f = G.dot(r, d1)
            df = G.dot(d1, d1) + G.dot(r, d2)
            if df <= 0:
                df = G.dot(d1, d1)
            if df == 0:
                break
            nt = t - f / df
            if self.period is None and self.t0 is not None:
                nt = min(max(nt, self.t0), self.t1)
            if abs(nt - t) < 1e-14 * (1 + abs(t)):
                t = nt
                break
            t = nt
        return t


class Line(Curve):
    def __init__(self, c):
        self.o = tuple(c["pvec"])
        self.d = tuple(c["direction"])

    def eval(self, t):
        return G.add(self.o, G.mul(self.d, t))

    def param(self, p, hint=None):
        return G.dot(G.sub(p, self.o), self.d) / G.dot(self.d, self.d)


class Circle(Curve):
    t0, t1, period = 0.0, TWO_PI, TWO_PI

    def __init__(self, c, ellipse=False):
        self.o = tuple(c["centre"])
        n = G.unit(c["normal"])
        x = c["x_axis"]
        self.x = G.unit(G.sub(x, G.mul(n, G.dot(x, n))))
        self.y = G.cross(n, self.x)
        if ellipse:
            self.a, self.b = c["major_radius"], c["minor_radius"]
        else:
            self.a = self.b = c["radius"]
        self.radius = max(abs(self.a), abs(self.b))

    def eval(self, t):
        ca, sa = math.cos(t) * self.a, math.sin(t) * self.b
        o, x, y = self.o, self.x, self.y
        return (o[0] + x[0] * ca + y[0] * sa, o[1] + x[1] * ca + y[1] * sa,
                o[2] + x[2] * ca + y[2] * sa)

    def param(self, p, hint=None):
        d = G.sub(p, self.o)
        t = math.atan2(G.dot(d, self.y) / self.b, G.dot(d, self.x) / self.a) % TWO_PI
        if self.a != self.b:
            t = self._newton1(p, t) % TWO_PI
        return t


class _SampledCurve(Curve):
    def _table(self, n=64):
        self.ts = [self.t0 + (self.t1 - self.t0) * i / (n - 1) for i in range(n)]
        self.pts = [self.eval(t) for t in self.ts]
        tol = 1e-9 * (max(G.dist(self.pts[0], q) for q in self.pts) or 1.0)
        if G.dist(self.pts[0], self.pts[-1]) <= tol and self.period is None:
            self.period = self.t1 - self.t0

    def param(self, p, hint=None):
        if hint is not None:
            t = self._newton1(p, hint)
            if G.dist(self.eval(t), p) < 1e-8:
                return t
        i = min(range(len(self.pts)), key=lambda k: G.dist(self.pts[k], p))
        return self._newton1(p, self.ts[i])


class BCurve(_SampledCurve):
    def __init__(self, t, c):
        nb, nc = G.nurbs_curve(t, c)
        if nb.dim != 3:
            raise SurfaceError("2-D B-curve used as a 3-D curve")
        self.ev = _NurbsCurveEval(nb)
        self.t0, self.t1 = nb.t0, nb.t1
        if nc.get("periodic"):
            self.period = self.t1 - self.t0
        self._table(max(32, 8 * (len(nb.distinct) - 1)))

    def eval(self, t):
        return self.ev.eval(t)


class SPCurve(_SampledCurve):
    def __init__(self, t, c, surf):
        nb, _ = G.nurbs_curve(t, t[c["b_curve"]])
        self.pc = _NurbsCurveEval(nb)
        self.s = surf
        self.t0, self.t1 = nb.t0, nb.t1
        self._table(max(32, 8 * (len(nb.distinct) - 1)))

    def uv(self, t):
        return self.pc.eval(t)

    def eval(self, t):
        u, v = self.pc.eval(t)[:2]
        return self.s.eval(u, v)


class Trimmed(Curve):
    def __init__(self, c, basis):
        self.b = basis
        self.t0, self.t1 = c["parm_1"], c["parm_2"]
        self.p1, self.p2 = tuple(c["point_1"]), tuple(c["point_2"])

    def eval(self, t):
        return self.b.eval(t)

    def param(self, p, hint=None):
        return self.b.param(p, hint)


class Polyline(Curve):
    """A curve known as points (an intersection chart), refined on demand by
    `snap`, which moves a point onto the true curve."""

    def __init__(self, pts, closed, snap=None, chord_err=None):
        self.pts = pts
        self.closed = closed
        self.snap = snap
        self.chord_err = chord_err      # most the chart's chords stray from the curve
        self.refined = {}               # tolerance -> refined points


class OnSurface(Curve):
    """A curve lying on a surface whose parameters we cannot use (a rebuilt
    blend); edges on it are drawn between their end points."""

    def __init__(self, surf):
        self.s = surf


class CurveSupport:
    """A curve standing in for a blend support (a cliff edge): the contact
    point is the nearest point of the curve. Parameters are (t, 0)."""

    def __init__(self, curve):
        self.c = curve

    def inverse(self, p, hint=None):
        return self.c.param(p, hint[0] if hint else None), 0.0

    def eval(self, t, _v=0.0):
        return self.c.eval(t)

    def normal(self, *a):
        return None


class PolylineSupport:
    """Cliff edge known only as points: parameter = fractional point index."""

    def __init__(self, pts, closed):
        self.pts = pts
        self.closed = closed

    def inverse(self, p, hint=None):
        pts = self.pts
        n = len(pts)
        segs = n if self.closed else n - 1
        best = (float("inf"), 0.0)
        for i in range(segs):
            a, b = pts[i], pts[(i + 1) % n]
            ab = G.sub(b, a)
            l2 = G.dot(ab, ab)
            f = 0.0 if l2 == 0 else max(0.0, min(1.0, G.dot(G.sub(p, a), ab) / l2))
            d = G.dist(p, G.add(a, G.mul(ab, f)))
            if d < best[0]:
                best = (d, i + f)
        return best[1], 0.0

    def eval(self, t, _v=0.0):
        pts = self.pts
        n = len(pts)
        i = int(math.floor(t))
        f = t - i
        a = pts[i % n]
        b = pts[(i + 1) % n] if (self.closed or i + 1 < n) else a
        return G.add(a, G.mul(G.sub(b, a), f))

    def normal(self, *a):
        return None
