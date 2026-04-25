import json
import asyncio
import os
import tempfile
import zipfile
import mimetypes
import xml.etree.ElementTree as ET
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Form, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from build123d import BuildPart, BuildSketch, import_svg, extrude, Compound, export_step, export_stl, Color, Plane, add, Mesher
import uvicorn

PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")
BASE_DIR = Path(__file__).parent.resolve()
ICONS_FOLDER = BASE_DIR / "Icons_SVG"
ICON_TAGS_FILE = ICONS_FOLDER / "icon_tags.json"
TMP_DIR = Path(os.environ.get("TMPDIR", "/tmp"))
MAX_PARALLEL_EXPORT_JOBS = max(1, int(os.environ.get("MAX_PARALLEL_EXPORT_JOBS", "4")))
EXPORT_JOB_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_EXPORT_JOBS)

# Lib3MF rejects overly-dense tessellations ("3mf mesh is invalid"); defaults in
# Mesher.add_shape (0.001 mm linear) are far finer than FDM needs and are very slow.
MESHER_LINEAR_DEFLECTION = float(os.environ.get("MESHER_LINEAR_DEFLECTION", "0.05"))
MESHER_ANGULAR_DEFLECTION = float(os.environ.get("MESHER_ANGULAR_DEFLECTION", "0.5"))

# Ensure font files under /assets are served with correct MIME types.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/otf", ".otf")

app = FastAPI(title="InfinityGrid Sticker Designer API")
THREEMF_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
THREEMF_MATERIAL_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
BAMBU_NS = "http://schemas.bambulab.com/package/2021"

async def run_export_worker(worker, *args):
    async with EXPORT_JOB_SEMAPHORE:
        await asyncio.to_thread(worker, *args)

@app.middleware("http")
async def disable_icon_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/icons/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

def _save_failed_svg_debug(filename: str, svg_content: str):
    if not svg_content:
        return
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        failed_path = TMP_DIR / filename
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write(svg_content)
        print(f"Saved failed SVG to {failed_path}")
    except Exception as write_err:
        print(f"Failed to save debug SVG '{filename}': {write_err}")

def _set_shape_metadata(shape, *, label=None, material=None, color=None):
    if label is not None:
        try:
            shape.label = label
        except Exception:
            pass
    if material is not None:
        try:
            shape.material = material
        except Exception:
            pass
    if color is not None:
        try:
            shape.color = color
        except Exception:
            pass


def _solid_components(shape):
    """Return discrete solids for STEP/Compound/metadata.

    import_svg + extrude sometimes yields topology that does not enumerate via
    :meth:`Shape.solids` but still meshes as a single body (``Mesher.add_shape``).
    """
    try:
        solids = list(shape.solids())
    except Exception:
        solids = []
    if solids:
        return solids
    try:
        if float(shape.volume) > 1e-12:
            return [shape]
    except Exception:
        pass
    # Open shells / numerical near-zero volume but still printable surface
    try:
        if float(shape.area) > 1e-3:
            return [shape]
    except Exception:
        pass
    try:
        bb = shape.bounding_box()
        diag = (bb.max - bb.min).length
        if float(diag) > 1e-9:
            return [shape]
    except Exception:
        pass
    return []


def _mesher_item_count(shape):
    """How many mesh objects ``_add_part_to_mesher`` adds for this shape."""
    try:
        solids = list(shape.solids())
        if solids:
            return len(solids)
    except Exception:
        pass
    return 1


