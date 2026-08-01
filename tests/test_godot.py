"""Tests for the Godot extractors: GDScript (.gd) and scene/resource files (.tscn/.tres)."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.extract import extract, extract_gdscript, extract_godot_scene

FIXTURES = Path(__file__).parent / "fixtures" / "godot_project"

try:
    from tree_sitter_language_pack import PackConfig, cache_dir, configure

    _GRAMMAR_CACHE: str | None = cache_dir()
except ImportError:
    _GRAMMAR_CACHE = None


@pytest.fixture(autouse=True)
def _real_grammar_cache():
    """tree-sitter-language-pack resolves the GDScript grammar under the user
    cache directory, which the repo-wide sandbox-home fixture repoints at an
    empty tmp dir. The pack then finds no grammar, fails to fall back to a
    download, and every GDScript extraction returns zero nodes. Pin the pack to
    the cache directory resolved at import time, while the real environment is
    still visible; HOME and USERPROFILE stay sandboxed."""
    if _GRAMMAR_CACHE is not None:
        configure(PackConfig(cache_dir=_GRAMMAR_CACHE))


def _labels(result: dict) -> set[str]:
    return {n["label"] for n in result["nodes"]}


def _edge_pairs(result: dict, relation: str | None = None) -> set[tuple[str, str]]:
    return {
        (e["source"], e["target"]) for e in result["edges"]
        if relation is None or e["relation"] == relation
    }


def _node_by_label(result: dict, label: str) -> dict:
    return next(n for n in result["nodes"] if n["label"] == label)


# ── GDScript (.gd) ────────────────────────────────────────────────────────────

pytest.importorskip("tree_sitter_language_pack", reason="godot extra not installed")


def test_gd_class_name_becomes_real_node():
    r = extract_gdscript(FIXTURES / "player.gd")
    player = _node_by_label(r, "Player")
    assert player["source_file"], "class_name node must be a real (sourced) definition"
    file_node = _node_by_label(r, "player.gd")
    assert (file_node["id"], player["id"]) in _edge_pairs(r, "defines")


def test_gd_members_attach_to_class_name_node():
    r = extract_gdscript(FIXTURES / "player.gd")
    player_id = _node_by_label(r, "Player")["id"]
    for member in ("take_damage()", "signal died", "State", "BulletScene", "speed"):
        member_id = _node_by_label(r, member)["id"]
        assert (player_id, member_id) in _edge_pairs(r, "defines"), member


def test_gd_inherits_builtin_base_as_sourceless_stub():
    r = extract_gdscript(FIXTURES / "player.gd")
    base = _node_by_label(r, "CharacterBody2D")
    assert base["source_file"] == ""
    assert base["origin_file"].endswith("player.gd")
    player_id = _node_by_label(r, "Player")["id"]
    assert (player_id, base["id"]) in _edge_pairs(r, "inherits")


def test_gd_cross_file_extends_named_class_is_stub():
    r = extract_gdscript(FIXTURES / "enemy.gd")
    base = _node_by_label(r, "Player")
    assert base["source_file"] == "", "cross-file base must stay a rewireable stub"
    enemy_id = _node_by_label(r, "Enemy")["id"]
    assert (enemy_id, base["id"]) in _edge_pairs(r, "inherits")


def test_gd_extends_res_path_inherits_the_script_file():
    r = extract_gdscript(FIXTURES / "helper.gd")
    target = _node_by_label(r, "player.gd")
    assert target["source_file"].endswith("player.gd")
    inherit_edges = [e for e in r["edges"] if e["relation"] == "inherits"]
    assert [e for e in inherit_edges if e["target"] == target["id"]]
    import_edges = [e for e in r["edges"] if e["relation"] == "imports"]
    assert [e for e in import_edges if e.get("context") == "extends"]


def test_gd_inner_class_and_method():
    r = extract_gdscript(FIXTURES / "player.gd")
    inv_id = _node_by_label(r, "Inventory")["id"]
    add_id = _node_by_label(r, "add_item()")["id"]
    assert (inv_id, add_id) in _edge_pairs(r, "defines")


def test_gd_same_file_calls():
    r = extract_gdscript(FIXTURES / "player.gd")
    heal = _node_by_label(r, "heal()")["id"]
    take_damage = _node_by_label(r, "take_damage()")["id"]
    assert (heal, take_damage) in _edge_pairs(r, "calls")


def test_gd_signal_emit_and_emit_signal_edges():
    r = extract_gdscript(FIXTURES / "player.gd")
    take_damage = _node_by_label(r, "take_damage()")["id"]
    health_changed = _node_by_label(r, "signal health_changed")["id"]
    died = _node_by_label(r, "signal died")["id"]
    signal_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "calls" and e.get("context") == "signal"
    }
    assert (take_damage, health_changed) in signal_edges   # health_changed.emit(...)
    assert (take_damage, died) in signal_edges              # emit_signal("died")


def test_gd_connect_wires_handler():
    r = extract_gdscript(FIXTURES / "player.gd")
    ready = _node_by_label(r, "_ready()")["id"]
    handler = _node_by_label(r, "_on_died()")["id"]
    signal_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "calls" and e.get("context") == "signal"
    }
    assert (ready, handler) in signal_edges                  # died.connect(_on_died)


def test_gd_preload_imports_res_and_relative_paths():
    r = extract_gdscript(FIXTURES / "player.gd")
    file_id = _node_by_label(r, "player.gd")["id"]
    bullet_id = _node_by_label(r, "bullet.tscn")["id"]      # preload("res://bullet.tscn")
    helper_id = _node_by_label(r, "helper.gd")["id"]        # preload("helper.gd")
    imports = _edge_pairs(r, "imports")
    assert (file_id, bullet_id) in imports
    assert (file_id, helper_id) in imports


def test_gd_unresolved_member_calls_become_raw_calls():
    r = extract_gdscript(FIXTURES / "enemy.gd")
    callees = {rc["callee"] for rc in r["raw_calls"]}
    assert "take_damage" in callees                          # super.take_damage(0)


def test_gd_no_dangling_edges():
    r = extract_gdscript(FIXTURES / "player.gd")
    ids = {n["id"] for n in r["nodes"]}
    for e in r["edges"]:
        assert e["source"] in ids, e
        assert e["target"] in ids, e


# ── Scenes (.tscn) ────────────────────────────────────────────────────────────

def test_tscn_node_tree_containment():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    file_id = _node_by_label(r, "player.tscn")["id"]
    root_id = _node_by_label(r, "Player")["id"]
    sprite_id = _node_by_label(r, "Sprite2D")["id"]
    contains = _edge_pairs(r, "contains")
    assert (file_id, root_id) in contains
    assert (root_id, sprite_id) in contains


def test_tscn_ext_resources_import_referenced_files():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    file_id = _node_by_label(r, "player.tscn")["id"]
    script_node = _node_by_label(r, "player.gd")
    hud_node = _node_by_label(r, "hud.tscn")
    imports = _edge_pairs(r, "imports")
    assert (file_id, script_node["id"]) in imports
    assert (file_id, hud_node["id"]) in imports
    assert script_node["source_file"].endswith("player.gd")


def test_tscn_script_attachment_references_script():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    root_id = _node_by_label(r, "Player")["id"]
    script_id = _node_by_label(r, "player.gd")["id"]
    script_edges = [
        e for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "script"
    ]
    assert [e for e in script_edges if (e["source"], e["target"]) == (root_id, script_id)]


def test_tscn_instance_embeds_scene():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    hud_node_id = _node_by_label(r, "Hud")["id"]
    hud_scene_id = _node_by_label(r, "hud.tscn")["id"]
    assert (hud_node_id, hud_scene_id) in _edge_pairs(r, "embeds")


def test_tscn_node_types_become_concept_nodes():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    concept = _node_by_label(r, "CharacterBody2D")
    assert concept["file_type"] == "concept"
    root_id = _node_by_label(r, "Player")["id"]
    type_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "type"
    }
    assert (root_id, concept["id"]) in type_edges


def test_tscn_connection_emits_signal_raw_call():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    sprite_id = _node_by_label(r, "Sprite2D")["id"]
    rc = next(rc for rc in r["raw_calls"] if rc["callee"] == "take_damage")
    assert rc["caller_nid"] == sprite_id
    assert rc["is_member_call"] is False


def test_tscn_child_of_instanced_subtree_anchors_at_root():
    # HealthLabel's parent path "Hud/Margin" has no [node] section of its own
    # (it lives inside the instanced hud.tscn), so it anchors at the root.
    r = extract_godot_scene(FIXTURES / "player.tscn")
    root_id = _node_by_label(r, "Player")["id"]
    label_id = _node_by_label(r, "HealthLabel")["id"]
    assert (root_id, label_id) in _edge_pairs(r, "contains")


def test_tscn_sub_resource_properties_are_ignored():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    assert "RectangleShape2D_1" not in _labels(r)


# ── Resources (.tres) ─────────────────────────────────────────────────────────

def test_tres_script_class_is_rewireable_stub():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    stub = _node_by_label(r, "Player")
    assert stub["source_file"] == ""
    file_id = _node_by_label(r, "ammo.tres")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "script_class"
    }
    assert (file_id, stub["id"]) in ref_edges


def test_tres_resource_script_references_script_file():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    file_id = _node_by_label(r, "ammo.tres")["id"]
    script_id = _node_by_label(r, "player.gd")["id"]
    assert (file_id, script_id) in _edge_pairs(r, "imports")
    script_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "script"
    }
    assert (file_id, script_id) in script_edges


# ── Full pipeline (same-stem .gd/.tscn collision, cross-file resolution) ──────

def test_extract_pipeline_unifies_scene_and_script(tmp_path):
    import shutil

    # Copy the project so the extraction cache lands in tmp, not in fixtures/.
    proj = tmp_path / "godot_project"
    shutil.copytree(FIXTURES, proj, ignore=shutil.ignore_patterns("graphify-out"))
    files = sorted(proj.glob("*.gd")) + sorted(proj.glob("*.tscn")) \
        + sorted(proj.glob("*.tres"))
    result = extract(files, cache_root=proj, parallel=False)
    nodes, edges = result["nodes"], result["edges"]
    ids = {n["id"] for n in nodes}
    by_id = {n["id"]: n for n in nodes}

    # player.gd and player.tscn share a stem; the colliding-id pass salts them
    # apart, and the target_file hint must route the scene's script reference to
    # the .gd variant — not a self-loop, not the dead unsalted id (#1475 analog).
    script_refs = [
        e for e in edges
        if e["relation"] == "references" and e.get("context") == "script"
        and str(e.get("source_file", "")).endswith("player.tscn")
    ]
    assert script_refs
    for e in script_refs:
        assert e["source"] != e["target"]
        assert e["target"] in ids
        assert str(by_id[e["target"]]["source_file"]).endswith("player.gd")

    # The scene's signal connection resolves to the handler in the attached script.
    connection_calls = [
        e for e in edges
        if e["relation"] == "calls"
        and str(e.get("source_file", "")).endswith("player.tscn")
    ]
    assert any(
        by_id.get(e["target"], {}).get("label") == "take_damage()"
        for e in connection_calls
    )

    # The routing hint is internal — it must never ship in the final edges.
    assert not any("target_file" in e for e in edges)
