#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.
"""Install the SolidWorks import add-on into Blender.

    python3 install.py                      # install into the blender on PATH and enable it
    python3 install.py --blender PATH       # use this Blender program (or its launcher script)
    python3 install.py --link               # symlink instead of copying (for development)
    python3 install.py --uninstall          # remove it
    python3 install.py --dest DIR           # copy into this addons folder; do not run Blender
    python3 install.py --zip                # only build io_import_solidworks.zip, for
                                            #   Edit > Preferences > Add-ons > Install

Works with Blender 2.79 and later; nothing else needs to be installed.
Blender 4.2 and later can also take the add-on as an extension:

    python3 install.py --extension          # build solidworks_import-<version>.zip for
                                            #   Edit > Preferences > Get Extensions >
                                            #   Install from Disk
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = "io_import_solidworks"
ADDON_SRC = os.path.join(HERE, ADDON)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
REPO = "https://github.com/djbarrow/blender_solidworks_plugin"


def fetch_addon():
    """install.py was copied on its own: download the add-on folder from GitHub
    into a temporary folder and install from there."""
    global ADDON_SRC
    import atexit
    import io
    import tempfile
    import urllib.request
    print("%s is missing next to install.py; downloading it from %s" % (ADDON, REPO))
    url = REPO + "/archive/refs/heads/main.zip"
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    tmp = tempfile.mkdtemp(prefix="sw_install_")
    atexit.register(shutil.rmtree, tmp, True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    for top in os.listdir(tmp):
        cand = os.path.join(tmp, top, ADDON)
        if os.path.isfile(os.path.join(cand, "__init__.py")):
            ADDON_SRC = cand
            return
    raise RuntimeError("the download has no %s folder" % ADDON)


def build_zip(out=None):
    out = out or os.path.join(HERE, ADDON + ".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(ADDON_SRC):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for n in sorted(fns):
                if n.endswith(".pyc"):
                    continue
                p = os.path.join(dp, n)
                z.write(p, os.path.relpath(p, HERE))
    return out


def run_blender(blender, code):
    """Run Python inside Blender without a window; return its output."""
    try:
        return subprocess.run([blender, "-b", "--python-expr", code],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, errors="replace", timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "ERROR %s" % exc


def ask_addons_dir(blender):
    code = ("import bpy, os\n"
            "p = bpy.utils.user_resource('SCRIPTS', path='addons', create=True) "
            "if bpy.app.version >= (2, 80, 0) else "
            "bpy.utils.user_resource('SCRIPTS', 'addons', create=True)\n"
            "print('SW_ADDONS=' + p)\n"
            "print('SW_VERSION=' + bpy.app.version_string)\n")
    out = run_blender(blender, code)
    found = {}
    for line in out.splitlines():
        if line.startswith("SW_"):
            k, v = line.split("=", 1)
            found[k] = v.strip()
    if "SW_ADDONS" not in found:
        raise SystemExit("FAILED  could not ask Blender for its add-ons folder:\n" + out[-2000:])
    return found["SW_ADDONS"], found.get("SW_VERSION", "?")


def _version_key(name):
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return ()


def guess_addons_dir():
    """Without a Blender to ask: the add-ons folder of the newest Blender
    version that has a user folder, e.g. ~/.config/blender/4.3/scripts/addons."""
    if sys.platform.startswith("win"):
        root = os.path.join(os.environ.get("APPDATA", ""), "Blender Foundation", "Blender")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support/Blender")
    else:
        root = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "blender")
    try:
        versions = [n for n in os.listdir(root) if _version_key(n)]
    except OSError:
        return None, None
    if not versions:
        return None, None
    v = max(versions, key=_version_key)
    return os.path.join(root, v, "scripts", "addons"), v


def enable_and_check(blender):
    """Enable the add-on in Blender's saved preferences and check it loads."""
    code = ("import bpy, addon_utils\n"
            "addon_utils.enable('%s', default_set=True, persistent=True)\n"
            "ok = hasattr(bpy.ops.import_scene, 'solidworks')\n"
            "bpy.ops.wm.save_userpref()\n"
            "print('SW_OK' if ok else 'SW_MISSING')\n" % ADDON)
    try:
        out = subprocess.run([blender, "-b", "--python-expr", code], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True,
                             errors="replace", timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return "SW_OK" in out, out


def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def build_extension(out_dir=None):
    """Zip in the Blender 4.2+ extension layout: the add-on's files at the top."""
    import re
    with open(os.path.join(ADDON_SRC, "blender_manifest.toml")) as fh:
        text = fh.read()
    ident = re.search(r'^id\s*=\s*"([^"]+)"', text, re.M).group(1)
    version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    out = os.path.join(out_dir or HERE, "%s-%s.zip" % (ident, version))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(ADDON_SRC):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for n in sorted(fns):
                if n.endswith(".pyc"):
                    continue
                p = os.path.join(dp, n)
                z.write(p, os.path.relpath(p, ADDON_SRC))
    return out


def main():
    p = argparse.ArgumentParser(description="Install the SolidWorks import add-on into Blender.")
    p.add_argument("--blender", default=shutil.which("blender"),
                   help="Blender program or launcher script (default: blender on PATH)")
    p.add_argument("--dest", help="Blender add-ons folder (default: ask Blender)")
    p.add_argument("--link", action="store_true",
                   help="symlink to this checkout instead of copying (for development)")
    p.add_argument("--uninstall", action="store_true", help="remove the add-on")
    p.add_argument("--zip", action="store_true",
                   help="only build %s.zip for Blender's 'Install add-on' button" % ADDON)
    p.add_argument("--extension", action="store_true",
                   help="only build the Blender 4.2+ extension zip")
    args = p.parse_args()

    if not os.path.isfile(os.path.join(ADDON_SRC, "__init__.py")) and not args.uninstall:
        if args.link:
            print("FAILED  --link needs a full checkout: git clone %s" % REPO, file=sys.stderr)
            return 1
        try:
            fetch_addon()
        except Exception as exc:
            print("FAILED  %s is missing next to install.py and could not be downloaded "
                  "(%s).\nDownload the whole project from %s and run install.py from it."
                  % (ADDON, exc, REPO), file=sys.stderr)
            return 1

    if args.extension:
        print("Built %s" % build_extension())
        print("In Blender 4.2+: Edit > Preferences > Get Extensions > (menu) Install from "
              "Disk, and pick the zip.")
        return 0

    if args.zip:
        print("Built %s" % build_zip())
        print("In Blender: Edit > Preferences > Add-ons > Install (2.79: File > User "
              "Preferences > Add-ons > Install Add-on from File), pick the zip, tick "
              "'Import-Export: SolidWorks Import'.")
        return 0

    blender = args.blender
    if args.dest:
        dest = os.path.abspath(os.path.expanduser(args.dest))
        version = None
    else:
        dest = version = None
        if blender:
            try:
                dest, version = ask_addons_dir(blender)
            except SystemExit as exc:
                print(exc, file=sys.stderr)
                blender = None
        if not dest:
            dest, version = guess_addons_dir()
            if not dest:
                print("FAILED  Blender not found; pass --blender PATH or --dest DIR",
                      file=sys.stderr)
                return 1
            blender = None          # cannot switch it on for you; say how instead
            print("Using Blender's user folder %s" % dest)
    target = os.path.join(dest, ADDON)

    if args.uninstall:
        if os.path.lexists(target):
            remove(target)
            print("Removed %s" % target)
        else:
            print("Not installed in %s" % dest)
        return 0

    try:
        os.makedirs(dest, exist_ok=True)
        remove(target)
        if args.link:
            os.symlink(ADDON_SRC, target)
        else:
            shutil.copytree(ADDON_SRC, target, ignore=IGNORE)
    except OSError as exc:
        print("FAILED  could not install into %s: %s" % (dest, exc), file=sys.stderr)
        return 1
    print("Installed %s%s%s" % (target, " (link)" if args.link else "",
                                " for Blender %s" % version if version else ""))

    if blender and not args.dest:
        ok, out = enable_and_check(blender)
        if ok:
            print("Enabled  the add-on loads in Blender and is switched on")
        else:
            print("WARNING  Blender could not load the add-on:\n" + out[-3000:], file=sys.stderr)
            return 1
    else:
        print("Enable it in Blender's Preferences > Add-ons: 'Import-Export: SolidWorks Import'.")

    print("Then use File > Import > SolidWorks (.sldprt, .sldasm).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
