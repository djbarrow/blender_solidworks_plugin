# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.
# If you find this project useful, please support it with a 10 euro tip
# via PayPal to barrow_dj@yahoo.com.

"""Build Blender objects from SolidWorks parts and assemblies.

Works with Blender 2.79 and 2.80+. Meshes come from runner.mesh_parts().
Units: SolidWorks geometry is in mm and assembly placements in metres; both
are multiplied by `scale` so that the default (0.001) gives metres.
"""

import math
import os

import bpy
from mathutils import Matrix

from . import runner
from .sw2fc import assembly as _asm
from .sw2fc import folder as _folder

PART_EXTS = (".sldprt", ".x_t", ".x_b", ".xmt_txt", ".xmt_bin")
ASM_EXTS = (".sldasm",)

LEGACY = bpy.app.version < (2, 80, 0)


class ImportFailed(RuntimeError):
    pass


# ------------------------------------------------------------ 2.79 / 2.80+ helpers
def matmul(a, b):
    return a * b if LEGACY else a @ b


def link_object(context, obj):
    if LEGACY:
        context.scene.objects.link(obj)
    else:
        context.collection.objects.link(obj)


def select(obj, state=True):
    if LEGACY:
        obj.select = state
    else:
        obj.select_set(state)


def set_active(context, obj):
    if LEGACY:
        context.scene.objects.active = obj
    else:
        context.view_layer.objects.active = obj


def hide(obj):
    obj.hide_render = True
    if LEGACY:
        obj.hide = True
    else:
        obj.hide_set(True)


def update_scene(context):
    if LEGACY:
        context.scene.update()
    else:
        context.view_layer.update()


def new_empty(context, name, size):
    obj = bpy.data.objects.new(name, None)
    if LEGACY:
        obj.empty_draw_type = "PLAIN_AXES"
        obj.empty_draw_size = size
    else:
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = size
    link_object(context, obj)
    return obj


# ------------------------------------------------------------ materials
def get_material(rgb, transparency):
    """A material per colour, shared by every imported object that uses it."""
    r, g, b = (max(0.0, min(1.0, float(c))) for c in rgb[:3])
    alpha = 1.0 - max(0.0, min(1.0, float(transparency or 0.0)))
    name = "SW_%02x%02x%02x" % tuple(int(round(c * 255)) for c in (r, g, b))
    if alpha < 1.0:
        name += "_a%02d" % int(round(alpha * 100))
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    if LEGACY:
        mat.diffuse_color = (r, g, b)
        mat.diffuse_intensity = 0.9
        mat.specular_intensity = 0.3
        if alpha < 1.0:
            mat.use_transparency = True
            mat.alpha = alpha
    else:
        mat.diffuse_color = (r, g, b, alpha)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
            if "Alpha" in bsdf.inputs:
                bsdf.inputs["Alpha"].default_value = alpha
        if alpha < 1.0:
            for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
                try:
                    setattr(mat, attr, val)
                except (AttributeError, TypeError):
                    pass
    return mat


# ------------------------------------------------------------ meshes
def build_mesh(name, body, scale, smooth_angle):
    """Blender mesh datablock for one body."""
    me = bpy.data.meshes.new(name)
    nv = len(body.verts) // 3
    nt = len(body.tris) // 3
    co = body.verts if scale == 1.0 else [c * scale for c in body.verts]
    me.vertices.add(nv)
    me.vertices.foreach_set("co", co)
    me.loops.add(3 * nt)
    me.loops.foreach_set("vertex_index", body.tris)
    me.polygons.add(nt)
    me.polygons.foreach_set("loop_start", range(0, 3 * nt, 3))
    me.polygons.foreach_set("loop_total", [3] * nt)
    me.polygons.foreach_set("material_index", body.tri_colour)
    for rgb in body.colours:
        me.materials.append(get_material(rgb, body.transparency))
    me.update(calc_edges=True)
    me.validate()
    if smooth_angle > 0.0:
        me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
        if hasattr(me, "use_auto_smooth"):            # up to Blender 4.0
            me.use_auto_smooth = True
            me.auto_smooth_angle = smooth_angle
        elif hasattr(me, "set_sharp_from_angle"):     # Blender 4.1+
            me.set_sharp_from_angle(angle=smooth_angle)
    return me


