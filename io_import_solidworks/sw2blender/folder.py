# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Pick which files to open from a folder of SolidWorks files. Pure Python."""

import os

from . import assembly as _asm

FOLDER_EXTS = (".sldprt", ".sldasm")


def _key(path):
    return os.path.normcase(os.path.realpath(path))


def solidworks_files(folder, recursive=False):
    """SolidWorks parts and assemblies in `folder`, sorted, without the
    ~$ lock files SolidWorks leaves while a file is open."""
    out = []
    for dirpath, dirnames, names in os.walk(folder):
        dirnames.sort()
        for n in sorted(names):
            if n.startswith("~$") or os.path.splitext(n)[1].lower() not in FOLDER_EXTS:
                continue
            out.append(os.path.join(dirpath, n))
        if not recursive:
            break
    return out


def has_subfolder_files(folder):
    top = {_key(p) for p in solidworks_files(folder)}
    return any(_key(p) not in top for p in solidworks_files(folder, True))


def _all_components(comp):
    for c in comp.children:
        yield c
        for g in _all_components(c):
            yield g


def plan_folder(folder, recursive=False):
    """(assemblies, parts, skipped) to open for a folder: every assembly no
    other assembly in the folder uses, and every part those assemblies do not
    use (so nothing is opened twice). `skipped` counts the files left out
    because an assembly already contains them."""
    files = solidworks_files(folder, recursive)
    asms = [f for f in files if f.lower().endswith(".sldasm")]
    parts = [f for f in files if not f.lower().endswith(".sldasm")]
    used = set()
    for a in asms:
        try:
            root = _asm.read_tree(a)
        except Exception:
            continue                      # reported when it is opened
        for comp in _all_components(root):
            p = _asm.resolve_file(comp.path, a, [folder])
            if p:
                used.add(_key(p))
    top = [a for a in asms if _key(a) not in used]
    loose = [p for p in parts if _key(p) not in used]
    skipped = len(files) - len(top) - len(loose)
    return top, loose, skipped
