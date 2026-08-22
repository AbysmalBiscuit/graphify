"""Godot extractors: GDScript (.gd) via tree-sitter, scene/resource files (.tscn/.tres) via the text format."""
from __future__ import annotations

import os
import re
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id, _read_text

# Godot global functions and value-type constructors. Calls to these are engine
# built-ins, never user code — emitting raw_calls for them just feeds the
# cross-file resolver names that can only mis-bind to a coincidentally
# same-named user symbol.
_GDSCRIPT_BUILTINS: frozenset[str] = frozenset({
    "preload", "load",
    "print", "prints", "printt", "printerr", "print_rich", "print_debug",
    "push_error", "push_warning",
    "str", "int", "float", "bool", "len", "range", "abs", "sign",
    "min", "max", "clamp", "clampf", "clampi", "lerp", "lerpf", "inverse_lerp",
    "round", "floor", "ceil", "snapped", "sqrt", "pow", "exp", "log",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "deg_to_rad", "rad_to_deg", "is_equal_approx", "is_zero_approx",
    "randf", "randi", "randf_range", "randi_range", "randomize", "seed",
    "is_instance_valid", "is_instance_of", "instance_from_id",
    "typeof", "type_exists", "weakref", "hash",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i",
    "Color", "Rect2", "Rect2i", "Plane", "Quaternion", "Basis", "AABB",
    "Transform2D", "Transform3D", "Projection", "NodePath", "StringName",
    "Callable", "Signal", "RID", "Array", "Dictionary",
    "PackedByteArray", "PackedInt32Array", "PackedInt64Array",
    "PackedFloat32Array", "PackedFloat64Array", "PackedStringArray",
    "PackedVector2Array", "PackedVector3Array", "PackedColorArray",
})


def _godot_project_root(path: Path) -> Path | None:
    """Directory containing project.godot, searched upward from the file."""
    for directory in (path.parent, *path.parent.parents):
        try:
            if (directory / "project.godot").is_file():
                return directory
        except OSError:
            continue
    return None


# Value types: a type annotation resolving to one of these is never a
# same-corpus cross-reference, and the annotation volume in a typed GDScript
# codebase would otherwise turn them into thousand-edge hub nodes (Global
# Constraint 4). `void` isn't part of Godot's value-type vocabulary but is
# the return-type annotation on every function with no return value, making
# it the worst-case hub of all if left unfiltered.
_GDSCRIPT_VALUE_TYPES: frozenset[str] = frozenset({
    "int", "float", "bool", "String", "StringName", "NodePath", "Variant",
    "Callable", "Signal", "RID", "Array", "Dictionary",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i",
    "Color", "Rect2", "Rect2i", "Plane", "Quaternion", "Basis", "AABB",
    "Transform2D", "Transform3D", "Projection", "void",
}) | {name for name in _GDSCRIPT_BUILTINS if name.startswith("Packed")}


_CLASS_NAME_RE = re.compile(r"^class_name\s+([A-Za-z_]\w*)", re.MULTILINE)
_class_name_maps: dict[Path, dict[str, Path]] = {}


def _class_name_map(path: Path) -> dict[str, Path]:
    """Project-wide `class_name X` -> declaring-file map, memoized per project
    root so a corpus of N scripts pays for one line-scan pass, not N.

    Read with a regex, not tree-sitter: every file contributes at most the one
    line this needs, and running the grammar over the whole project just to
    find it would dwarf the cost of the extraction this map serves.
    """
    root = _godot_project_root(path) or path.parent
    try:
        root = root.resolve()
    except OSError:
        pass
    cached = _class_name_maps.get(root)
    if cached is not None:
        return cached

    mapping: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".gd"):
                continue
            gd_path = Path(dirpath) / name
            try:
                text = gd_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _CLASS_NAME_RE.search(text)
            if m and m.group(1) not in mapping:
                mapping[m.group(1)] = gd_path

    _class_name_maps[root] = mapping
    return mapping


