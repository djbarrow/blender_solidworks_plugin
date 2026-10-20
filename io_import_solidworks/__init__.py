# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Blender add-on: File > Import > SolidWorks (.sldprt, .sldasm)."""

bl_info = {
    "name": "SolidWorks Import",
    "author": "Denis Joseph Barrow",
    "version": (1, 0, 0),
    "blender": (2, 79, 0),
    "location": "File > Import > SolidWorks (.sldprt, .sldasm)",
    "description": "Import SolidWorks parts and assemblies (needs FreeCAD installed)",
    "warning": "",
    "doc_url": "https://github.com/djbarrow/blender_solidworks_plugin",
    "wiki_url": "https://github.com/djbarrow/blender_solidworks_plugin",
    "category": "Import-Export",
}

import math
import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       StringProperty)
from bpy_extras.io_utils import ImportHelper, axis_conversion

from . import importer, runner

LEGACY = bpy.app.version < (2, 80, 0)

AXES = [("X", "X", ""), ("Y", "Y", ""), ("Z", "Z", ""),
        ("-X", "-X", ""), ("-Y", "-Y", ""), ("-Z", "-Z", "")]

DETAIL = [("COARSE", "Coarse", "Fewer triangles, fastest", 0),
          ("NORMAL", "Normal", "Good for most models", 1),
          ("FINE", "Fine", "Smooth curves, more triangles", 2)]
DETAIL_VALUE = {"COARSE": 0.002, "NORMAL": 0.0005, "FINE": 0.0001}


def _annotate(cls):
    """Blender 2.80+ wants properties as annotations, 2.79 as class attributes."""
    if LEGACY:
        return cls
    ann = cls.__dict__.get("__annotations__", {})
    for k, v in list(cls.__dict__.items()):
        if isinstance(v, tuple) or type(v).__name__ == "_PropertyDeferred":
            ann[k] = v
            delattr(cls, k)
    cls.__annotations__ = ann
    return cls


def _prefs(context):
    p = context.user_preferences if LEGACY else context.preferences
    addon = p.addons.get(__name__)
    return addon.preferences if addon else None


class SolidWorksPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    freecad_path = StringProperty(
        name="FreeCAD",
        description="FreeCAD program (freecadcmd, FreeCADCmd.exe or the FreeCAD AppImage). "
                    "Leave empty to find it automatically",
        subtype="FILE_PATH", default="")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "freecad_path")
        found = runner.find_freecad(bpy.path.abspath(self.freecad_path) or None)
        layout.label(text=("Using: " + found) if found else
                     "FreeCAD not found: install FreeCAD 0.21+ or set its path above")
        layout.label(text="Mesh cache: " + runner.cache_dir())
        layout.operator(SolidWorksClearCache.bl_idname)


class SolidWorksClearCache(bpy.types.Operator):
    """Delete the meshes kept from earlier imports"""
    bl_idname = "import_scene.solidworks_clear_cache"
    bl_label = "Clear Mesh Cache"

    def execute(self, context):
        runner.clear_cache()
        self.report({"INFO"}, "SolidWorks mesh cache cleared")
        return {"FINISHED"}


class _Options:
    """Options shared by the file and folder importers."""

    scale = FloatProperty(
        name="Scale", description="Size of 1 mm in Blender units (0.001 = metres)",
        default=0.001, min=1e-6, max=1e6, soft_min=1e-4, soft_max=1.0, precision=4)
    detail = EnumProperty(name="Mesh Detail", items=DETAIL, default="NORMAL",
                          description="How closely the triangles follow curved surfaces")
    smooth = BoolProperty(name="Smooth Shading", default=True,
                          description="Smooth curved faces, keep edges sharper than 30 degrees")
    axis_forward = EnumProperty(name="Forward", items=AXES, default="-Z")
    axis_up = EnumProperty(name="Up", items=AXES, default="Y",
                           description="SolidWorks uses Y up; Blender uses Z up")
    search_folder = StringProperty(
        name="Parts Folder", subtype="DIR_PATH", default="",
        description="Extra folder to look in for parts an assembly uses")

    def import_options(self, context):
        try:
            gm = axis_conversion(from_forward=self.axis_forward, from_up=self.axis_up).to_4x4()
        except Exception:
            raise importer.ImportFailed("Forward and Up must be different axes")
        prefs = _prefs(context)
        fc = bpy.path.abspath(prefs.freecad_path) if prefs and prefs.freecad_path else None
        dirs = [bpy.path.abspath(self.search_folder)] if self.search_folder else []
        return {"scale": self.scale, "detail": DETAIL_VALUE[self.detail],
                "smooth_angle": math.radians(30) if self.smooth else 0.0,
                "freecad": fc, "search_dirs": dirs, "global_matrix": gm}

    def show(self, warnings, errors, summary):
        for w in warnings[:10]:
            self.report({"WARNING"}, w)
        if len(warnings) > 10:
            self.report({"WARNING"}, "... and %d more warnings (see the console)"
                        % (len(warnings) - 10))
        for e in errors:
            self.report({"ERROR"}, e)
        if summary:
            self.report({"INFO"}, summary)


