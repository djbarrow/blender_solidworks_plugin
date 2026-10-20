# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Get triangle meshes for SolidWorks parts. Pure Python (no bpy).

Rebuilding the geometry needs OpenCASCADE, so parts are meshed by headless
FreeCAD (sw2fc/_mesh_entry.py). Results are cached by file contents, so a part
that was imported before does not start FreeCAD again.
"""

import array
import glob
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MESH_ENTRY = os.path.join(HERE, "sw2fc", "_mesh_entry.py")
MAX_CACHE_ENTRIES = 500


class MeshError(RuntimeError):
    pass


# ------------------------------------------------------------ FreeCAD
def _candidates():
    w = shutil.which
    out = [os.environ.get("SW2FC_FREECAD"),
           w("freecadcmd"), w("FreeCADCmd"), w("freecad.cmd"),
           "/opt/FreeCAD.AppImage"]
    out += sorted(glob.glob(os.path.expanduser("~/Applications/FreeCAD*.AppImage")), reverse=True)
    out += sorted(glob.glob(os.path.expanduser("~/FreeCAD*.AppImage")), reverse=True)
    if platform.system() == "Windows":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            if root:
                out += sorted(glob.glob(os.path.join(root, "FreeCAD*", "bin", "FreeCADCmd.exe")),
                              reverse=True)
                out += sorted(glob.glob(os.path.join(root, "Programs", "FreeCAD*", "bin",
                                                     "FreeCADCmd.exe")), reverse=True)
    if platform.system() == "Darwin":
        out += ["/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
                "/Applications/FreeCAD.app/Contents/MacOS/FreeCADCmd"]
    out += [w("FreeCAD"), w("freecad")]
    return out


def find_freecad(explicit=None):
    """Path of a FreeCAD that can run scripts without a window, or None."""
    for c in ([explicit] if explicit else []) + _candidates():
        if c and os.path.isfile(c):
            return c
    return None


def freecad_command(exe):
    """Command line that starts FreeCAD without a window."""
    real = os.path.realpath(exe)
    low = os.path.basename(real).lower()
    if low.endswith(".appimage"):
        return [exe, "freecadcmd"]            # the AppImage picks the program from argv[1]
    if low in ("freecad", "freecad.exe"):
        d = os.path.dirname(real)
        for n in ("freecadcmd", "FreeCADCmd", "FreeCADCmd.exe", "freecadcmd.exe"):
            if os.path.isfile(os.path.join(d, n)):
                return [os.path.join(d, n)]
        return [exe, "-c"]                     # console mode of the GUI program
    return [exe]


# ------------------------------------------------------------ cache
def cache_dir():
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "blender-solidworks")


_code_hash = None


def _code_fingerprint():
    """Changes whenever the converter changes, so old meshes are not reused."""
    global _code_hash
    if _code_hash is None:
        h = hashlib.sha1()
        for f in sorted(glob.glob(os.path.join(HERE, "sw2fc", "*.py"))):
            with open(f, "rb") as fh:
                h.update(fh.read())
        _code_hash = h.hexdigest()[:16]
    return _code_hash


def cache_key(path, detail):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    h.update(_code_fingerprint().encode())
    h.update(("detail=%.6g" % detail).encode())
    return h.hexdigest()


def _prune(root):
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for n in names:                         # leftovers of an interrupted run
        p = os.path.join(root, n)
        if n.startswith(".tmp") and now - os.path.getmtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)
    entries = [os.path.join(root, n) for n in names if not n.startswith(".")]
    if len(entries) > MAX_CACHE_ENTRIES:
        entries.sort(key=os.path.getmtime)
        for p in entries[:len(entries) - MAX_CACHE_ENTRIES]:
            shutil.rmtree(p, ignore_errors=True)


def clear_cache():
    shutil.rmtree(cache_dir(), ignore_errors=True)


# ------------------------------------------------------------ results
class Body:
    def __init__(self, label, verts, tris, tri_colour, colours, transparency):
        self.label = label
        self.verts = verts              # array('f'), xyz in mm
        self.tris = tris                # array('i'), 3 vertex indices per triangle
        self.tri_colour = tri_colour    # array('i'), index into colours per triangle
        self.colours = colours          # [[r, g, b], ...] 0..1
        self.transparency = transparency


class PartMesh:
    def __init__(self, path, bodies, warnings, error):
        self.path = path
        self.bodies = bodies
        self.warnings = warnings
        self.error = error


def read_result(folder, path):
    with open(os.path.join(folder, "meta.json")) as fh:
        meta = json.load(fh)
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
            bodies.append(Body(b["label"], arrs[0], arrs[1], arrs[2], b["colors"],
                               b.get("transparency", 0.0)))
    return PartMesh(path, bodies, meta.get("warnings", []), meta.get("error"))


def mesh_parts(paths, detail=0.0005, freecad=None, progress=None, log=print, own_dir=None):
    """{path: PartMesh} for the given part files.

    Parts not in the cache are meshed by one FreeCAD run. `progress(done, total)`
    is called as parts finish. Raises MeshError if FreeCAD is needed but missing
    or fails to start. `own_dir` is the host program's install folder: its
    library folders are taken out of LD_LIBRARY_PATH before FreeCAD starts."""
    root = cache_dir()
    os.makedirs(root, exist_ok=True)
    folders = {}
    todo = []
    for p in dict.fromkeys(paths):
        d = os.path.join(root, cache_key(p, detail))
        folders[p] = d
        if not os.path.isfile(os.path.join(d, "meta.json")):
            todo.append(p)
        else:
            try:
                os.utime(d)                   # recently used entries survive pruning
            except OSError:
                pass
    if todo:
        _run_freecad([{"path": os.path.abspath(p), "out": folders[p]} for p in todo],
                     detail, freecad, progress, log, own_dir)
    out = {}
    for p, d in folders.items():
        try:
            out[p] = read_result(d, p)
        except (OSError, ValueError, KeyError) as exc:
            out[p] = PartMesh(p, [], [], "could not read the mesh (%s)" % exc)
    _prune(root)
    return out


def _clean_env(own_dir):
    env = dict(os.environ)
    # Blender sets these for its own Python and libraries; they would break FreeCAD's
    for k in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    if own_dir:
        own = os.path.realpath(own_dir) + os.sep
        for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            if k in env:
                keep = [d for d in env[k].split(os.pathsep)
                        if d and not (os.path.realpath(d) + os.sep).startswith(own)]
                if keep:
                    env[k] = os.pathsep.join(keep)
                else:
                    del env[k]
    return env


def _run_freecad(items, detail, freecad, progress, log, own_dir=None):
    exe = find_freecad(freecad)
    if not exe:
        raise MeshError("FreeCAD was not found. It is needed to rebuild SolidWorks geometry: "
                        "install FreeCAD 0.21 or newer, or set its path in the add-on preferences.")
    fd, job = tempfile.mkstemp(prefix="sw2blender-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"parts": items, "detail": detail}, fh)
    env = _clean_env(own_dir)
    env["SW2FC_JOB"] = job
    cmd = freecad_command(exe) + [MESH_ENTRY]
    log("starting FreeCAD to mesh %d part(s): %s" % (len(items), " ".join(cmd)))
    tail = []
    rc = None
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, errors="replace")
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("SW2FC_PROGRESS"):
                f = line.split(" ", 3)
                if progress:
                    progress(int(f[1]), int(f[2]))
                if len(f) > 3:
                    log("meshing %s (%d/%d)" % (f[3], int(f[1]) + 1, int(f[2])))
            elif line.startswith("SW2FC_RC="):
                rc = int(line.split("=", 1)[1])
            elif line.startswith("SW2FC_DONE"):
                log(line[len("SW2FC_DONE "):])
            elif line:
                tail = (tail + [line])[-20:]
        proc.wait()
    except OSError as exc:
        raise MeshError("could not start FreeCAD (%s): %s" % (exe, exc))
    finally:
        try:
            os.remove(job)
        except OSError:
            pass
    if rc != 0:
        raise MeshError("FreeCAD failed while meshing the parts:\n" + "\n".join(tail[-10:]))