def _resolve_res_path(res_path: str, path: Path) -> Path | None:
    """Resolve a Godot resource reference to an existing file.

    ``res://`` paths are anchored at the project root (the directory containing
    project.godot); without a project.godot, fall back to the first ancestor
    directory under which the relative path exists. Plain relative paths
    (``preload("../_Components/widget.tscn")``) resolve against the referencing
    file's directory. A reference is dropped when the file doesn't exist (a
    phantom node would carry a path found nowhere in the corpus) or when it
    escapes the project — a corpus file can never mint a node outside the
    scanned tree. ``uid://`` / ``user://`` references are not resolvable from
    text and return None.
    """
    if res_path.startswith("res://"):
        rel = res_path[len("res://"):]
        if not rel or ".." in rel.split("/"):
            return None
        root = _godot_project_root(path)
        if root is not None:
            candidate = root / rel
            try:
                return candidate if candidate.is_file() else None
            except OSError:
                return None
        for directory in (path.parent, *path.parent.parents):
            candidate = directory / rel
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None
    if "://" in res_path:
        return None
    # Relative to the referencing file's directory.
    candidate = path.parent / res_path
    try:
        if not candidate.is_file():
            return None
    except OSError:
        return None
    root = _godot_project_root(path)
    if root is not None:
        try:
            candidate.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return None
    elif ".." in res_path.replace("\\", "/").split("/"):
        # No project root to bound the walk-up, so reject parent traversal.
        return None
    return candidate


# ── GDScript extractor (custom walk) ──────────────────────────────────────────

