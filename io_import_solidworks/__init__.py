# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Blender add-on: File > Import > SolidWorks (.sldprt, .sldasm).

Works on its own: the SolidWorks geometry is read and meshed in pure Python
(sw2blender/). Supports Blender 2.79 up to the current 5.x releases; Blender
4.2+ also reads blender_manifest.toml and installs it as an extension.
"""

bl_info = {
    "name": "SolidWorks Import",
    "author": "Denis Joseph Barrow",
    "version": (2, 0, 0),
    "blender": (2, 79, 0),
    "location": "File > Import > SolidWorks (.sldprt, .sldasm)",
    "description": "Import SolidWorks parts and assemblies",
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

from . import importer, meshcache

LEGACY = bpy.app.version < (2, 80, 0)

AXES = [("X", "X", ""), ("Y", "Y", ""), ("Z", "Z", ""),
        ("-X", "-X", ""), ("-Y", "-Y", ""), ("-Z", "-Z", "")]

DETAIL = [("COARSE", "Coarse", "Fewer triangles, fastest", 0),
          ("NORMAL", "Normal", "Good for most models", 1),
          ("FINE", "Fine", "Smooth curves, more triangles", 2)]
DETAIL_VALUE = {"COARSE": 0.002, "NORMAL": 0.0005, "FINE": 0.0001}


def _option_props():
    """Import options shared by the file and folder importers (made fresh for
    each operator class: Blender wants its own property objects per class)."""
    return {
        "scale": FloatProperty(
            name="Scale", description="Size of 1 mm in Blender units (0.001 = metres)",
            default=0.001, min=1e-6, max=1e6, soft_min=1e-4, soft_max=1.0, precision=4),
        "detail": EnumProperty(name="Mesh Detail", items=DETAIL, default="NORMAL",
                               description="How closely the triangles follow curved surfaces"),
        "smooth": BoolProperty(name="Smooth Shading", default=True,
                               description="Smooth curved faces, keep edges sharper than "
                                           "30 degrees sharp"),
        "axis_forward": EnumProperty(name="Forward", items=AXES, default="-Z"),
        "axis_up": EnumProperty(name="Up", items=AXES, default="Y",
                                description="SolidWorks uses Y up; Blender uses Z up"),
        "search_folder": StringProperty(
            name="Parts Folder", subtype="DIR_PATH", default="",
            description="Extra folder to look in for parts an assembly uses"),
    }


def _is_prop(v):
    return isinstance(v, tuple) or type(v).__name__ == "_PropertyDeferred"


def _make(name, base, extra=None):
    """Final operator class: `base` plus the shared import options, with every
    property in the class body when the class is created (Blender 2.79 reads
    them only then; 2.80+ wants them as annotations)."""
    props = {}
    if LEGACY:
        # 2.79 only looks at the class itself: bring in ImportHelper's filepath etc.
        for klass in reversed(base.__mro__):
            props.update({k: v for k, v in klass.__dict__.items() if _is_prop(v)})
    else:
        props = {k: v for k, v in base.__dict__.items() if _is_prop(v)}
        for k in props:
            delattr(base, k)          # 2.80+: properties belong in annotations only
    props.update(extra or {})
    ns = {"__module__": __name__, "__doc__": base.__doc__}
    if LEGACY:
        ns.update(props)
    else:
        ns["__annotations__"] = props
    return type(name, (base,), ns)


def _fix_annotations(cls):
    """Move properties written as class attributes into annotations (2.80+)."""
    if LEGACY:
        return cls
    ann = dict(cls.__dict__.get("__annotations__", {}))
    for k, v in list(cls.__dict__.items()):
        if _is_prop(v):
            ann[k] = v
            delattr(cls, k)
    cls.__annotations__ = ann
    return cls


class _Options:
    def import_options(self, context):
        try:
            gm = axis_conversion(from_forward=self.axis_forward, from_up=self.axis_up).to_4x4()
        except Exception:
            raise importer.ImportFailed("Forward and Up must be different axes")
        dirs = [bpy.path.abspath(self.search_folder)] if self.search_folder else []
        return {"scale": self.scale, "detail": DETAIL_VALUE[self.detail],
                "smooth_angle": math.radians(30) if self.smooth else 0.0,
                "search_dirs": dirs, "global_matrix": gm}

    def show(self, warnings, errors, summary):
        for w in warnings[:10]:
            self.report({"WARNING"}, w)
        if len(warnings) > 10:
            self.report({"WARNING"}, "... and %d more warnings (see the system console)"
                        % (len(warnings) - 10))
        for e in errors:
            self.report({"ERROR"}, e)
        if summary:
            self.report({"INFO"}, summary)


class SolidWorksPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        layout.label(text="Meshes are kept so files open faster the next time.")
        layout.label(text="Mesh cache: " + meshcache.cache_dir())
        layout.operator(SolidWorksClearCache.bl_idname)


class SolidWorksClearCache(bpy.types.Operator):
    """Delete the meshes kept from earlier imports"""
    bl_idname = "import_scene.solidworks_clear_cache"
    bl_label = "Clear Mesh Cache"

    def execute(self, context):
        meshcache.clear_cache()
        self.report({"INFO"}, "SolidWorks mesh cache cleared")
        return {"FINISHED"}


class _ImportSolidWorks(bpy.types.Operator, ImportHelper, _Options):
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


class _ImportSolidWorksFolder(bpy.types.Operator, ImportHelper, _Options):
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

    def execute(self, context):
        folder = self.directory or os.path.dirname(self.filepath)
        if not os.path.isdir(folder):
            self.report({"ERROR"}, "Not a folder: %s" % folder)
            return {"CANCELLED"}
        try:
            opts = self.import_options(context)
            opts["search_dirs"] = [folder] + opts["search_dirs"]
            tops, warnings, errors, summary = importer.import_folder(
                context, folder, recursive=self.subfolders, **opts)
        except importer.ImportFailed as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.show(warnings, errors, "SolidWorks: " + summary)
        return {"FINISHED"} if tops else {"CANCELLED"}


ImportSolidWorks = _make("ImportSolidWorks", _ImportSolidWorks, _option_props())
ImportSolidWorksFolder = _make("ImportSolidWorksFolder", _ImportSolidWorksFolder, _option_props())


def menu_import(self, context):
    self.layout.operator(ImportSolidWorks.bl_idname, text="SolidWorks (.sldprt, .sldasm)")
    self.layout.operator(ImportSolidWorksFolder.bl_idname, text="SolidWorks Folder")


CLASSES = [SolidWorksClearCache, SolidWorksPreferences, ImportSolidWorks, ImportSolidWorksFolder]

# Blender 4.1+: drop .sldprt / .sldasm files onto the 3D view to import them
if hasattr(bpy.types, "FileHandler"):
    class IO_FH_solidworks(bpy.types.FileHandler):
        bl_idname = "IO_FH_solidworks"
        bl_label = "SolidWorks"
        bl_import_operator = ImportSolidWorks.bl_idname
        bl_file_extensions = ".sldprt;.sldasm;.x_t;.x_b"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type == "VIEW_3D"

    CLASSES.append(IO_FH_solidworks)


def _import_menu():
    return bpy.types.INFO_MT_file_import if LEGACY else bpy.types.TOPBAR_MT_file_import


def register():
    for cls in CLASSES:
        _fix_annotations(cls)
        bpy.utils.register_class(cls)
    _import_menu().append(menu_import)


def unregister():
    _import_menu().remove(menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
