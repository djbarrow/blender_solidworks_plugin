# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""SolidWorks part -> triangle meshes, one per body. Pure Python.

mesh_part(path) returns a list of Body objects:

    label        name for the object
    verts        array('f') of x, y, z in millimetres
    tris         array('i'), 3 vertex indices per triangle (counter-clockwise
                 seen from outside)
    tri_colour   array('i'), index into colours for each triangle
    colours      [[r, g, b], ...] 0..1
    transparency 0..1
"""

import array
import os

from .loader import ConversionError, load_transmit
from .tessellate import Log, Mesher, MeshError

# SolidWorks' default part colour, for bodies that carry no colour
DEFAULT_RGB = (202 / 255.0, 209 / 255.0, 238 / 255.0)


class Body:
    def __init__(self, label, verts, tris, tri_colour, colours, transparency, kind):
        self.label = label
        self.verts = verts
        self.tris = tris
        self.tri_colour = tri_colour
        self.colours = colours
        self.transparency = transparency
        self.kind = kind            # "solid", "sheet" or "open" (a solid with gaps)


def mesh_part(path, detail=0.0005, keep_duplicates=False, verbose=False, progress=None):
    """Mesh every body of a part file. Returns (bodies, log)."""
    t, _ = load_transmit(path)
    log = Log(verbose)
    m = Mesher(t, detail=detail, log=log)
    colours, trans = m.colour_attributes()
    items = m.bodies()
    out = []
    for i, (body, faces) in enumerate(items):
        if progress:
            progress(i, len(items))
        try:
            bm = m.mesh_body(body, faces)
        except MeshError as exc:
            log.warn("body %s: %s" % (body.index if body else "?", exc))
            continue
        bidx = body.index if body is not None else None
        res = _finish(bm, colours.get(bidx), log)
        if res is None:
            continue
        verts, tris, tri_col, palette, kind = res
        out.append(Body(None, verts, tris, tri_col, palette, trans.get(bidx, 0.0), kind))
    if not out:
        raise ConversionError("no bodies could be meshed: " + "; ".join(log.warnings[:5]))
    if not keep_duplicates:
        out = _drop_duplicates(out, log)
    base = os.path.splitext(os.path.basename(path))[0]
    for i, b in enumerate(out):
        b.label = base if len(out) == 1 else "%s_Body%d" % (base, i + 1)
    st = m.stats
    if st["failed_faces"]:
        log.warn("%d of %d faces could not be meshed" % (st["failed_faces"], st["faces"]))
    if st["approx_faces"]:
        log.info("%d faces filled approximately" % st["approx_faces"])
    log.stats = st
    return out, log


# ------------------------------------------------------------ finishing a body
def _finish(bm, body_rgb, log):
    verts = bm.verts
    n = len(verts)
    # merge vertices at the same place: only seam and pole points can be
    # duplicates; every other vertex is already shared by id
    remap = list(range(n))
    cand = sorted(bm.weld)
    if cand:
        lo = [min(verts[i][k] for i in cand) for k in range(3)]
        hi = [max(verts[i][k] for i in cand) for k in range(3)]
        span = max(hi[k] - lo[k] for k in range(3))
        allp = verts
        q = max(span, max(abs(c) for c in lo + hi), 1e-9) * 1e-9
        inv = 1.0 / q
        index = {}
        for i in cand:
            p = allp[i]
            key = (int(round(p[0] * inv)), int(round(p[1] * inv)), int(round(p[2] * inv)))
            j = index.get(key)
            if j is None:
                index[key] = i
            else:
                remap[i] = j
    out_v = verts
    tris = []
    seen = set()
    for a, b, c, f in _split_t_junctions(bm.tris, bm.splits):
        a, b, c = remap[a], remap[b], remap[c]
        if a == b or b == c or a == c:
            continue
        key = tuple(sorted((a, b, c)))
        if key in seen:
            continue
        seen.add(key)
        tris.append([a, b, c, f])
    if not tris:
        return None
    bset = {remap[i] for i in getattr(bm, "boundary", range(n))}
    kind = _orient(out_v, tris, bset)
    # drop vertices nothing uses
    used = {}
    vout = array.array("f")
    for t in tris:
        for k in range(3):
            v = t[k]
            if v not in used:
                used[v] = len(used)
                p = out_v[v]
                vout.extend((p[0] * 1000.0, p[1] * 1000.0, p[2] * 1000.0))
            t[k] = used[v]
    palette = {}
    tri_col = array.array("i")
    flat = array.array("i")
    default = body_rgb or DEFAULT_RGB
    for a, b, c, f in tris:
        rgb = bm.face_colour.get(f) or default
        key = tuple(round(x, 4) for x in rgb)
        tri_col.append(palette.setdefault(key, len(palette)))
        flat.extend((a, b, c))
    colours = [list(k) for k, _ in sorted(palette.items(), key=lambda kv: kv[1])]
    return vout, flat, tri_col, colours, kind


def _split_t_junctions(tris, splits):
    """Faces add points along edge segments where their seams cross them.
    Give every triangle edge lying on such a segment all the points between
    its ends, so neighbouring faces share every vertex along the edge."""
    if not splits:
        return tris
    chains = {}            # segment -> ordered vertices a .. b
    chains_f = {}
    member = {}            # vertex -> [(segment, position in chain)]
    for (a, b), pts in splits.items():
        key = (a, b) if (b, a) not in chains else (b, a)
        if key == (b, a):
            pts = [(1.0 - f, g) for f, g in pts]
            merged = {g: f for f, g in pts}
            for i, g in enumerate(chains[key][1:-1]):
                merged.setdefault(g, chains_f[key][i])
            items = sorted((f, g) for g, f in merged.items())
        else:
            items = sorted({g: f for f, g in pts}.items(), key=lambda x: x[1])
            items = [(f, g) for g, f in items]
        chains[key] = [key[0]] + [g for _, g in items] + [key[1]]
        chains_f[key] = [f for f, _ in items]
    for key, seq in chains.items():
        for i, g in enumerate(seq):
            member.setdefault(g, []).append((key, i))
    out = []
    for a, b, c, f in tris:
        tri = (a, b, c)
        done = False
        for k in range(3):
            u, v, w = tri[k], tri[(k + 1) % 3], tri[(k + 2) % 3]
            mu, mv = member.get(u), member.get(v)
            if not mu or not mv:
                continue
            between = None
            for key, i in mu:
                for key2, j in mv:
                    if key2 == key and abs(i - j) > 1:
                        seq = chains[key]
                        between = seq[i + 1:j] if i < j else seq[j + 1:i][::-1]
                        break
                if between:
                    break
            if between:
                seq = [u] + between + [v]
                for p, q in zip(seq, seq[1:]):
                    out.append((p, q, w, f))
                done = True
                break
        if not done:
            out.append((a, b, c, f))
    return out


def _orient(verts, tris, boundary):
    """Flip whole faces so neighbouring faces agree, then turn each closed
    piece outward. Returns 'solid', 'open' or 'sheet'. Only edges between two
    boundary vertices can join different faces, so only those are looked at."""
    owners = {}
    for t in tris:
        a, b, c, f = t
        ia, ib, ic = a in boundary, b in boundary, c in boundary
        if ia and ib:
            owners.setdefault((a, b), []).append(f)
        if ib and ic:
            owners.setdefault((b, c), []).append(f)
        if ic and ia:
            owners.setdefault((c, a), []).append(f)
    # relation between face pairs: +1 vote consistent, -1 vote inconsistent
    votes = {}
    open_edges = 0
    for (u, v), same in owners.items():
        back = owners.get((v, u))
        if not back:
            if len(same) == 1 and (v, u) not in owners:
                open_edges += 1
            back = ()
        for f in same:
            for g in back:
                if f != g:
                    k = (min(f, g), max(f, g))
                    votes[k] = votes.get(k, 0) + 1
            for g in same:
                if f < g:
                    votes[(f, g)] = votes.get((f, g), 0) - 1
    nbrs = {}
    for (f, g), w in votes.items():
        if w:
            nbrs.setdefault(f, []).append((g, w < 0))
            nbrs.setdefault(g, []).append((f, w < 0))
    faces = sorted({t[3] for t in tris})
    flip = {}
    comps = []
    for f0 in faces:
        if f0 in flip:
            continue
        flip[f0] = False
        comp = [f0]
        stack = [f0]
        while stack:
            f = stack.pop()
            for g, inv in nbrs.get(f, ()):
                if g not in flip:
                    flip[g] = flip[f] ^ inv
                    comp.append(g)
                    stack.append(g)
        comps.append(set(comp))
    # outward: signed volume of each connected piece
    vol = {}
    comp_of = {}
    for i, c in enumerate(comps):
        for f in c:
            comp_of[f] = i
    for t in tris:
        a, b, c, f = t
        if flip[f]:
            t[1], t[2] = t[2], t[1]
            a, b, c = t[0], t[1], t[2]
        p, q, r = verts[a], verts[b], verts[c]
        v6 = (p[0] * (q[1] * r[2] - q[2] * r[1]) - p[1] * (q[0] * r[2] - q[2] * r[0])
              + p[2] * (q[0] * r[1] - q[1] * r[0]))
        i = comp_of[f]
        vol[i] = vol.get(i, 0.0) + v6
    flip_comp = {i for i, v in vol.items() if v < 0}
    if flip_comp:
        for t in tris:
            if comp_of[t[3]] in flip_comp:
                t[1], t[2] = t[2], t[1]
    n_edges = max(len(owners), 1)
    if open_edges == 0:
        return "solid"
    return "open" if open_edges < 0.02 * n_edges else "sheet"


# ------------------------------------------------------------ duplicates
def _summary(b):
    v = b.verts
    xs, ys, zs = v[0::3], v[1::3], v[2::3]
    box = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    area = 0.0
    t = b.tris
    for i in range(0, len(t), 3):
        a, c, d = t[i] * 3, t[i + 1] * 3, t[i + 2] * 3
        ux, uy, uz = v[c] - v[a], v[c + 1] - v[a + 1], v[c + 2] - v[a + 2]
        wx, wy, wz = v[d] - v[a], v[d + 1] - v[a + 1], v[d + 2] - v[a + 2]
        area += ((uy * wz - uz * wy) ** 2 + (uz * wx - ux * wz) ** 2 + (ux * wy - uy * wx) ** 2) ** 0.5
    return box, area * 0.5


def _painted(b):
    default = tuple(round(x, 4) for x in DEFAULT_RGB)
    return sum(1 for c in b.tri_colour if tuple(round(x, 4) for x in b.colours[c]) != default)


def _drop_duplicates(bodies, log):
    """Remove bodies lying exactly on top of an earlier one (both would be
    drawn in the same place and flicker), keeping the more painted copy."""
    info = [_summary(b) for b in bodies]
    keep = []
    for i, b in enumerate(bodies):
        box, area = info[i]
        size = max(box[3] - box[0], box[4] - box[1], box[5] - box[2], 1e-9)
        dup = None
        for j in keep:
            box2, area2 = info[j]
            if (all(abs(x - y) <= 1e-4 * size for x, y in zip(box, box2))
                    and abs(area - area2) <= 1e-3 * max(area, 1e-12)):
                dup = j
                break
        if dup is None:
            keep.append(i)
            continue
        log.warn("dropped a duplicate body lying exactly on top of another")
        if _painted(b) > _painted(bodies[dup]):
            keep[keep.index(dup)] = i
    return [bodies[i] for i in keep]