def extract_gdscript(path: Path) -> dict:
    """Extract classes, functions, signals, vars, extends, preloads, and calls from a .gd file."""
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except ImportError:
        return {"nodes": [], "edges": [],
                "error": "tree-sitter-language-pack not installed (pip install graphifyy[godot])"}
    try:
        parser = Parser(get_language("gdscript"))
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    raw_calls: list[dict] = []
    seen_ids: set[str] = set()
    signal_names: set[str] = set()
    function_bodies: list[tuple[str, object]] = []

    def add_node(nid: str, label: str, line: int, *, file_type: str = "code") -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": file_type,
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None, target_file: str | None = None) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        if target_file:
            # Routing hint for the colliding-id pass: a .gd and its same-stem
            # .tscn share a file id, and this says which file the edge targets.
            edge["target_file"] = target_file
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't defined in this file, so this is a cross-file reference
            # (e.g. `extends Enemy` where Enemy is another script's class_name). Emit
            # a SOURCELESS stub so the corpus-level rewire can collapse it onto the
            # real definition. A sourced stub here makes _disambiguate_colliding_node_ids
            # bake the referencing file's path (with extension) into the id and blocks
            # the rewire, which is the phantom-duplicate-node bug (#1402).
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    def add_file_ref_node(target: Path, line: int) -> tuple[str, str]:
        """Node for another project file (a res:// target). Normalized in the same
        path form the extractor inputs use (relative stays relative) so the
        id-remap pass collapses it onto that file's own node when it is part of
        the corpus, and an out-of-corpus reference (e.g. into an excluded noise
        dir) never bakes a machine-specific absolute path into its id."""
        norm_target = os.path.normpath(str(target))
        nid = _make_id(norm_target)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": target.name,
                "file_type": "code",
                "source_file": norm_target,
                "source_location": f"L{line}",
            })
        return nid, norm_target

    class_name_map = _class_name_map(path)

    def _type_identifiers(node):
        if node.type == "identifier":
            yield node
        for child in node.children:
            yield from _type_identifiers(child)

    def resolve_type(type_node, scope_nid: str, context: str, line: int) -> None:
        """`var x: Foo`, `func f(a: Foo)`, `func f() -> Foo` — Foo becomes a
        `references` edge. A generic (`Array[Foo]`) or qualified (`Outer.Inner`)
        annotation takes its last identifier segment, same as `handle_extends`."""
        idents = list(_type_identifiers(type_node))
        if not idents:
            return
        name = _read_text(idents[-1], source)
        if not name or name in _GDSCRIPT_VALUE_TYPES:
            return
        local_nid = _make_id(stem, name)
        if local_nid in seen_ids:
            add_edge(scope_nid, local_nid, "references", line, context=context)
            return
        mapped = class_name_map.get(name)
        if mapped is not None:
            add_edge(scope_nid, _make_id(_file_stem(mapped), name), "references", line,
                     context=context, target_file=os.path.normpath(str(mapped)))
            return
        concept_nid = _make_id("godot", name)
        add_node(concept_nid, name, line, file_type="concept")
        add_edge(scope_nid, concept_nid, "references", line, context=context)

    # `class_name Foo` names the file's implicit class: it is the identifier other
    # scripts use (`extends Foo`, `Foo.bar()`), so it becomes the real node that
    # cross-file stubs rewire onto, and members hang off it instead of the file.
    class_name_nid: str | None = None
    for child in root.children:
        if child.type == "class_name_statement":
            name_node = child.child_by_field_name("name")
            if name_node:
                cls_name = _read_text(name_node, source)
                line = child.start_point[0] + 1
                class_name_nid = _make_id(stem, cls_name)
                add_node(class_name_nid, cls_name, line)
                add_edge(file_nid, class_name_nid, "defines", line)
            break
    top_scope = class_name_nid or file_nid

    def handle_extends(node, scope_nid: str) -> None:
        line = node.start_point[0] + 1
        for child in node.children:
            if child.type == "type":
                # `extends Node3D` / `extends Foo.Inner` — inherit from the (last)
                # named type; a stub if it isn't defined in this file.
                base_name = _read_text(child, source).split(".")[-1].strip()
                if base_name:
                    add_edge(scope_nid, ensure_named_node(base_name, line), "inherits", line)
                return
            if child.type == "string":
                # `extends "res://base.gd"` — inherit from the script file itself.
                res_path = _read_text(child, source).strip("\"'")
                target = _resolve_res_path(res_path, path)
                if target is not None:
                    target_nid, abs_target = add_file_ref_node(target, line)
                    add_edge(scope_nid, target_nid, "inherits", line, target_file=abs_target)
                    add_edge(file_nid, target_nid, "imports", line, context="extends",
                             target_file=abs_target)
                return

    def walk(node, scope_nid: str) -> None:
        t = node.type
        line = node.start_point[0] + 1

        if t == "class_name_statement":
            return  # handled in the pre-scan above

        if t == "extends_statement":
            handle_extends(node, scope_nid)
            return

        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                cls_name = _read_text(name_node, source)
                cls_nid = _make_id(stem, cls_name)
                add_node(cls_nid, cls_name, line)
                add_edge(scope_nid, cls_nid, "defines", line)
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        walk(child, cls_nid)
            return

        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(scope_nid, func_nid, "defines", line)
                params = node.child_by_field_name("parameters")
                if params:
                    for param in params.children:
                        if param.type == "typed_parameter":
                            param_type = param.child_by_field_name("type")
                            if param_type:
                                resolve_type(param_type, func_nid, "parameter_type",
                                             param.start_point[0] + 1)
                return_type = node.child_by_field_name("return_type")
                if return_type:
                    resolve_type(return_type, func_nid, "return_type", line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t in ("variable_statement", "const_statement"):
            name_node = node.child_by_field_name("name")
            if name_node:
                var_name = _read_text(name_node, source)
                var_nid = _make_id(stem, var_name)
                add_node(var_nid, var_name, line)
                add_edge(scope_nid, var_nid, "defines", line)
                var_type = node.child_by_field_name("type")
                if var_type:
                    resolve_type(var_type, var_nid, "var_type", line)
            return

        if t == "signal_statement":
            name_node = node.child_by_field_name("name")
            if name_node:
                sig_name = _read_text(name_node, source)
                sig_nid = _make_id(stem, sig_name)
                signal_names.add(sig_name)
                add_node(sig_nid, f"signal {sig_name}", line)
                add_edge(scope_nid, sig_nid, "defines", line)
            return

        if t == "enum_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                enum_name = _read_text(name_node, source)
                enum_nid = _make_id(stem, enum_name)
                add_node(enum_nid, enum_name, line)
                add_edge(scope_nid, enum_nid, "defines", line)
            return

        for child in node.children:
            walk(child, scope_nid)

    def connect_handler_args(args_node, func_nid: str, line: int) -> None:
        """`sig.connect(_on_sig)` — bare-identifier callback args that name an
        in-file function become calls edges (the handler is invoked via signal)."""
        for arg in args_node.children:
            if arg.type == "identifier":
                handler = _read_text(arg, source)
                handler_nid = _make_id(stem, handler)
                if handler_nid in seen_ids:
                    add_edge(func_nid, handler_nid, "calls", line, context="signal")

    def walk_calls(node, func_nid: str) -> None:
        t = node.type
        if t in ("function_definition", "class_definition"):
            return
        line = node.start_point[0] + 1

        if t == "call" and node.children and node.children[0].type == "identifier":
            callee = _read_text(node.children[0], source)
            args = node.child_by_field_name("arguments")
            if callee == "emit_signal" and args is not None:
                first_str = next((c for c in args.children if c.type == "string"), None)
                if first_str is not None:
                    sig_name = _read_text(first_str, source).strip("\"'")
                    if sig_name in signal_names:
                        add_edge(func_nid, _make_id(stem, sig_name), "calls", line,
                                 context="signal")
            elif callee == "connect" and args is not None:
                connect_handler_args(args, func_nid, line)
            elif callee not in _GDSCRIPT_BUILTINS:
                callee_nid = _make_id(stem, callee)
                if callee_nid in seen_ids:
                    add_edge(func_nid, callee_nid, "calls", line, context="call")
                else:
                    raw_calls.append({
                        "caller_nid": func_nid,
                        "callee": callee,
                        "is_member_call": False,
                        "source_file": str_path,
                        "source_location": f"L{line}",
                    })

        elif t == "attribute":
            receiver = node.children[0] if node.children else None
            attr_call = next((c for c in node.children if c.type == "attribute_call"), None)
            if attr_call is not None and attr_call.children:
                method_node = attr_call.children[0]
                method = _read_text(method_node, source) if method_node.type == "identifier" else None
                receiver_name = (
                    _read_text(receiver, source) if receiver is not None
                    and receiver.type == "identifier" else None
                )
                args = attr_call.child_by_field_name("arguments")
                if receiver_name in signal_names and method in ("emit", "connect"):
                    # `health_changed.emit(...)` / `died.connect(_on_died)` — the
                    # signal itself is the target; connect also wires its handlers.
                    add_edge(func_nid, _make_id(stem, receiver_name), "calls", line,
                             context="signal")
                    if method == "connect" and args is not None:
                        connect_handler_args(args, func_nid, line)
                elif method == "connect" and args is not None:
                    connect_handler_args(args, func_nid, line)
                elif method and method not in _GDSCRIPT_BUILTINS:
                    method_nid = _make_id(stem, method)
                    if method_nid in seen_ids:
                        add_edge(func_nid, method_nid, "calls", line, context="call")
                    else:
                        raw_calls.append({
                            "caller_nid": func_nid,
                            "callee": method,
                            "is_member_call": True,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                        })

        for child in node.children:
            walk_calls(child, func_nid)

    def scan_res_imports(node) -> None:
        """preload("res://...") / load("res://...") anywhere in the file — an
        `imports` edge from the file to the referenced script/scene/resource."""
        if (node.type == "call" and node.children
                and node.children[0].type == "identifier"
                and _read_text(node.children[0], source) in ("preload", "load")):
            args = node.child_by_field_name("arguments")
            if args is not None:
                first_str = next((c for c in args.children if c.type == "string"), None)
                if first_str is not None:
                    res_path = _read_text(first_str, source).strip("\"'")
                    target = _resolve_res_path(res_path, path)
                    if target is not None:
                        line = node.start_point[0] + 1
                        target_nid, abs_target = add_file_ref_node(target, line)
                        add_edge(file_nid, target_nid, "imports", line, context="preload",
                                 target_file=abs_target)
        for child in node.children:
            scan_res_imports(child)

    walk(root, top_scope)
    for func_nid, body_node in function_bodies:
        walk_calls(body_node, func_nid)
    scan_res_imports(root)

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls}