def _build_label_parts_from_svg(svg_path: Path, w, h, sty, label_shape="classic", carve_pocket=True):
    base_color = Color(0, 0, 0)
    content_color = Color(1, 1, 1)
    base_thickness = 0.8
    svg_width_val = float(w)
    svg_height_val = float(h)

    with BuildPart() as base:
        from build123d import RectangleRounded, chamfer, Axis, Locations
        is_cullenect = str(label_shape).lower() == "cullenect"

        if not is_cullenect:
            length = svg_width_val + 1.3
            base_width_val = 11.5
            chamfer_val = 0.2
            corner_radius = 0.9
            with BuildSketch() as _sketch:
                RectangleRounded(length, base_width_val, corner_radius)
                RectangleRounded(length + 2, 5.7, 0.2)
            extrude(amount=base_thickness)
            top_edges = base.faces().sort_by(Axis.Z)[-1].outer_wire().edges()
            bottom_edges = base.faces().sort_by(Axis.Z)[0].outer_wire().edges()
            try:
                chamfer(top_edges + bottom_edges, length=chamfer_val)
            except Exception as e:
                print(f"Warning: Chamfer failed on base: {e}")
        else:
            # Cullenect geometry mirrored from Cullenect.scad:
            # - total thickness 1.2 mm
            # - 0.2 mm bottom rib layer + 0.6 mm middle + 0.4 mm top
            # - V1 segmented latch profile for 1U labels
            base_thickness = 1.2
            label_len = svg_width_val + 1.5  # 34.5 -> 36.0
            label_w = 11.0
            rounded_r = 0.5
            one_u_v1 = abs(label_len - 36.0) < 0.35

            def _x_center(left_edge, seg_width):
                return (-label_len / 2.0) + left_edge + (seg_width / 2.0)

            if one_u_v1:
                # Bottom segmented layer (0.2 mm)
                bottom_segments = [5.395, 11.18, 11.18, 5.395]
                bottom_gap = 0.95
                left = 0.0
                for seg_w in bottom_segments:
                    with BuildSketch(Plane.XY.offset(0.0)):
                        with Locations((_x_center(left, seg_w), 0)):
                            RectangleRounded(seg_w, label_w, rounded_r)
                    extrude(amount=0.2)
                    left += seg_w + bottom_gap

                # Middle segmented layer (0.6 mm)
                mid_segments = [4.695, 10.18, 10.18, 4.695]
                mid_gap = 1.95
                left = 0.2
                for seg_w in mid_segments:
                    with BuildSketch(Plane.XY.offset(0.2)):
                        with Locations((_x_center(left, seg_w), 0)):
                            RectangleRounded(seg_w, label_w - 0.4, rounded_r)
                    extrude(amount=0.6)
                    left += seg_w + mid_gap
            else:
                # V2 profile (for wider labels): full bottom + inset middle
                with BuildSketch(Plane.XY.offset(0.0)):
                    RectangleRounded(label_len, label_w, rounded_r)
                extrude(amount=0.2)
                with BuildSketch(Plane.XY.offset(0.2)):
                    RectangleRounded(label_len - 0.4, label_w - 0.4, rounded_r)
                extrude(amount=0.6)

            # Top full cap layer (0.4 mm)
            with BuildSketch(Plane.XY.offset(0.8)):
                RectangleRounded(label_len, label_w, rounded_r)
            extrude(amount=0.4)
        _set_shape_metadata(
            base.part,
            label="Base_Black",
            material="Base_Black",
            color=base_color
        )

    from build123d import Locations

    def build_svg_part(z_offset: float, depth: float):
        with BuildPart() as p:
            with BuildSketch(Plane.XY.offset(z_offset)):
                with Locations((-svg_width_val / 2, -svg_height_val / 2)):
                    add(import_svg(str(svg_path)))
            extrude(amount=depth)
        return p.part

    if sty == "flush":
        # Flush: content top sits at the base top (z = base_thickness).
        inlay_depth = 0.2
        floor_clearance = 0.02
        pocket_depth = inlay_depth + floor_clearance
        if carve_pocket:
            cutter_part = build_svg_part(base_thickness - pocket_depth, pocket_depth)
            base.part -= cutter_part
            content_part = build_svg_part(base_thickness - inlay_depth, inlay_depth)
        else:
            # In fallback mode, keep content visible on the top face while
            # avoiding perfectly coplanar overlap with the base.
            z_bias = 0.01
            top_lift = 0.005
            content_part = build_svg_part(
                base_thickness - inlay_depth - z_bias,
                inlay_depth + z_bias + top_lift
            )
    else:
        # Raised: preserve 0.2 mm visible height above base, but when pocket
        # carving is enabled, sink a small anchor into the base pocket to avoid
        # coplanar-body ambiguity for export formats.
        raised_height = 0.2
        anchor_depth = 0.04
        floor_clearance = 0.01
        if carve_pocket:
            pocket_depth = anchor_depth + floor_clearance
            cutter_part = build_svg_part(base_thickness - pocket_depth, pocket_depth)
            base.part -= cutter_part
            content_part = build_svg_part(
                base_thickness - anchor_depth,
                raised_height + anchor_depth
            )
        else:
            # Add a tiny anchor overlap in fallback mode to prevent coplanar seams.
            content_part = build_svg_part(
                base_thickness - anchor_depth,
                raised_height + anchor_depth
            )

    _set_shape_metadata(
        content_part,
        label="Content_White",
        material="Content_White",
        color=content_color
    )

    for i, solid in enumerate(_solid_components(base.part)):
        _set_shape_metadata(
            solid,
            label=f"Base_Black_{i + 1}",
            material="Base_Black",
            color=base_color
        )

    for i, solid in enumerate(_solid_components(content_part)):
        _set_shape_metadata(
            solid,
            label=f"Content_White_{i + 1}",
            material="Content_White",
            color=content_color
        )

    return base.part, content_part

