# SolidWorks Import for Blender

Import SolidWorks files directly into Blender, without SolidWorks or any
converter:

* **Parts** (`.SLDPRT`, SolidWorks 2011 and later)
* **Assemblies** (`.SLDASM`), with every component in its SolidWorks position
* **Parasolid** files (`.x_t`, `.x_b`)

They appear under **File → Import → SolidWorks (.sldprt, .sldasm)**, and
**File → Import → SolidWorks Folder** imports a whole folder at once. Each body
becomes a mesh object with its SolidWorks colours. Works with Blender 2.79 and
later (tested with 2.79 and 4.3).

## What you need

* **Blender** 2.79 or later.
* **FreeCAD** 0.21 or later (free, from [freecad.org](https://www.freecad.org/)).
  Blender cannot rebuild SolidWorks surfaces by itself, so the add-on runs
  FreeCAD in the background, without a window, to turn each part into a mesh.
  FreeCAD is found automatically (`freecadcmd` on the PATH, the FreeCAD
  AppImage in `/opt` or your home folder, or the usual Windows and macOS
  install folders). Otherwise set its path in the add-on preferences.

## Support the add-on

A SolidWorks licence costs around **3500 euro a year**. This add-on opens your
SolidWorks files in free Blender, which makes it excellent value.

This is very useful for getting solidworks files from [**GrabCAD**](https://www.grabcad.com/) and similar.


If you like the add-on, please support it by paying **10 euro** via
[**PayPal**](https://www.paypal.com/) to **barrow_dj@yahoo.com**.

On [paypal.com](https://www.paypal.com/), choose **Send**, enter
`barrow_dj@yahoo.com`, and send 10 EUR.

## Install

### From a zip (any computer)

    python3 install.py --zip

builds `io_import_solidworks.zip`. In Blender open **Edit → Preferences →
Add-ons → Install** (Blender 2.79: **File → User Preferences → Add-ons →
Install Add-on from File**), choose the zip, and tick
**Import-Export: SolidWorks Import**.

### With the install script

    ./install.sh                         # Linux / macOS, uses the blender on the PATH
    py install.py                        # Windows
    ./install.sh --blender PATH          # use this Blender (program or launcher script)
    ./install.sh --link                  # symlink to this checkout (for development)
    ./install.sh --uninstall
    ./install.sh --dest DIR              # copy into this add-ons folder only

The script asks Blender where its add-ons folder is, copies the add-on there
and switches it on.

## Import options

* **Scale**: size of 1 mm in Blender units. The default 0.001 gives metres,
  Blender's own unit.
* **Mesh Detail**: Coarse, Normal or Fine. Fine follows curved surfaces more
  closely and makes more triangles.
* **Smooth Shading**: curved faces are smooth, edges sharper than 30° stay sharp.
* **Forward / Up**: SolidWorks uses Y up and Blender Z up; the defaults turn
  the model the right way up.
* **Parts Folder**: an extra folder to look in for the parts of an assembly.

## Assemblies

An assembly becomes an empty named after the file, with one object per
component, named as in the SolidWorks tree (for example `crank-1`).
Sub-assemblies become empties of their own. Components hidden in SolidWorks
are hidden; suppressed components are left out. Every copy of a part shares
one mesh, so large assemblies stay light.

SolidWorks stores the full Windows path of every part used by an assembly. The
add-on looks for each part next to the assembly, in folders below it, and in
the **Parts Folder** option. Components whose files are not found are left
out, and their names are listed.

## Folders

**File → Import → SolidWorks Folder** imports every `.SLDASM` and `.SLDPRT` in
a folder, side by side along X:

* Every assembly that no other assembly in the folder uses, with all its
  components.
* Every part that none of those assemblies use.

Parts and sub-assemblies that an assembly already contains are not imported a
second time. Tick **Include Subfolders** to also import files in folders
below. Problems (unreadable files, missing parts) are listed at the end
instead of stopping the import.

## Speed

Each part is converted once and the mesh is kept in a cache, so importing the
same file again (or an assembly whose parts were imported before) takes well
under a second and does not start FreeCAD at all. The cache is keyed by the
file's contents and the mesh detail, so a changed file is always converted
afresh. It lives in `~/.cache/blender-solidworks` (Windows:
`%LOCALAPPDATA%\blender-solidworks`, macOS: `~/Library/Caches/blender-solidworks`)
and is safe to delete; the add-on preferences have a **Clear Mesh Cache** button.

The first import of a session waits for FreeCAD to start, which can take
10–20 seconds for the FreeCAD AppImage.

## Limitations

* SolidWorks files saved before 2011 use an older format that is not supported.
  Open and re-save them in a newer SolidWorks.
* Only the geometry is imported, as meshes: not the feature history, sketches
  or dimensions. Only each part's default configuration is used.
* Some complex surfaces may not rebuild exactly. Problems are reported as
  warnings in Blender's info bar and in the console.

## Command line

`io_import_solidworks/sw2fc/cli.py` converts files with FreeCAD without opening
Blender:

    python3 io_import_solidworks/sw2fc/cli.py part.SLDPRT -o part.step

Output formats: `.FCStd`, `.step`, `.iges`, `.brep`, `.stl`. Run with `--help`
for all options.

## How it works

* `io_import_solidworks/__init__.py`: the Blender menu entries, options and
  preferences.
* `io_import_solidworks/importer.py`: builds Blender meshes, materials and the
  assembly hierarchy (Blender 2.79 and 2.80+).
* `io_import_solidworks/runner.py`: finds FreeCAD, runs it and keeps the mesh
  cache.
* `io_import_solidworks/sw2fc/`: reads the SolidWorks files (pure Python) and,
  inside FreeCAD, rebuilds the solids (`convert.py`) and meshes them
  (`mesh.py`).

## Author and licence

Written by Denis Joseph Barrow (barrow_dj@yahoo.com) with the help of
[Claude Code](https://claude.com/claude-code).

LGPL-2.1-or-later. See [LICENSE](LICENSE).

SolidWorks and Parasolid are trademarks of Dassault Systèmes and Siemens
respectively. This add-on is an independent project, not affiliated with or
endorsed by either company.