# ── Godot scene / resource extractor (.tscn / .tres text format) ─────────────

_GODOT_SECTION_RE = re.compile(r"^\[(\w+)\s*(.*?)\]\s*$")
_GODOT_ATTR_RE = re.compile(r'([\w/]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|(\S+))')
_GODOT_EXT_REF_RE = re.compile(r'ExtResource\(\s*"?([^")]+?)"?\s*\)')
_GODOT_SUB_REF_RE = re.compile(r'SubResource\(\s*"?([^")]+?)"?\s*\)')
_GODOT_PROPERTY_RE = re.compile(r"^([\w/]+)\s*=\s*(.*)$")
_GODOT_QUOTED_RE = re.compile(r'^"([^"]*)"$')
_GODOT_NODEPATH_RE = re.compile(r'^NodePath\(\s*"([^"]*)"\s*\)$')

# Per-instance engine transform and editor state: present on nearly every node,
# carries no architectural meaning, and would otherwise dominate the
# `properties` attribute.
_GODOT_NOISE_PROPERTY_KEYS: frozenset[str] = frozenset({
    "transform", "position", "rotation", "scale", "size",
    "offset_left", "offset_top", "offset_right", "offset_bottom",
    "anchor_left", "anchor_top", "anchor_right", "anchor_bottom",
    "global_position", "visible", "z_index", "modulate", "self_modulate",
})
_GODOT_PROPERTIES_CAP = 500