class Importer:
    """One import run: options, the mesh cache and messages for the user."""

    def __init__(self, context, scale=0.001, detail=0.0005, smooth_angle=math.radians(30),
                 freecad=None, search_dirs=(), global_matrix=None, report=None):
        self.context = context
        self.scale = scale
        self.detail = detail
        self.smooth_angle = smooth_angle
        self.freecad = freecad
        self.search_dirs = list(search_dirs)
        self.global_matrix = global_matrix if global_matrix is not None else Matrix.Identity(4)
        self.report = report or (lambda level, text: print("SolidWorks import: %s" % text))
        self.warnings = []
        self.meshes = {}          # (part path, body index) -> bpy mesh, shared by instances
        self.parts = {}           # part path -> runner.PartMesh

    # ---------------------------------------------------- messages
    def warn(self, text):
        self.warnings.append(text)
        print("SolidWorks import: %s" % text)

    def log(self, text):
        print("SolidWorks import: %s" % text)

    # ---------------------------------------------------- meshing
    def fetch(self, paths):
        """Mesh every part file in `paths` that has not been meshed yet."""
        todo = [p for p in dict.fromkeys(paths) if p not in self.parts]
        if not todo:
            return
        wm = self.context.window_manager
        wm.progress_begin(0, len(todo))
        try:
            own = os.path.dirname(os.path.dirname(os.path.realpath(bpy.app.binary_path)))
            self.parts.update(runner.mesh_parts(
                todo, detail=self.detail, freecad=self.freecad, log=self.log, own_dir=own,
                progress=lambda done, total: wm.progress_update(done)))
        except runner.MeshError as exc:
            raise ImportFailed(str(exc))
        finally:
            wm.progress_end()
        for p in todo:
            pm = self.parts[p]
            for w in pm.warnings:
                self.warn("%s: %s" % (os.path.basename(p), w))
            if pm.error:
                self.warn("%s: %s" % (os.path.basename(p), pm.error))

    def body_mesh(self, path, i):
        key = (path, i)
        me = self.meshes.get(key)
        if me is None:
            body = self.parts[path].bodies[i]
            me = build_mesh(body.label, body, self.scale, self.smooth_angle)
            self.meshes[key] = me
        return me

    # ---------------------------------------------------- objects
    def part_objects(self, path, name, hidden=False):
        """Objects for a part: one mesh object, or an empty holding one
        object per body when the part has several. None if nothing was meshed."""
        pm = self.parts.get(path)
        if pm is None or not pm.bodies:
            return None
        if len(pm.bodies) == 1:
            obj = bpy.data.objects.new(name, self.body_mesh(path, 0))
            link_object(self.context, obj)
            if hidden:
                hide(obj)
            return obj
        holder = new_empty(self.context, name, 20.0 * self.scale)
        for i, body in enumerate(pm.bodies):
            obj = bpy.data.objects.new(body.label, self.body_mesh(path, i))
            link_object(self.context, obj)
            obj.parent = holder
            if hidden:
                hide(obj)
        return holder

    def placement(self, m):
        """SolidWorks 4x4 (column vectors, translation in metres) -> Blender matrix."""
        t = 1000.0 * self.scale
        return Matrix(((m[0][0], m[0][1], m[0][2], m[0][3] * t),
                       (m[1][0], m[1][1], m[1][2], m[1][3] * t),
                       (m[2][0], m[2][1], m[2][2], m[2][3] * t),
                       (0.0, 0.0, 0.0, 1.0)))

    # ---------------------------------------------------- files
    def import_file(self, path):
        """Import one part or assembly. Returns its top object (or None)."""
        ext = os.path.splitext(path)[1].lower()
        if ext in ASM_EXTS:
            top = self.import_assembly(path)
        elif ext in PART_EXTS:
            top = self.import_part(path)
        else:
            raise ImportFailed("%s: not a SolidWorks part or assembly" % os.path.basename(path))
        if top is not None:
            top.matrix_basis = matmul(self.global_matrix, top.matrix_basis)
        return top

    def import_part(self, path):
        self.fetch([path])
        name = os.path.splitext(os.path.basename(path))[0]
        top = self.part_objects(path, name)
        if top is None:
            raise ImportFailed("%s: %s" % (os.path.basename(path),
                                           self.parts[path].error or "nothing could be imported"))
        return top

    def import_assembly(self, path):
        try:
            root = _asm.read_tree(path)
        except _asm.AssemblyError as exc:
            raise ImportFailed("%s: %s" % (os.path.basename(path), exc))
        except Exception as exc:
            raise ImportFailed("%s: not a readable SolidWorks assembly (%s)"
                               % (os.path.basename(path), exc))
        leaves = [c for c, _ in root.walk() if c.doc_type == "PART"]
        stored = sorted({c.path for c in leaves})
        found = {p: _asm.resolve_file(p, path, self.search_dirs) for p in stored}
        lost = sorted({_asm._basename(p) for p in stored if not found[p]})
        self.fetch(sorted({f for f in found.values() if f}))
        counts = {"placed": 0, "missing": 0}
        top = new_empty(self.context, root.name, 50.0 * self.scale)
        self._add_children(root, top, found, counts, hidden=False)
        self.log("%s: %d component(s) placed" % (os.path.basename(path), counts["placed"]))
        if lost:
            self.warn("%s: %d component(s) left out because these part files were not found "
                      "(put them next to the assembly or add a search folder): %s"
                      % (os.path.basename(path), counts["missing"], ", ".join(lost)))
        if not counts["placed"]:
            raise ImportFailed("%s: no components could be imported" % os.path.basename(path))
        return top

    def _add_children(self, comp, parent, found, counts, hidden):
        for c in comp.children:
            if c.suppressed:
                continue
            label = "%s-%d" % (c.name, c.instance)        # as in the SolidWorks tree
            is_hidden = hidden or c.hidden
            if c.doc_type == "PART":
                p = found.get(c.path)
                if not p:
                    counts["missing"] += 1
                    continue
                obj = self.part_objects(p, label, hidden=is_hidden)
                if obj is None:
                    continue
                counts["placed"] += 1
            else:
                obj = new_empty(self.context, label, 30.0 * self.scale)
                self._add_children(c, obj, found, counts, is_hidden)
                if not obj.children:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    continue
            obj.parent = parent
            obj.matrix_basis = self.placement(c.matrix)


