# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Meshes for SolidWorks parts, kept on disk so a part is only meshed once.
Pure Python (no bpy).

Entries are keyed by the file's contents, the mesh detail and a fingerprint of
the mesher's source, so a changed file or a newer add-on never returns stale
meshes. Each entry is a folder with meta.json and mesh.bin (per body: float32
xyz in mm, int32 triangle corners, int32 colour index per triangle).
"""

import array
import glob
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time

from .sw2blender import mesh as _mesh
from .sw2blender.loader import ConversionError

HERE = os.path.dirname(os.path.abspath(__file__))
FORMAT = 2
MAX_ENTRIES = 500


class PartMesh:
    def __init__(self, path, bodies, warnings, error):
        self.path = path
        self.bodies = bodies            # list of sw2blender.mesh.Body
        self.warnings = warnings
        self.error = error


def cache_dir():
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "blender-solidworks")


def clear_cache():
    shutil.rmtree(cache_dir(), ignore_errors=True)


_code_hash = None


def _code_fingerprint():
    global _code_hash
    if _code_hash is None:
        h = hashlib.sha1()
        for f in sorted(glob.glob(os.path.join(HERE, "sw2blender", "*.py"))):
            with open(f, "rb") as fh:
                h.update(fh.read())
        _code_hash = h.hexdigest()[:16]
    return _code_hash


def _key(path, detail):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    h.update(("%s|%d|%.6g" % (_code_fingerprint(), FORMAT, detail)).encode())
    return h.hexdigest()


def _write(folder, pm):
    parent = os.path.dirname(folder)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=parent, prefix=".tmp")
    meta = {"format": FORMAT, "error": pm.error, "warnings": pm.warnings, "bodies": []}
    with open(os.path.join(tmp, "mesh.bin"), "wb") as fh:
        for b in pm.bodies:
            meta["bodies"].append({"label": b.label, "nverts": len(b.verts) // 3,
                                   "ntris": len(b.tris) // 3, "colors": b.colours,
                                   "transparency": b.transparency, "kind": b.kind})
            for a in (b.verts, b.tris, b.tri_colour):
                if sys.byteorder != "little":
                    a = array.array(a.typecode, a)
                    a.byteswap()
                a.tofile(fh)
    with open(os.path.join(tmp, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    if os.path.isdir(folder):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        os.replace(tmp, folder)


def _read(folder, path):
    with open(os.path.join(folder, "meta.json")) as fh:
        meta = json.load(fh)
    if meta.get("format") != FORMAT:
        raise ValueError("old cache format")
    bodies = []
    with open(os.path.join(folder, "mesh.bin"), "rb") as fh:
        for b in meta["bodies"]:
            arrs = []
            for code, n in (("f", 3 * b["nverts"]), ("i", 3 * b["ntris"]), ("i", b["ntris"])):
                a = array.array(code)
                a.fromfile(fh, n)
                if sys.byteorder != "little":
                    a.byteswap()
                arrs.append(a)
            bodies.append(_mesh.Body(b["label"], arrs[0], arrs[1], arrs[2], b["colors"],
                                     b.get("transparency", 0.0), b.get("kind", "solid")))
    return PartMesh(path, bodies, meta.get("warnings", []), meta.get("error"))


def mesh_parts(paths, detail=0.0005, progress=None, log=print, use_cache=True):
    """{path: PartMesh} for part files, from the cache where possible.
    `progress(done, total)` is called as parts finish."""
    root = cache_dir()
    out = {}
    todo = list(dict.fromkeys(paths))
    for i, p in enumerate(todo):
        if progress:
            progress(i, len(todo))
        folder = None
        if use_cache:
            try:
                folder = os.path.join(root, _key(p, detail))
                if os.path.isfile(os.path.join(folder, "meta.json")):
                    out[p] = _read(folder, p)
                    os.utime(folder)            # recently used entries survive pruning
                    continue
            except (OSError, ValueError, KeyError):
                pass
        t0 = time.time()
        log("meshing %s (%d/%d)" % (os.path.basename(p), i + 1, len(todo)))
        try:
            bodies, mlog = _mesh.mesh_part(p, detail=detail)
            pm = PartMesh(p, bodies, list(mlog.warnings), None)
        except ConversionError as exc:
            pm = PartMesh(p, [], [], str(exc))
        except Exception as exc:          # a broken file must not stop an assembly
            pm = PartMesh(p, [], [], "%s: %s" % (exc.__class__.__name__, exc))
        log("  %s in %.1f s" % ("failed" if pm.error else "done", time.time() - t0))
        out[p] = pm
        if folder is not None:
            try:
                _write(folder, pm)
            except OSError as exc:
                log("could not write the mesh cache: %s" % exc)
    if progress:
        progress(len(todo), len(todo))
    _prune(root)
    return out


def _prune(root):
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for n in names:                         # leftovers of an interrupted write
        p = os.path.join(root, n)
        if n.startswith(".tmp") and now - os.path.getmtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)
    entries = [os.path.join(root, n) for n in names if not n.startswith(".")]
    if len(entries) > MAX_ENTRIES:
        entries.sort(key=os.path.getmtime)
        for p in entries[:len(entries) - MAX_ENTRIES]:
            shutil.rmtree(p, ignore_errors=True)