class ImportSolidWorks(bpy.types.Operator, ImportHelper, _Options):
    """Import SolidWorks parts (.sldprt) and assemblies (.sldasm)"""
    bl_idname = "import_scene.solidworks"
    bl_label = "Import SolidWorks"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".sldprt"
    filter_glob = StringProperty(
        default="*.sldprt;*.SLDPRT;*.sldasm;*.SLDASM;*.x_t;*.X_T;*.x_b;*.X_B",
        options={"HIDDEN"})
    files = CollectionProperty(type=bpy.types.OperatorFileListElement,
                               options={"HIDDEN", "SKIP_SAVE"})
    directory = StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    # these properties belong to _Options; repeat them so 2.79 registers them
    scale = _Options.scale
    detail = _Options.detail
    smooth = _Options.smooth
    axis_forward = _Options.axis_forward
    axis_up = _Options.axis_up
    search_folder = _Options.search_folder

    def execute(self, context):
        if self.files and self.directory:
            paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        else:
            paths = [self.filepath]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}
        try:
            tops, warnings, errors = importer.import_files(context, paths,
                                                           **self.import_options(context))
        except importer.ImportFailed as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        summary = "SolidWorks: imported %d of %d file(s)" % (len(tops), len(paths))
        self.show(warnings, errors, summary)
        return {"FINISHED"} if tops else {"CANCELLED"}


class ImportSolidWorksFolder(bpy.types.Operator, ImportHelper, _Options):
    """Import every SolidWorks assembly in a folder, and every part they do not use"""
    bl_idname = "import_scene.solidworks_folder"
    bl_label = "Import SolidWorks Folder"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ""
    use_filter_folder = True
    filter_glob = StringProperty(default="*.sldprt;*.SLDPRT;*.sldasm;*.SLDASM",
                                 options={"HIDDEN"})
    directory = StringProperty(subtype="DIR_PATH")
    subfolders = BoolProperty(name="Include Subfolders", default=False,
                              description="Also import files in folders below this one")

    scale = _Options.scale
    detail = _Options.detail
    smooth = _Options.smooth
    axis_forward = _Options.axis_forward
    axis_up = _Options.axis_up
    search_folder = _Options.search_folder

    def execute(self, context):
        folder = self.directory or os.path.dirname(self.filepath)
        if not os.path.isdir(folder):
            self.report({"ERROR"}, "Not a folder: %s" % folder)
            return {"CANCELLED"}
        try:
            opts = self.import_options(context)
            if opts["search_dirs"]:
                opts["search_dirs"] = [folder] + opts["search_dirs"]
            tops, warnings, errors, summary = importer.import_folder(
                context, folder, recursive=self.subfolders, **opts)
        except importer.ImportFailed as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.show(warnings, errors, "SolidWorks: " + summary)
        return {"FINISHED"} if tops else {"CANCELLED"}


def menu_import(self, context):
    self.layout.operator(ImportSolidWorks.bl_idname, text="SolidWorks (.sldprt, .sldasm)")
    self.layout.operator(ImportSolidWorksFolder.bl_idname, text="SolidWorks Folder")


CLASSES = (SolidWorksClearCache, SolidWorksPreferences, ImportSolidWorks, ImportSolidWorksFolder)


def _import_menu():
    return bpy.types.INFO_MT_file_import if LEGACY else bpy.types.TOPBAR_MT_file_import


def register():
    for cls in CLASSES:
        bpy.utils.register_class(_annotate(cls))
    _import_menu().append(menu_import)


def unregister():
    _import_menu().remove(menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
