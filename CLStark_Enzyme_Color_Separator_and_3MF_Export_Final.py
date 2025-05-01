bl_info = {
    "name": "Enzyme Color Separator + Export All Visible Meshes to 3MF",
    "author": "Colin L. Stark",
    "version": (3, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Prusa Tools",
    "description": "Separates mesh by vertex color and exports valid 3MF for PrusaSlicer, rebuilds the broken Blender 3MF output system and respects the PrusaSlicer XML tags as well as relative locations for models so nothing must be done manually!",

    "category": "Import-Export"
}

import bpy
import math
import os
import zipfile
import struct
import xml.etree.ElementTree as ET
from mathutils import Color
from collections import defaultdict
import re

def quantize_color(color, levels):
    return tuple(round(c * (levels - 1)) / (levels - 1) for c in color)

def color_distance(c1, c2):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))

def parse_stl(filepath):
    vertices = []
    triangles = []
    vertex_map = {}

    with open(filepath, 'rb') as f:
        f.read(80)
        count = struct.unpack('<I', f.read(4))[0]
        for _ in range(count):
            f.read(12)
            tri = []
            for _ in range(3):
                coords = struct.unpack('<fff', f.read(12))
                if coords not in vertex_map:
                    vertex_map[coords] = len(vertices)
                    vertices.append(coords)
                tri.append(vertex_map[coords])
            triangles.append(tri)
            f.read(2)
    return vertices, triangles

def safe_xml_name(name):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name)

class EnzymeColorSplitter(bpy.types.Operator):
    bl_idname = "object.separate_enzyme_color"
    bl_label = "Separate Vertex Colors"
    bl_description = "Separate mesh into objects by vertex color clusters"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Select an active mesh object.")
            return {'CANCELLED'}

        mesh = obj.data
        if not mesh.color_attributes:
            self.report({'ERROR'}, "No vertex color attributes found.")
            return {'CANCELLED'}

        color_attr = mesh.color_attributes.active_color
        levels_per_channel = 4
        max_color_distance = 0.15

        poly_colors = []
        for poly in mesh.polygons:
            face_colors = []
            for li in poly.loop_indices:
                if li < len(color_attr.data):
                    color = color_attr.data[li].color[:3]
                    face_colors.append(color)
            if face_colors:
                avg_color = [sum(c[i] for c in face_colors) / len(face_colors) for i in range(3)]
            else:
                avg_color = (0.5, 0.5, 0.5)
            poly_colors.append(tuple(avg_color))

        clusters = defaultdict(list)
        cluster_centers = []
        cluster_colors = {}

        for poly_idx, avg_color in enumerate(poly_colors):
            quantized = quantize_color(avg_color, levels_per_channel)
            assigned = False
            for i, center in enumerate(cluster_centers):
                if color_distance(quantized, center) < max_color_distance:
                    clusters[i].append(poly_idx)
                    assigned = True
                    break
            if not assigned:
                cluster_id = len(cluster_centers)
                cluster_centers.append(quantized)
                clusters[cluster_id].append(poly_idx)
                cluster_colors[cluster_id] = avg_color

        obj.select_set(True)
        bpy.ops.object.duplicate()
        dup_obj = context.selected_objects[0]

        dup_obj.data.materials.clear()
        for idx in range(len(clusters)):
            color = cluster_colors[idx]
            mat = bpy.data.materials.new(name=f"ClusterColor_{idx}")
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                bsdf.inputs["Base Color"].default_value = (*color, 1.0)
            dup_obj.data.materials.append(mat)

        for mat_idx, poly_indices in clusters.items():
            for poly_idx in poly_indices:
                dup_obj.data.polygons[poly_idx].material_index = mat_idx

        context.view_layer.objects.active = dup_obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.separate(type='MATERIAL')
        bpy.ops.object.mode_set(mode='OBJECT')

        for idx, o in enumerate(context.selected_objects):
            o.name = f"Enzyme_ColorGroup_{idx+1}"

        self.report({'INFO'}, f"Separated into {len(clusters)} color groups.")
        return {'FINISHED'}