def _godot_section_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _GODOT_ATTR_RE.finditer(raw):
        attrs[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return attrs


def extract_godot_scene(path: Path) -> dict:
    """Extract the node tree, ext_resource references, script attachments, scene
    instances, and signal connections from a Godot .tscn/.tres file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}"}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    raw_calls: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str, str | None]] = set()

    def add_node(nid: str, label: str, line: int, *,
                 file_type: str = "code", source_file: str = str_path) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append({
            "id": nid,
            "label": label,
            "file_type": file_type,
            "source_file": source_file,
            "source_location": f"L{line}",
        })

    def add_edge(src_nid: str, tgt_nid: str, relation: str, line: int,
                 context: str | None = None, target_file: str | None = None) -> None:
        key = (src_nid, tgt_nid, relation, context)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edge = {
            "source": src_nid,
            "target": tgt_nid,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        if target_file:
            # Routing hint for the colliding-id pass: a .gd and its same-stem
            # .tscn share a file id, and this says which file the edge targets.
            edge["target_file"] = target_file
        edges.append(edge)

    def add_file_ref_node(target: Path, line: int) -> tuple[str, str]:
        norm_target = os.path.normpath(str(target))
        nid = _make_id(norm_target)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": target.name,
                "file_type": "code",
                "source_file": norm_target,
                "source_location": f"L{line}",
            })
        return nid, norm_target

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    ext_resources: dict[str, tuple[str, str, Path]] = {}  # id -> (node id, abs path, resolved path)
    sub_resources: dict[str, str] = {}  # sub_resource id -> node id
    node_path_to_nid: dict[str, str] = {}  # scene-tree path ("." = root) -> node id
    root_nid: str | None = None
    # SubResource(...) refs may name an id whose [sub_resource] header hasn't
    # been read yet, so resolution happens after the loop.
    pending_sub_refs: list[tuple[str, str, int]] = []
    # A section's script may be declared before or after the properties it
    # governs, so member-binding edges (section_nid -> script member) also wait
    # until the whole file has been read.
    section_scripts: dict[str, Path] = {}
    pending_members: list[tuple[str, str, int]] = []
    section_properties: dict[str, dict] = {}

    def add_property(nid: str, key: str, value: str) -> None:
        if key in _GODOT_NOISE_PROPERTY_KEYS or key.startswith(("metadata/", "editor_")):
            return
        cleaned = value[1:] if value.startswith("&") else value
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
            cleaned = cleaned[1:-1]
        if len(cleaned) > 120:
            return
        state = section_properties.setdefault(nid, {"text": "", "capped": False})
        if state["capped"]:
            return
        piece = f"{key}={cleaned}"
        addition = f"; {piece}" if state["text"] else piece
        if len(state["text"]) + len(addition) > _GODOT_PROPERTIES_CAP:
            state["text"] += "; ..." if state["text"] else "..."
            state["capped"] = True
            return
        state["text"] += addition

    # Per-section state: only [node] / [resource] / [sub_resource] property lines
    # are inspected, so a megabyte PackedVector3Array elsewhere is never parsed.
    section: str | None = None
    section_nid: str | None = None

    for lineno, line in enumerate(src.splitlines(), 1):
        if line.startswith("["):
            m = _GODOT_SECTION_RE.match(line)
            if not m:
                continue
            section, raw_attrs = m.group(1), m.group(2)
            section_nid = None
            attrs = _godot_section_attrs(raw_attrs)

            if section == "gd_resource":
                # [gd_resource type="Resource" script_class="Ammo" ...] — the
                # script_class is the class_name of the attached script; a
                # sourceless stub lets the rewire collapse it onto that class.
                script_class = attrs.get("script_class")
                if script_class:
                    stub_nid = _make_id(script_class)
                    if stub_nid not in seen_ids:
                        seen_ids.add(stub_nid)
                        nodes.append({
                            "id": stub_nid,
                            "label": script_class,
                            "file_type": "code",
                            "source_file": "",
                            "source_location": "",
                            "origin_file": str_path,
                        })
                    add_edge(file_nid, stub_nid, "references", lineno, context="script_class")

            elif section == "ext_resource":
                res_id = attrs.get("id", "")
                res_path = attrs.get("path", "")
                target = _resolve_res_path(res_path, path) if res_path else None
                if target is not None:
                    target_nid, abs_target = add_file_ref_node(target, lineno)
                    add_edge(file_nid, target_nid, "imports", lineno,
                             context="ext_resource", target_file=abs_target)
                    if res_id:
                        ext_resources[res_id] = (target_nid, abs_target, target)

            elif section == "node":
                name = attrs.get("name")
                if not name:
                    continue
                parent = attrs.get("parent")
                if parent is None:
                    node_path = "."
                elif parent == ".":
                    node_path = name
                else:
                    node_path = f"{parent}/{name}"
                nid = _make_id(stem, name if parent is None else node_path)
                add_node(nid, name, lineno)
                node_path_to_nid[node_path] = nid
                if parent is None:
                    root_nid = nid
                    add_edge(file_nid, nid, "contains", lineno)
                else:
                    # A parent path inside an instanced sub-scene has no [node]
                    # section of its own; anchor such children at the root.
                    parent_nid = node_path_to_nid.get(parent) or root_nid or file_nid
                    add_edge(parent_nid, nid, "contains", lineno)
                node_type = attrs.get("type")
                if node_type:
                    type_nid = _make_id("godot", node_type)
                    add_node(type_nid, node_type, lineno, file_type="concept")
                    add_edge(nid, type_nid, "references", lineno, context="type")
                instance = attrs.get("instance")
                if instance:
                    ref = _GODOT_EXT_REF_RE.search(instance)
                    if ref and ref.group(1) in ext_resources:
                        target_nid, abs_target, _ = ext_resources[ref.group(1)]
                        add_edge(nid, target_nid, "embeds", lineno,
                                 context="instance", target_file=abs_target)
                section_nid = nid

            elif section == "resource":
                # [resource] properties belong to the .tres file itself.
                section_nid = file_nid

            elif section == "sub_resource":
                sub_id = attrs.get("id")
                if sub_id:
                    sub_nid = _make_id(stem, "sub", sub_id)
                    add_node(sub_nid, sub_id, lineno)
                    add_edge(file_nid, sub_nid, "contains", lineno)
                    sub_resources[sub_id] = sub_nid
                    sub_type = attrs.get("type")
                    if sub_type:
                        type_nid = _make_id("godot", sub_type)
                        add_node(type_nid, sub_type, lineno, file_type="concept")
                        add_edge(sub_nid, type_nid, "references", lineno, context="type")
                    section_nid = sub_nid

            elif section == "connection":
                signal = attrs.get("signal", "")
                method = attrs.get("method", "")
                from_nid = node_path_to_nid.get(attrs.get("from", ""))
                if method:
                    # The handler lives in a script (usually the one attached to
                    # the `to` node) — resolved corpus-wide like any other call.
                    raw_calls.append({
                        "caller_nid": from_nid or root_nid or file_nid,
                        "callee": method,
                        "is_member_call": False,
                        "context": "signal",
                        "source_file": str_path,
                        "source_location": f"L{lineno}",
                    })
            continue

        # Property line inside a section with a node. A SubResource(...) reference
        # embeds that sub-resource regardless of which key holds it, so every line
        # is scanned for one; a full `key = value` line is classified below.
        if section_nid is not None:
            for ref in _GODOT_SUB_REF_RE.finditer(line):
                pending_sub_refs.append((section_nid, ref.group(1), lineno))

            prop = _GODOT_PROPERTY_RE.match(line)
            if prop:
                key, value = prop.group(1), prop.group(2).strip()
                ext_ref = _GODOT_EXT_REF_RE.search(value)

                if key == "script":
                    if ext_ref and ext_ref.group(1) in ext_resources:
                        target_nid, abs_target, script_path = ext_resources[ext_ref.group(1)]
                        add_edge(section_nid, target_nid, "references", lineno,
                                 context="script", target_file=abs_target)
                        section_scripts[section_nid] = script_path
                else:
                    pending_members.append((section_nid, key, lineno))
                    if ext_ref:
                        if ext_ref.group(1) in ext_resources:
                            target_nid, abs_target, _ = ext_resources[ext_ref.group(1)]
                            add_edge(section_nid, target_nid, "references", lineno,
                                     context=key, target_file=abs_target)
                    elif _GODOT_SUB_REF_RE.search(value):
                        pass  # embedded by the per-line scan above
                    else:
                        quoted = _GODOT_QUOTED_RE.match(value)
                        nodepath_ref = _GODOT_NODEPATH_RE.match(value)
                        if quoted and quoted.group(1).startswith(("res://", "uid://")):
                            target = _resolve_res_path(quoted.group(1), path)
                            if target is not None:
                                target_nid, abs_target = add_file_ref_node(target, lineno)
                                add_edge(section_nid, target_nid, "references", lineno,
                                         context=key, target_file=abs_target)
                        elif nodepath_ref:
                            target_nid = node_path_to_nid.get(nodepath_ref.group(1))
                            if target_nid is not None:
                                add_edge(section_nid, target_nid, "references", lineno,
                                         context=key)
                        else:
                            add_property(section_nid, key, value)

    for ref_nid, sub_id, lineno in pending_sub_refs:
        sub_nid = sub_resources.get(sub_id)
        if sub_nid is not None:
            add_edge(ref_nid, sub_nid, "embeds", lineno, context="sub_resource")

    for sec_nid, key, lineno in pending_members:
        script_path = section_scripts.get(sec_nid)
        if script_path is not None:
            member_nid = _make_id(_file_stem(script_path), key)
            add_edge(sec_nid, member_nid, "references", lineno, context="property")

    for node in nodes:
        state = section_properties.get(node["id"])
        if state and state["text"]:
            node["properties"] = state["text"]

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls}