def _apply_3mf_materials(three_mf_path: Path, base_item_count: int, content_item_count: int):
    ET.register_namespace("", THREEMF_CORE_NS)
    ET.register_namespace("m", THREEMF_MATERIAL_NS)

    with zipfile.ZipFile(three_mf_path, "r") as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}

    model_path = next((name for name in entries if name.lower().endswith(".model")), None)
    if not model_path:
        return

    root = ET.fromstring(entries[model_path])
    root.set("xmlns:BambuStudio", BAMBU_NS)
    resources = root.find(f"{{{THREEMF_CORE_NS}}}resources")
    build = root.find(f"{{{THREEMF_CORE_NS}}}build")
    if resources is None or build is None:
        return

    items = build.findall(f"{{{THREEMF_CORE_NS}}}item")
    required_items = max(0, int(base_item_count)) + max(0, int(content_item_count))
    if required_items <= 0 or len(items) < required_items:
        return

    base_items = items[:base_item_count]
    content_items = items[base_item_count:base_item_count + content_item_count]
    ordered_items = base_items + content_items

    max_resource_id = 0
    for node in list(resources):
        rid = node.attrib.get("id")
        if rid and rid.isdigit():
            max_resource_id = max(max_resource_id, int(rid))
    material_resource_id = str(max_resource_id + 1)

    basematerials = ET.Element(
        f"{{{THREEMF_MATERIAL_NS}}}basematerials",
        {"id": material_resource_id}
    )
    basematerials.append(
        ET.Element(
            f"{{{THREEMF_MATERIAL_NS}}}base",
            {"name": "Base_Black", "displaycolor": "#000000"}
        )
    )
    basematerials.append(
        ET.Element(
            f"{{{THREEMF_MATERIAL_NS}}}base",
            {"name": "Content_White", "displaycolor": "#FFFFFF"}
        )
    )
    resources.insert(0, basematerials)

    objects_by_id = {
        obj.attrib.get("id"): obj
        for obj in resources.findall(f"{{{THREEMF_CORE_NS}}}object")
    }

    for i, item in enumerate(base_items):
        obj_id = item.attrib.get("objectid")
        item.attrib["pid"] = material_resource_id
        item.attrib["pindex"] = "0"
        obj = objects_by_id.get(obj_id)
        if obj is not None:
            obj.attrib["name"] = f"Base_Black_{i + 1}"
            obj.attrib["pid"] = material_resource_id
            obj.attrib["pindex"] = "0"

    for i, item in enumerate(content_items):
        obj_id = item.attrib.get("objectid")
        item.attrib["pid"] = material_resource_id
        item.attrib["pindex"] = "1"
        obj = objects_by_id.get(obj_id)
        if obj is not None:
            obj.attrib["name"] = f"Content_White_{i + 1}"
            obj.attrib["pid"] = material_resource_id
            obj.attrib["pindex"] = "1"

    root.attrib["requiredextensions"] = "m"

    # Build a Bambu/Orca-friendly assembly object with explicit components.
    # Many slicers (including Orca/Bambu family) read part/extruder metadata
    # from this structure more reliably than core 3MF materials alone.
    max_obj_id = 0
    for obj_id in objects_by_id.keys():
        if obj_id and obj_id.isdigit():
            max_obj_id = max(max_obj_id, int(obj_id))
    assembly_obj_id = str(max_obj_id + 1)
    assembly_obj = ET.Element(
        f"{{{THREEMF_CORE_NS}}}object",
        {"id": assembly_obj_id, "type": "model"}
    )
    assembly_components = ET.SubElement(assembly_obj, f"{{{THREEMF_CORE_NS}}}components")

    def _parse_transform12(transform_text: str):
        if not transform_text:
            return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        vals = []
        for p in str(transform_text).split():
            try:
                vals.append(float(p))
            except Exception:
                pass
        if len(vals) != 12:
            return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        return vals

    def _fmt_num(v: float):
        s = f"{float(v):.9f}".rstrip("0").rstrip(".")
        return s if s else "0"

    component_infos = []
    global_min_x = None
    global_min_y = None
    global_min_z = None

    def _extract_object_bounds(obj_node):
        mesh_node = obj_node.find(f"{{{THREEMF_CORE_NS}}}mesh")
        if mesh_node is None:
            return None
        verts_node = mesh_node.find(f"{{{THREEMF_CORE_NS}}}vertices")
        if verts_node is None:
            return None
        xs, ys, zs = [], [], []
        for v in verts_node.findall(f"{{{THREEMF_CORE_NS}}}vertex"):
            try:
                xs.append(float(v.attrib.get("x", "0")))
                ys.append(float(v.attrib.get("y", "0")))
                zs.append(float(v.attrib.get("z", "0")))
            except Exception:
                continue
        if not xs:
            return None
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    object_bounds = {}
    for obj_id, obj in objects_by_id.items():
        if obj_id:
            b = _extract_object_bounds(obj)
            if b is not None:
                object_bounds[obj_id] = b

    for idx, item in enumerate(ordered_items):
        obj_id = item.attrib.get("objectid")
        if not obj_id:
            continue
        transform_text = item.attrib.get("transform")
        comp_attrs = {"objectid": obj_id}
        if transform_text:
            comp_attrs["transform"] = transform_text
        ET.SubElement(assembly_components, f"{{{THREEMF_CORE_NS}}}component", comp_attrs)
        transform_vals = _parse_transform12(transform_text)
        tx, ty, tz = transform_vals[9], transform_vals[10], transform_vals[11]
        matrix16 = " ".join([
            _fmt_num(transform_vals[0]), _fmt_num(transform_vals[1]), _fmt_num(transform_vals[2]), _fmt_num(tx),
            _fmt_num(transform_vals[3]), _fmt_num(transform_vals[4]), _fmt_num(transform_vals[5]), _fmt_num(ty),
            _fmt_num(transform_vals[6]), _fmt_num(transform_vals[7]), _fmt_num(transform_vals[8]), _fmt_num(tz),
            "0", "0", "0", "1"
        ])
        obj = objects_by_id.get(obj_id)
        part_name = obj.attrib.get("name") if obj is not None else ""
        if not part_name:
            part_name = f"Part_{idx + 1}"
        extruder = "1" if idx < base_item_count else "2"

        bounds = object_bounds.get(obj_id)
        if bounds is not None:
            min_x = bounds[0] + tx
            min_y = bounds[1] + ty
            min_z = bounds[2] + tz
            global_min_x = min_x if global_min_x is None else min(global_min_x, min_x)
            global_min_y = min_y if global_min_y is None else min(global_min_y, min_y)
            global_min_z = min_z if global_min_z is None else min(global_min_z, min_z)

        component_infos.append({
            "part_name": part_name,
            "object_id": obj_id,
            "matrix16": matrix16,
            "tx": _fmt_num(tx),
            "ty": _fmt_num(ty),
            "tz": _fmt_num(tz),
            "extruder": extruder
        })

    resources.append(assembly_obj)

    # Replace build with one assembly item (Bambu/Orca-style packaging).
    for item in list(build.findall(f"{{{THREEMF_CORE_NS}}}item")):
        build.remove(item)
    build_item_attrs = {"objectid": assembly_obj_id, "printable": "1"}
    # Move model into positive XY with a small margin so slicers using corner-origin
    # beds don't flag "object over boundary" on import.
    margin_xy = 5.0
    shift_x = 0.0
    shift_y = 0.0
    shift_z = 0.0
    if global_min_x is not None and global_min_x < margin_xy:
        shift_x = margin_xy - global_min_x
    if global_min_y is not None and global_min_y < margin_xy:
        shift_y = margin_xy - global_min_y
    if global_min_z is not None and global_min_z < 0.0:
        shift_z = -global_min_z
    if shift_x or shift_y or shift_z:
        build_item_attrs["transform"] = " ".join([
            "1", "0", "0",
            "0", "1", "0",
            "0", "0", "1",
            _fmt_num(shift_x),
            _fmt_num(shift_y),
            _fmt_num(shift_z)
        ])
    ET.SubElement(build, f"{{{THREEMF_CORE_NS}}}item", build_item_attrs)

    # Drop unreferenced wrapper objects emitted by the mesher. Orca can treat
    # those as extra parts (often black/default), which causes the duplicate
    # InfinityGrid_Label_* entries and wrong color assignment in the object tree.
    keep_object_ids = {assembly_obj_id}
    for info in component_infos:
        keep_object_ids.add(info["object_id"])
    for obj in list(resources.findall(f"{{{THREEMF_CORE_NS}}}object")):
        oid = obj.attrib.get("id")
        if oid not in keep_object_ids:
            resources.remove(obj)

    # Renumber kept objects contiguously (1..N for mesh parts, N+1 for assembly).
    # Orca/Bambu metadata uses source_object_id that aligns with contiguous object
    # numbering in common project files.
    mesh_old_ids = [info["object_id"] for info in component_infos]
    id_map = {old_id: str(i + 1) for i, old_id in enumerate(mesh_old_ids)}
    new_assembly_id = str(len(mesh_old_ids) + 1)
    id_map[assembly_obj_id] = new_assembly_id

    for obj in resources.findall(f"{{{THREEMF_CORE_NS}}}object"):
        old_id = obj.attrib.get("id")
        if old_id in id_map:
            obj.attrib["id"] = id_map[old_id]

    for comp in assembly_components.findall(f"{{{THREEMF_CORE_NS}}}component"):
        old_ref = comp.attrib.get("objectid")
        if old_ref in id_map:
            comp.attrib["objectid"] = id_map[old_ref]

    for item in build.findall(f"{{{THREEMF_CORE_NS}}}item"):
        old_ref = item.attrib.get("objectid")
        if old_ref in id_map:
            item.attrib["objectid"] = id_map[old_ref]

    assembly_obj_id = new_assembly_id
    for idx, info in enumerate(component_infos):
        mapped_obj_id = id_map.get(info["object_id"], info["object_id"])
        info["mapped_object_id"] = mapped_obj_id
        try:
            info["source_object_id"] = str(int(mapped_obj_id) - 1)
        except Exception:
            info["source_object_id"] = str(idx)

    def _upsert_model_metadata(meta_name: str, meta_value: str):
        md_tag = f"{{{THREEMF_CORE_NS}}}metadata"
        for node in root.findall(md_tag):
            if node.attrib.get("name") == meta_name:
                node.text = meta_value
                return
        new_node = ET.Element(md_tag, {"name": meta_name})
        new_node.text = meta_value
        resources_node = root.find(f"{{{THREEMF_CORE_NS}}}resources")
        if resources_node is not None:
            insert_at = list(root).index(resources_node)
            root.insert(insert_at, new_node)
        else:
            root.append(new_node)

    _upsert_model_metadata("Application", "OrcaSlicer")
    _upsert_model_metadata("BambuStudio:3mfVersion", "1")

    entries[model_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # Add Orca/Bambu project metadata files for part-to-extruder mapping.
    model_settings_root = ET.Element("config")
    object_cfg = ET.SubElement(model_settings_root, "object", {"id": assembly_obj_id})
    ET.SubElement(object_cfg, "metadata", {"key": "name", "value": "InfinityGrid_Label"})
    ET.SubElement(object_cfg, "metadata", {"key": "extruder", "value": "1"})
    for idx, info in enumerate(component_infos, start=1):
        part_node = ET.SubElement(
            object_cfg,
            "part",
            {"id": str(idx), "subtype": "normal_part"}
        )
        ET.SubElement(part_node, "metadata", {"key": "name", "value": info["part_name"]})
        ET.SubElement(part_node, "metadata", {"key": "matrix", "value": info["matrix16"]})
        ET.SubElement(part_node, "metadata", {"key": "source_file", "value": "multicolor_label.3mf"})
        ET.SubElement(part_node, "metadata", {"key": "source_object_id", "value": info["source_object_id"]})
        ET.SubElement(part_node, "metadata", {"key": "source_volume_id", "value": "0"})
        ET.SubElement(part_node, "metadata", {"key": "source_offset_x", "value": info["tx"]})
        ET.SubElement(part_node, "metadata", {"key": "source_offset_y", "value": info["ty"]})
        ET.SubElement(part_node, "metadata", {"key": "source_offset_z", "value": info["tz"]})
        ET.SubElement(part_node, "metadata", {"key": "extruder", "value": info["extruder"]})

    plate_node = ET.SubElement(model_settings_root, "plate")
    ET.SubElement(plate_node, "metadata", {"key": "plater_id", "value": "1"})
    ET.SubElement(plate_node, "metadata", {"key": "plater_name", "value": ""})
    ET.SubElement(plate_node, "metadata", {"key": "locked", "value": "false"})
    ET.SubElement(plate_node, "metadata", {"key": "filament_map_mode", "value": "Auto For Flush"})
    ET.SubElement(plate_node, "metadata", {"key": "filament_maps", "value": "1 2"})

    project_settings = {
        "filament_colour": ["#000000", "#FFFFFF"],
        "extruder_colour": ["#000000"],
        "filament_type": ["PLA", "PLA"],
        "filament_ids": ["", ""],
        "filament_settings_id": ["Base_Black", "Content_White"],
        "default_filament_profile": ["Base_Black", "Content_White"],
        # Orca/Bambu validates relative E mode against layer-change reset.
        # Provide the common reset snippet to avoid import/slice validation errors.
        "before_layer_change_gcode": ";BEFORE_LAYER_CHANGE\n;[layer_z]\nG92 E0\n",
        "use_relative_e_distances": "1"
    }

    slice_info_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<config>\n"
        "  <header>\n"
        "    <header_item key=\"X-BBL-Client-Type\" value=\"slicer\"/>\n"
        "    <header_item key=\"X-BBL-Client-Version\" value=\"\"/>\n"
        "  </header>\n"
        "</config>\n"
    )

    entries["Metadata/model_settings.config"] = ET.tostring(
        model_settings_root, encoding="utf-8", xml_declaration=True
    )
    entries["Metadata/project_settings.config"] = json.dumps(
        project_settings, indent=2
    ).encode("utf-8")
    entries["Metadata/slice_info.config"] = slice_info_xml.encode("utf-8")

    with zipfile.ZipFile(three_mf_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, payload in entries.items():
            zout.writestr(name, payload)

def build_step_worker(svg_text, w, h, sty, label_shape, queue):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            svg_path = temp_dir_path / "label_content.svg"
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg_text)
            try:
                base_part, content_part = _build_label_parts_from_svg(
                    svg_path, w, h, sty, label_shape, carve_pocket=True
                )
            except Exception as carved_err:
                print(f"Warning: STEP carved geometry failed, retrying without pocket carve: {carved_err}")
                base_part, content_part = _build_label_parts_from_svg(
                    svg_path, w, h, sty, label_shape, carve_pocket=False
                )
            base_solids = _solid_components(base_part)
            content_solids = _solid_components(content_part)
            if len(base_solids) == 0:
                raise ValueError("STEP export generated no base geometry")
            if len(content_solids) == 0:
                raise ValueError("STEP export generated no label content geometry")

            my_assembly = Compound(
                label="InfinityGrid_Label",
                children=base_solids + content_solids
            )
            step_path = temp_dir_path / "multicolor_label.step"
            export_step(my_assembly, str(step_path))
            with open(step_path, "rb") as f:
                queue.put(("ok", f.read()))
    except Exception as e:
        queue.put(("err", str(e)))

