# SolidWorks Import for Blender

Import SolidWorks files directly into Blender, without SolidWorks or any
converter:

* **Parts** (`.SLDPRT`, SolidWorks 2011 and later)
* **Assemblies** (`.SLDASM`), with every component in its SolidWorks position
* **Parasolid** files (`.x_t`, `.x_b`)

They appear under **File → Import → SolidWorks (.sldprt, .sldasm)**, and
**File → Import → SolidWorks Folder** imports a whole folder at once. Each body
becomes a mesh object with its SolidWorks colours. On Blender 4.1 and later
you can also drag SolidWorks files onto the 3D view.

Works with Blender 2.79 and later, and uses only Python API calls that are
still current in Blender 5.x (tested with Blender 2.79 and 4.3 on Linux).
Nothing else needs to be installed: the add-on reads the SolidWorks geometry
and turns it into meshes in pure Python, so it runs wherever Blender runs.

## What you need

Only **Blender** 2.79 or later.

## Support the add-on

A SolidWorks licence costs around **3500 euro a year**. This add-on opens your
SolidWorks files in free Blender, which makes it excellent value.

This is very useful for getting solidworks files from [**GrabCAD**](https://www.grabcad.com/) and similar.


If you like the add-on, please support it by paying **10 euro** via
[**PayPal**](https://www.paypal.com/) to **barrow_dj@yahoo.com**.

On [paypal.com](https://www.paypal.com/), choose **Send**, enter
`barrow_dj@yahoo.com`, and send 10 EUR.

## Install

### Blender 4.2 and later (extension)

    python3 install.py --extension

builds `solidworks_import-2.0.0.zip`. In Blender open **Edit → Preferences →
Get Extensions**, choose **Install from Disk** in the menu at the top right,
and pick the zip.

### Any Blender version (add-on zip)

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

Each part is meshed once and kept in a cache, so importing the same file
again (or an assembly whose parts were imported before) takes well under a
second. The cache is keyed by the file's contents and the mesh detail, so a
changed file is always meshed afresh. It lives in `~/.cache/blender-solidworks`
(Windows: `%LOCALAPPDATA%\blender-solidworks`, macOS:
`~/Library/Caches/blender-solidworks`) and is safe to delete; the add-on
preferences have a **Clear Mesh Cache** button.

The first import of a part takes from well under a second for simple parts to
a minute for large ones with many curved surfaces (measured on a Raspberry Pi
4; a desktop PC is several times faster). Blender 2.81 and later triangulate
with Blender's own fast C code; Blender 2.79 uses a slower pure-Python version.

## Limitations

* SolidWorks files saved before 2011 use an older format that is not supported.
  Open and re-save them in a newer SolidWorks.
* Only the geometry is imported, as meshes: not the feature history, sketches
  or dimensions. Only each part's default configuration is used.
* Rolling-ball fillets are rebuilt from their spine and the two surfaces they
  touch, which is very close to, but not exactly, SolidWorks' own surface.
  Other unusual surfaces may come out flat-filled. Problems are reported as
  warnings in Blender's info bar and in the system console.

## Command line

`io_import_solidworks/sw2blender/cli.py` converts parts to mesh files without
Blender:

    python3 io_import_solidworks/sw2blender/cli.py part.SLDPRT -o part.obj

Output formats: `.obj` (with colours in a `.mtl`), `.stl`, `.ply`, all in
millimetres. Run with `--help` for all options.

## How it works

* `io_import_solidworks/__init__.py`: the Blender menu entries, options,
  preferences and drag-and-drop handler.
* `io_import_solidworks/importer.py`: builds Blender meshes, materials and the
  assembly hierarchy (Blender 2.79 and 2.80+).
* `io_import_solidworks/meshcache.py`: the mesh cache.
* `io_import_solidworks/sw2blender/`: reads SolidWorks files and meshes them,
  all in pure Python:
  * `cfb.py`, `container.py`, `partition.py`, `xt.py`: unpack the file and
    decode its Parasolid geometry.
  * `assembly.py`, `folder.py`: assembly structure and folder imports.
  * `surfaces.py`: planes, cylinders, cones, spheres, tori, B-splines, swept,
    spun and offset surfaces, and rolling-ball blends.
  * `tessellate.py`: samples the edges, lays each face out in its surface's
    parameters and triangulates it (`cdt.py`), keeping the inside.
  * `mesh.py`: welds the faces into watertight bodies facing outwards.

## Author and licence

Written by Denis Joseph Barrow (barrow_dj@yahoo.com) with the help of
[Claude Code](https://claude.com/claude-code).

LGPL-2.1-or-later. See [LICENSE](LICENSE).

SolidWorks and Parasolid are trademarks of Dassault Systèmes and Siemens
respectively. This add-on is an independent project, not affiliated with or
endorsed by either company.
