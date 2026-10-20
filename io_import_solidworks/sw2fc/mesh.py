# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Turn SolidWorks parts into triangle meshes. Runs inside headless FreeCAD.

Started by the Blender add-on as  freecadcmd sw2fc/_mesh_entry.py  with the path of a
JSON job file in the SW2FC_JOB environment variable:

    {"parts": [{"path": "a.SLDPRT", "out": "/cache/abc123"}, ...],
     "detail": 0.0005}

For each part, `out` becomes a folder holding (see MESH_FORMAT):

    meta.json  {"format": 1, "bodies": [{"label", "nverts", "ntris",
                "colors": [[r, g, b], ...], "transparency"}], "warnings": [...],
                "error": null}
    mesh.bin   per body, in order: float32 xyz * nverts (mm),
               int32 vertex indices * 3 * ntris, int32 colour index * ntris

The folder is written under a temporary name and renamed when complete, so a
reader never sees half a result. Progress lines "SW2FC_PROGRESS i n name" go
to stdout.
"""

import array
import json
import os
import shutil
import sys
import tempfile

MESH_FORMAT = 1

# SolidWorks' default part colour, used for bodies that carry no colour
DEFAULT_RGB = (202 / 255.0, 209 / 255.0, 238 / 255.0)

CACHE_OPTIONS = {"sew_tolerance": 0.01, "keep_duplicates": False}


def _convert(path):
    """[(label, shape, appearance)], warnings for a part file, from the BREP
    cache when this exact file was converted before."""
    from . import cache as _cache
    from .convert import convert
    hit = _cache.load(path, CACHE_OPTIONS)
    if hit is not None:
        bodies, warnings, st = hit
    else:
        results, log = convert(path, verbose=False, **CACHE_OPTIONS)
        apps = list(getattr(log, "appearances", []))
        apps += [None] * (len(results) - len(apps))
        bodies = [(label, shape, kind, app)
                  for (label, shape, kind), app in zip(results, apps)]
        warnings, st = log.warnings, getattr(log, "stats", {})
        _cache.store(path, CACHE_OPTIONS, bodies, warnings, st)
    warnings = list(warnings)
    if st.get("failed_faces"):
        warnings.append("%d of %d faces could not be rebuilt" % (st["failed_faces"], st["faces"]))
    return [(label, shape, app) for label, shape, _, app in bodies], warnings


def _face_colours(shape, app):
    """One rgb per face of `shape`."""
    n = len(shape.Faces)
    cols = (app or {}).get("faces") or []
    if len(cols) == n:
        return [tuple(c[:3]) for c in cols]
    body = (app or {}).get("color") or (cols[0] if cols else None) or DEFAULT_RGB
    return [tuple(body[:3])] * n


def tessellate(shape, app, detail):
    """(verts, tris, tri_colour, colours) for one body.

    `detail` is the largest allowed distance between mesh and surface, as a
    fraction of the body's size. Vertices shared by neighbouring faces are
    merged, so the mesh is connected and can be smooth shaded."""
    diag = shape.BoundBox.DiagonalLength if shape.BoundBox.isValid() else 1.0
    deflection = max(diag * detail, 1e-4)
    try:
        shape.tessellate(deflection)          # mesh every face in one go, edges shared
    except Exception:
        pass
    grid = max(diag * 1e-7, 1e-9)
    index = {}
    verts = array.array("f")
    tris = array.array("i")
    tri_col = array.array("i")
    palette = {}
    for face, rgb in zip(shape.Faces, _face_colours(shape, app)):
        try:
            pts, faces = face.tessellate(deflection)
        except Exception:
            continue
        if not faces:
            continue
        key = tuple(round(c, 4) for c in rgb)
        ci = palette.setdefault(key, len(palette))
        remap = []
        for p in pts:
            k = (round(p.x / grid), round(p.y / grid), round(p.z / grid))
            vi = index.get(k)
            if vi is None:
                vi = index[k] = len(index)
                verts.extend((p.x, p.y, p.z))
            remap.append(vi)
        for a, b, c in faces:
            a, b, c = remap[a], remap[b], remap[c]
            if a == b or b == c or a == c:
                continue                     # collapsed by the merge
            tris.extend((a, b, c))
            tri_col.append(ci)
    colours = [list(k) for k, _ in sorted(palette.items(), key=lambda kv: kv[1])]
    return verts, tris, tri_col, colours


def write_part(path, out, detail):
    """Convert and mesh one part into the folder `out`."""
    meta = {"format": MESH_FORMAT, "source": path, "bodies": [], "warnings": [], "error": None}
    chunks = []
    try:
        bodies, warnings = _convert(path)
        meta["warnings"] = warnings
        for label, shape, app in bodies:
            verts, tris, tri_col, colours = tessellate(shape, app, detail)
            if not tris:
                meta["warnings"].append("%s: no triangles (empty body)" % label)
                continue
            meta["bodies"].append({
                "label": label, "nverts": len(verts) // 3, "ntris": len(tris) // 3,
                "colors": colours,
                "transparency": float((app or {}).get("transparency") or 0.0)})
            chunks += [verts, tris, tri_col]
        if not meta["bodies"]:
            meta["error"] = "no bodies could be meshed"
    except Exception as exc:
        meta["error"] = str(exc) or exc.__class__.__name__
        chunks = []
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=parent, prefix=".tmp")
    with open(os.path.join(tmp, "mesh.bin"), "wb") as fh:
        for a in chunks:
            if sys.byteorder != "little":
                a = array.array(a.typecode, a)
                a.byteswap()
            a.tofile(fh)
    with open(os.path.join(tmp, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    os.replace(tmp, out)
    return meta


def main():
    with open(os.environ["SW2FC_JOB"]) as fh:
        job = json.load(fh)
    parts = job["parts"]
    detail = float(job.get("detail", 0.0005))
    for i, item in enumerate(parts):
        print("SW2FC_PROGRESS %d %d %s" % (i, len(parts), os.path.basename(item["path"])),
              flush=True)
        meta = write_part(item["path"], item["out"], detail)
        status = "ERROR " + meta["error"] if meta["error"] else "OK"
        print("SW2FC_DONE %s %s" % (os.path.basename(item["path"]), status), flush=True)
    print("SW2FC_PROGRESS %d %d" % (len(parts), len(parts)), flush=True)