def _add_part_to_mesher(mesh: Mesher, shape):
    solids = []
    try:
        solids = list(shape.solids())
    except Exception:
        solids = []

    ld, ad = MESHER_LINEAR_DEFLECTION, MESHER_ANGULAR_DEFLECTION
    if solids:
        for solid in solids:
            mesh.add_shape(solid, ld, ad)
        return len(solids)

    mesh.add_shape(shape, ld, ad)
    return 1

def build_3mf_worker(svg_text, w, h, sty, label_shape, queue):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            svg_path = temp_dir_path / "label_content.svg"
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg_text)
            three_mf_path = temp_dir_path / "multicolor_label.3mf"
            attempt_errors = []

            # Try no-pocket first: skips expensive base boolean and usually meshes faster;
            # pocket carve is still attempted if the fast path fails.
            for carve_pocket in (False, True):
                try:
                    base_part, content_part = _build_label_parts_from_svg(
                        svg_path, w, h, sty, label_shape, carve_pocket=carve_pocket
                    )
                    base_solids = _solid_components(base_part)
                    content_solids = _solid_components(content_part)
                    if len(base_solids) == 0:
                        raise ValueError("3MF export generated no base geometry")
                    if len(content_solids) == 0:
                        raise ValueError("3MF export generated no label content geometry")

                    mesh = Mesher()
                    _add_part_to_mesher(mesh, base_part)
                    _add_part_to_mesher(mesh, content_part)
                    mesh.write(three_mf_path)
                    _apply_3mf_materials(
                        three_mf_path,
                        base_item_count=_mesher_item_count(base_part),
                        content_item_count=_mesher_item_count(content_part),
                    )

                    # Validate archive integrity before returning a "success" payload.
                    with zipfile.ZipFile(three_mf_path, "r") as zf:
                        if zf.testzip() is not None:
                            raise ValueError("3MF archive failed integrity test")
                        has_model = any(name.lower().endswith(".model") for name in zf.namelist())
                        if not has_model:
                            raise ValueError("3MF archive missing .model entry")

                    with open(three_mf_path, "rb") as f:
                        queue.put(("ok", f.read()))
                    return
                except Exception as attempt_err:
                    mode_label = "carved" if carve_pocket else "no-pocket"
                    attempt_errors.append(f"{mode_label}: {attempt_err}")
                    print(f"Warning: 3MF {mode_label} attempt failed: {attempt_err}")

            raise ValueError(f"3MF export failed for all build modes. {' | '.join(attempt_errors)}")
    except Exception as e:
        queue.put(("err", str(e)))