class ExportEnzyme3MF(bpy.types.Operator):
    bl_idname = "export_scene.enzyme_3mf"
    bl_label = "Export All Visible Meshes to 3MF"
    bl_description = "Export all visible mesh objects in the scene to valid 3MF"

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        export_path = bpy.path.abspath(self.filepath)
        export_dir = os.path.dirname(export_path)
        model_name = os.path.splitext(os.path.basename(export_path))[0]

        temp_dir = os.path.join(export_dir, f"{model_name}_temp")
        os.makedirs(temp_dir, exist_ok=True)
        model_dir = os.path.join(temp_dir, "3mf_model")
        os.makedirs(os.path.join(model_dir, "3D"), exist_ok=True)

        model_root = ET.Element("model", xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02", unit="millimeter")
        resources = ET.SubElement(model_root, "resources")
        basematerials = ET.SubElement(resources, "basematerials", id="1")

        object_ids = []
        mesh_objs = [
            o for o in bpy.context.scene.objects
            if o.type == 'MESH' and not o.hide_viewport and not o.hide_get()
        ]

        for idx, obj in enumerate(mesh_objs, start=1):
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

            export_name = safe_xml_name(obj.name)
            stl_path = os.path.join(temp_dir, f"{export_name}.stl")
            bpy.ops.export_mesh.stl(filepath=stl_path, use_selection=True, global_scale=1.0)

            color = (0.5, 0.5, 0.5)
            if obj.active_material and obj.active_material.use_nodes:
                bsdf = obj.active_material.node_tree.nodes.get("Principled BSDF")
                if bsdf:
                    color = bsdf.inputs["Base Color"].default_value[:3]

            c = Color(color)
            hex_color = '#{:02X}{:02X}{:02X}'.format(int(c.r * 255), int(c.g * 255), int(c.b * 255))
            ET.SubElement(basematerials, "base", name=export_name, displaycolor=hex_color)

            obj_elem = ET.SubElement(resources, "object", id=str(idx), type="model", pid="1", pindex=str(idx - 1))
            mesh_elem = ET.SubElement(obj_elem, "mesh")
            vertices_elem = ET.SubElement(mesh_elem, "vertices")
            triangles_elem = ET.SubElement(mesh_elem, "triangles")

            verts, tris = parse_stl(stl_path)
            for v in verts:
                ET.SubElement(vertices_elem, "vertex", x=str(v[0]), y=str(v[1]), z=str(v[2]))
            for t in tris:
                ET.SubElement(triangles_elem, "triangle", v1=str(t[0]), v2=str(t[1]), v3=str(t[2]))

            object_ids.append((export_name, idx))

        composite_id = 100
        composite = ET.SubElement(resources, "object", id=str(composite_id), type="model")
        components = ET.SubElement(composite, "components")
        for (_, idx) in object_ids:
            ET.SubElement(components, "component", objectid=str(idx))

        build = ET.SubElement(model_root, "build")
        ET.SubElement(build, "item", objectid=str(composite_id))

        ET.ElementTree(model_root).write(os.path.join(model_dir, "3D", "3DModel.model"), encoding='UTF-8', xml_declaration=True)

        with open(os.path.join(model_dir, "[Content_Types].xml"), 'w') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n</Types>')

        rels_dir = os.path.join(model_dir, "_rels")
        os.makedirs(rels_dir, exist_ok=True)
        with open(os.path.join(rels_dir, ".rels"), 'w') as f:
            f.write('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  <Relationship Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" Target="/3D/3DModel.model"/>\n</Relationships>')

        with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for foldername, subfolders, filenames in os.walk(model_dir):
                for filename in filenames:
                    filepath = os.path.join(foldername, filename)
                    arcname = os.path.relpath(filepath, model_dir)
                    zipf.write(filepath, arcname)

        self.report({'INFO'}, f"3MF exported to: {export_path}")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

class EnzymePanel(bpy.types.Panel):
    bl_label = "Prusa 3MF Export Tools"
    bl_idname = "VIEW3D_PT_enzyme_export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Prusa Tools'

    def draw(self, context):
        layout = self.layout
        layout.operator("object.separate_enzyme_color")
        layout.operator("export_scene.enzyme_3mf")

classes = [EnzymeColorSplitter, ExportEnzyme3MF, EnzymePanel]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