def _descendants(obj):
    for c in obj.children:
        yield c
        for g in _descendants(c):
            yield g


def world_bounds(context, obj):
    """(min xyz, max xyz) of the mesh objects under `obj`, in world space."""
    update_scene(context)
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for o in [obj] + list(_descendants(obj)):
        if o.type != "MESH":
            continue
        for corner in o.bound_box:
            from mathutils import Vector
            w = matmul(o.matrix_world, Vector(corner))
            for k in range(3):
                lo[k] = min(lo[k], w[k])
                hi[k] = max(hi[k], w[k])
    if lo[0] == float("inf"):
        return None
    return lo, hi


def import_files(context, paths, **options):
    """Import part / assembly files. Returns (top objects, warnings, errors)."""
    imp = Importer(context, **options)
    tops, errors = [], []
    for p in paths:
        try:
            top = imp.import_file(p)
        except ImportFailed as exc:
            errors.append(str(exc))
            if "FreeCAD was not found" in str(exc) or "FreeCAD failed" in str(exc):
                break                     # every other file would fail the same way
            continue
        if top is not None:
            tops.append(top)
    finish(context, tops)
    return tops, imp.warnings, errors


def import_folder(context, folder, recursive=False, spacing=0.2, **options):
    """Import every top-level assembly and every part no assembly uses, laid
    out side by side along X. Returns (top objects, warnings, errors, summary)."""
    top_asms, loose, skipped = _folder.plan_folder(folder, recursive)
    items = top_asms + loose
    if not items:
        raise ImportFailed("no SolidWorks parts or assemblies found in %s" % folder)
    options.setdefault("search_dirs", [folder])
    imp = Importer(context, **options)
    # one FreeCAD run for every part in the folder instead of one per file
    wanted = list(loose)
    for a in top_asms:
        try:
            root = _asm.read_tree(a)
            for c, _ in root.walk():
                if c.doc_type == "PART":
                    p = _asm.resolve_file(c.path, a, imp.search_dirs)
                    if p:
                        wanted.append(p)
        except Exception:
            pass                           # reported when the assembly is imported
    imp.fetch(wanted)
    tops, errors = [], []
    x = 0.0
    for p in items:
        try:
            top = imp.import_file(p)
        except ImportFailed as exc:
            errors.append(str(exc))
            continue
        if top is None:
            continue
        b = world_bounds(context, top)
        if b is not None:
            lo, hi = b
            top.location.x += x - lo[0]
            x += (hi[0] - lo[0]) + spacing
        tops.append(top)
    finish(context, tops)
    summary = "imported %d of %d: %d assembl%s and %d part%s" % (
        len(tops), len(items), len(top_asms), "y" if len(top_asms) == 1 else "ies",
        len(loose), "" if len(loose) == 1 else "s")
    if skipped:
        summary += "; %d file%s used inside the assemblies %s not imported separately" % (
            skipped, "" if skipped == 1 else "s", "was" if skipped == 1 else "were")
    return tops, imp.warnings, errors, summary


def finish(context, tops):
    if not tops:
        return
    for o in context.scene.objects:
        if o.select if LEGACY else o.select_get():
            select(o, False)
    for t in tops:
        select(t)
    set_active(context, tops[-1])
    update_scene(context)