def build_stl_preview_worker(svg_text, w, h, sty, label_shape, queue):
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            svg_path = temp_dir_path / "label_content.svg"
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg_text)

            base_part, content_part = _build_label_parts_from_svg(
                svg_path, w, h, sty, label_shape, carve_pocket=False
            )
            stl_path = temp_dir_path / "preview_label.stl"
            wrote_preview = False

            # Fast path: Mesher with both bodies.
            try:
                preview_mesh = Mesher()
                _add_part_to_mesher(preview_mesh, base_part)
                _add_part_to_mesher(preview_mesh, content_part)
                preview_mesh.write(stl_path)
                wrote_preview = True
            except Exception as mesher_err:
                print(f"Warning: Mesher preview failed, trying export_stl fallback: {mesher_err}")

            # Robust fallback: build123d STL export can succeed when mesher rejects
            # dense icon geometry.
            if not wrote_preview:
                try:
                    preview_assembly = Compound(
                        label="Preview_Label",
                        children=_solid_components(base_part) + _solid_components(content_part),
                    )
                    export_stl(preview_assembly, str(stl_path))
                    wrote_preview = True
                except Exception as export_err:
                    print(f"Warning: export_stl preview failed, trying base-only fallback: {export_err}")

            if not wrote_preview:
                base_only_mesh = Mesher()
                _add_part_to_mesher(base_only_mesh, base_part)
                base_only_mesh.write(stl_path)
                print("Warning: Using base-only STL preview fallback")

            with open(stl_path, "rb") as f:
                queue.put(("ok", f.read()))
    except Exception as e:
        queue.put(("err", str(e)))

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_icon_files():
    """Get all SVG files from Icons_SVG folder."""
    if not ICONS_FOLDER.exists():
        print(f"Icons folder not found: {ICONS_FOLDER}")
        return []
    files = [f.name for f in ICONS_FOLDER.iterdir() if f.suffix.lower() == ".svg"]
    return sorted(files)

def get_icons_version():
    """Version token that changes when icon files are added/updated/removed."""
    if not ICONS_FOLDER.exists():
        return "0-0"
    latest_mtime_ns = 0
    count = 0
    for f in ICONS_FOLDER.iterdir():
        if f.suffix.lower() == ".svg":
            count += 1
            latest_mtime_ns = max(latest_mtime_ns, f.stat().st_mtime_ns)
    if ICON_TAGS_FILE.exists():
        latest_mtime_ns = max(latest_mtime_ns, ICON_TAGS_FILE.stat().st_mtime_ns)
    return f"{count}-{latest_mtime_ns}"

def get_icon_tags():
    """Load icon category metadata from icon_tags.json."""
    if not ICON_TAGS_FILE.exists():
        return []
    try:
        payload = json.loads(ICON_TAGS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read icon tags file: {exc}")
        return []

    if isinstance(payload, dict):
        entries = payload.get("icons", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    if not isinstance(entries, list):
        return []

    tags = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        file_name = str(row.get("fileName", "")).strip()
        if not file_name:
            continue
        tags.append({
            "mainCategory": str(row.get("mainCategory", "uncategorized")).lower(),
            "subCategory": str(row.get("subCategory", "general")).lower(),
            "fileName": file_name
        })
    return tags

@app.get("/api/icons")
async def list_icons():
    """API endpoint to list available icons."""
    icons = get_icon_files()
    tags = get_icon_tags()
    return JSONResponse(content={"files": icons, "tags": tags, "version": get_icons_version()})

@app.post("/api/export_step")
async def export_step_endpoint(
    svg_file: UploadFile = File(...),
    width: float = Form(...),
    height: float = Form(...),
    style: str = Form("flush"),
    label_shape: str = Form("classic")
):
    """
    Receives SVG File and dimensions, uses Build123d to create a Multi-Body Assembly,
    and returns a downloadable .step file.
    """
    svg_content = ""
    try:
        # Read the uploaded file content bytes
        svg_content_bytes = await svg_file.read()
        svg_content = svg_content_bytes.decode('utf-8')
        
        class _Q:
            def __init__(self):
                self.items = []
            def put(self, value):
                self.items.append(value)

        q = _Q()
        await run_export_worker(build_step_worker, svg_content, width, height, style, label_shape, q)
        if not q.items:
            raise HTTPException(status_code=500, detail="STEP export failed without details")
        status, payload = q.items[0]
        if status != "ok":
            raise HTTPException(status_code=500, detail=payload)
        step_data = payload
                
        # Return the STEP file as a downloadable response
        from fastapi import Response
        return Response(
            content=step_data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=multicolor_label.step"}
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        _save_failed_svg_debug("failed_step.svg", svg_content)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/export_3mf")
async def export_3mf_endpoint(
    svg_file: UploadFile = File(...),
    width: float = Form(...),
    height: float = Form(...),
    style: str = Form("flush"),
    label_shape: str = Form("classic")
):
    """
    Receives SVG File and dimensions, builds base/content as separate meshes,
    and returns a downloadable .3mf file with black/white material assignment.
    """
    svg_content = ""
    try:
        svg_content_bytes = await svg_file.read()
        svg_content = svg_content_bytes.decode("utf-8")

        class _Q:
            def __init__(self):
                self.items = []

            def put(self, value):
                self.items.append(value)

        q = _Q()
        await run_export_worker(build_3mf_worker, svg_content, width, height, style, label_shape, q)
        if not q.items:
            raise HTTPException(status_code=500, detail="3MF export failed without details")

        status, payload = q.items[0]
        if status != "ok":
            raise HTTPException(status_code=500, detail=payload)

        from fastapi import Response
        return Response(
            content=payload,
            media_type="model/3mf",
            headers={"Content-Disposition": "attachment; filename=multicolor_label.3mf"}
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        _save_failed_svg_debug("failed_3mf.svg", svg_content)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/preview_stl")
async def preview_stl_endpoint(
    svg_file: UploadFile = File(...),
    width: float = Form(...),
    height: float = Form(...),
    style: str = Form("flush"),
    label_shape: str = Form("classic")
):
    svg_content = ""
    try:
        svg_content_bytes = await svg_file.read()
        svg_content = svg_content_bytes.decode("utf-8")

        class _Q:
            def __init__(self):
                self.items = []

            def put(self, value):
                self.items.append(value)

        q = _Q()
        await run_export_worker(build_stl_preview_worker, svg_content, width, height, style, label_shape, q)
        if not q.items:
            raise HTTPException(status_code=500, detail="STL preview failed without details")

        status, payload = q.items[0]
        if status != "ok":
            raise HTTPException(status_code=500, detail=payload)

        return Response(
            content=payload,
            media_type="model/stl"
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        _save_failed_svg_debug("failed_preview_stl.svg", svg_content)
        raise HTTPException(status_code=500, detail=str(e))

# Serve the Icons directory
app.mount("/icons", StaticFiles(directory=str(ICONS_FOLDER)), name="icons")

# Serve the 'assets' directory properly
assets_dir = BASE_DIR / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    favicon_path = BASE_DIR / "favicon.svg"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/svg+xml")
    app_icon_path = BASE_DIR / "AppIcon.png"
    if app_icon_path.exists():
        return FileResponse(app_icon_path, media_type="image/png")
    return Response(status_code=204)

@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    file_path = BASE_DIR / "favicon.svg"
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/AppIcon.png", include_in_schema=False)
async def app_icon_png():
    file_path = BASE_DIR / "AppIcon.png"
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/")
async def serve_index():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Index.html not found")


if __name__ == "__main__":
    icons = get_icon_files()
    print()
    print("InfinityGrid Sticker Designer (FastAPI + Build123d)")
    print("-" * 50)
    print(f"Server running at: http://{HOST}:{PORT}")
    print(f"Icons folder: {ICONS_FOLDER}")
    print(f"Icons found: {len(icons)}")

    if len(icons) == 0:
        print()
        print("WARNING: No SVG files found in Icons_SVG folder.")

    print()
    print("Press Ctrl+C to stop.")
    print()

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
