"""Tests for the Godot extractors: GDScript (.gd) and scene/resource files (.tscn/.tres)."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.extract import extract, extract_gdscript, extract_godot_project, extract_godot_scene
from graphify.extractors.base import _file_stem, _make_id
from graphify.extractors.godot import _GODOT_PROPERTIES_CAP, _class_name_map, _resolve_res_path

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


# ── Type annotations (var/const, parameters, return types) ───────────────────

def test_gd_var_type_targets_user_class_in_other_file():
    fighter = extract_gdscript(FIXTURES / "fighter.gd")
    weapon = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(weapon, "Weapon")["id"]
    var_id = _node_by_label(fighter, "current_weapon")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in fighter["edges"]
        if e["relation"] == "references" and e.get("context") == "var_type"
    }
    assert (var_id, weapon_id) in ref_edges
    edge = next(e for e in fighter["edges"] if e["target"] == weapon_id)
    assert edge["target_file"].endswith("weapon.gd")


def test_gd_engine_type_annotation_becomes_concept_node():
    r = extract_gdscript(FIXTURES / "fighter.gd")
    concept = _node_by_label(r, "Area3D")
    assert concept["file_type"] == "concept"
    assert concept["id"] == _make_id("godot", "Area3D")
    hitbox_id = _node_by_label(r, "hitbox")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "var_type"
    }
    assert (hitbox_id, concept["id"]) in ref_edges


def test_gd_value_type_annotation_produces_no_edge():
    r = extract_gdscript(FIXTURES / "fighter.gd")
    speed_id = _node_by_label(r, "speed_limit")["id"]
    assert not [e for e in r["edges"] if e["source"] == speed_id]


def test_gd_generic_annotation_resolves_to_last_segment():
    fighter = extract_gdscript(FIXTURES / "fighter.gd")
    weapon = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(weapon, "Weapon")["id"]
    ammo_id = _node_by_label(fighter, "extra_ammo")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in fighter["edges"]
        if e["relation"] == "references" and e.get("context") == "var_type"
    }
    assert (ammo_id, weapon_id) in ref_edges


def test_gd_qualified_annotation_resolves_to_last_segment(tmp_path):
    fixture = tmp_path / "q.gd"
    fixture.write_text("extends Node\n\nvar q: Outer.Area3D\n", encoding="utf-8")
    r = extract_gdscript(fixture)
    concept = _node_by_label(r, "Area3D")
    q_id = _node_by_label(r, "q")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "var_type"
    }
    assert (q_id, concept["id"]) in ref_edges


def test_gd_function_parameter_and_return_type_edges():
    fighter = extract_gdscript(FIXTURES / "fighter.gd")
    weapon = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(weapon, "Weapon")["id"]
    hit_id = _node_by_label(fighter, "hit()")["id"]
    param_edges = {
        (e["source"], e["target"]) for e in fighter["edges"]
        if e["relation"] == "references" and e.get("context") == "parameter_type"
    }
    return_edges = {
        (e["source"], e["target"]) for e in fighter["edges"]
        if e["relation"] == "references" and e.get("context") == "return_type"
    }
    assert (hit_id, weapon_id) in param_edges
    assert (hit_id, weapon_id) in return_edges


# ── Static class references (Foo.new(), Foo.method(), Foo.CONST) ─────────────

def test_gd_static_new_call_emits_reference_only():
    r = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(r, "Weapon")["id"]
    build_id = _node_by_label(r, "build()")["id"]
    static_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "static"
    }
    assert (build_id, weapon_id) in static_edges
    assert not [e for e in r["edges"] if e["source"] == build_id and e["relation"] == "calls"]
    assert not any(rc["callee"] == "new" for rc in r["raw_calls"])


def test_gd_static_method_call_keeps_calls_edge():
    r = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(r, "Weapon")["id"]
    build_id = _node_by_label(r, "build()")["id"]
    rebuild_id = _node_by_label(r, "rebuild()")["id"]
    static_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "static"
    }
    assert (rebuild_id, weapon_id) in static_edges
    assert (rebuild_id, build_id) in _edge_pairs(r, "calls")


def test_gd_static_attribute_access_without_call():
    fighter = extract_gdscript(FIXTURES / "fighter.gd")
    weapon = extract_gdscript(FIXTURES / "weapon.gd")
    weapon_id = _node_by_label(weapon, "Weapon")["id"]
    ammo_cap_id = _node_by_label(fighter, "ammo_cap()")["id"]
    static_edges = {
        (e["source"], e["target"]) for e in fighter["edges"]
        if e["relation"] == "references" and e.get("context") == "static"
    }
    assert (ammo_cap_id, weapon_id) in static_edges


def test_gd_plain_identifier_attribute_access_stays_silent(tmp_path):
    fixture = tmp_path / "s.gd"
    fixture.write_text(
        "extends Node\n\n\nfunc f(some_node) -> void:\n\tvar p = some_node.position\n",
        encoding="utf-8",
    )
    r = extract_gdscript(fixture)
    f_id = _node_by_label(r, "f()")["id"]
    assert not [e for e in r["edges"] if e["source"] == f_id]


def test_gd_value_type_attribute_access_stays_silent(tmp_path):
    fixture = tmp_path / "v.gd"
    fixture.write_text(
        "extends Node\n\n\nfunc g() -> void:\n\tvar z = Vector3.ZERO\n",
        encoding="utf-8",
    )
    r = extract_gdscript(fixture)
    g_id = _node_by_label(r, "g()")["id"]
    assert not [e for e in r["edges"] if e["source"] == g_id]


def test_gd_const_receiver_is_not_treated_as_class():
    r = extract_gdscript(FIXTURES / "player.gd")
    fire_id = _node_by_label(r, "fire()")["id"]
    bullet_scene_id = _node_by_label(r, "BulletScene")["id"]
    assert (fire_id, bullet_scene_id) not in _edge_pairs(r, "references")


def test_gd_instance_var_receiver_is_not_treated_as_class():
    r = extract_gdscript(FIXTURES / "fighter.gd")
    use_weapon_id = _node_by_label(r, "use_weapon()")["id"]
    current_weapon_id = _node_by_label(r, "current_weapon")["id"]
    assert (use_weapon_id, current_weapon_id) not in _edge_pairs(r, "references")
    static_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "static"
    }
    assert not [e for e in static_edges if e[0] == use_weapon_id]


def test_gd_function_name_receiver_is_not_treated_as_class(tmp_path):
    fixture = tmp_path / "h.gd"
    fixture.write_text(
        "extends Node\n\n\nfunc helper() -> void:\n\tpass\n\n\n"
        "func f() -> void:\n\tvar y = helper.bind(1)\n",
        encoding="utf-8",
    )
    r = extract_gdscript(fixture)
    f_id = _node_by_label(r, "f()")["id"]
    helper_id = _node_by_label(r, "helper()")["id"]
    assert (f_id, helper_id) not in _edge_pairs(r, "references")
    static_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "static"
    }
    assert not [e for e in static_edges if e[0] == f_id]


def test_class_name_map_cached_across_sibling_files():
    first = _class_name_map(FIXTURES / "player.gd")
    second = _class_name_map(FIXTURES / "enemy.gd")
    assert first is second
    assert first["Weapon"].resolve() == (FIXTURES / "weapon.gd").resolve()


# ── project.godot / uid resolution / autoloads ────────────────────────────────

def test_resolve_res_path_uid_resolves_and_rejects_unknown():
    resolved = _resolve_res_path("uid://kb8cc1vpp45t", FIXTURES / "player.gd")
    assert resolved is not None
    assert resolved.resolve() == (FIXTURES / "game_data.gd").resolve()
    assert _resolve_res_path("uid://does-not-exist", FIXTURES / "player.gd") is None


def test_gd_preload_uid_produces_imports_edge():
    r = extract_gdscript(FIXTURES / "event_bus.gd")
    file_id = _node_by_label(r, "event_bus.gd")["id"]
    game_data_id = _node_by_label(r, "game_data.gd")["id"]
    assert (file_id, game_data_id) in _edge_pairs(r, "imports")


def test_gd_autoload_call_resolves_to_singleton_script():
    r = extract_gdscript(FIXTURES / "event_bus.gd")
    notify_id = _node_by_label(r, "notify()")["id"]
    load_id = _make_id(_file_stem((FIXTURES / "game_data.gd").resolve()), "load")
    calls = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "calls" and e.get("context") == "autoload"
    }
    assert (notify_id, load_id) in calls
    refs = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "autoload"
    }
    assert (notify_id, _make_id("autoload", "GameData")) in refs


def test_godot_project_autoload_and_main_scene_edges():
    r = extract_godot_project(FIXTURES / "project.godot")
    file_id = _node_by_label(r, "project.godot")["id"]
    event_bus_singleton = _make_id("autoload", "EventBus")
    game_data_singleton = _make_id("autoload", "GameData")
    event_bus_script = _node_by_label(r, "event_bus.gd")["id"]
    game_data_script = _node_by_label(r, "game_data.gd")["id"]
    player_scene = _node_by_label(r, "player.tscn")["id"]

    autoload_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "autoload"
    }
    autoload_script_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "autoload_script"
    }
    main_scene_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "main_scene"
    }

    assert (file_id, event_bus_script) in autoload_edges
    assert (file_id, game_data_script) in autoload_edges
    assert (event_bus_singleton, event_bus_script) in autoload_script_edges
    assert (game_data_singleton, game_data_script) in autoload_script_edges
    assert (file_id, player_scene) in main_scene_edges

    singleton_labels = {n["label"] for n in r["nodes"] if n["file_type"] == "concept"}
    assert {"EventBus", "GameData"} <= singleton_labels


def test_extract_pipeline_autoload_call_survives_stem_collision(tmp_path):
    # game_data.gd and a same-stem game_data.tscn (a script + companion scene,
    # an ordinary Godot pairing) collide on the bare id "game_data_load": the
    # script's load() function and the scene's root node share it until the
    # colliding-id pass salts them apart. That pass keys its salt by
    # source_file, and for an edge whose id was minted from an ALREADY-RESOLVED
    # absolute path (autoload_map, like class_name_map, resolves the project
    # root) the edge's own source_file is the CALLER's file, not the target's
    # — so without a target_file stamp naming game_data.gd, salting can't tell
    # which variant the autoload call meant and the edge is left on the dead
    # unsalted id.
    (tmp_path / "project.godot").write_text(
        '; Engine configuration file.\nconfig_version=5\n\n[autoload]\n\n'
        'GameData="*res://game_data.gd"\n',
        encoding="utf-8",
    )
    (tmp_path / "game_data.gd").write_text(
        "extends Node\n\n\nfunc load() -> void:\n\tpass\n", encoding="utf-8",
    )
    (tmp_path / "game_data.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="load" type="Node"]\n', encoding="utf-8",
    )
    (tmp_path / "event_bus.gd").write_text(
        "extends Node\n\n\nfunc notify() -> void:\n\tGameData.load()\n", encoding="utf-8",
    )
    files = [tmp_path / "event_bus.gd", tmp_path / "game_data.gd", tmp_path / "game_data.tscn"]

    result = extract(files, cache_root=tmp_path, root=tmp_path, parallel=False)
    ids = {n["id"] for n in result["nodes"]}
    by_id = {n["id"]: n for n in result["nodes"]}

    calls = [
        e for e in result["edges"]
        if e["relation"] == "calls" and e.get("context") == "autoload"
    ]
    assert calls
    for e in calls:
        assert e["target"] in ids, e
        assert str(by_id[e["target"]]["source_file"]).endswith("game_data.gd")


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


def test_tscn_sub_resource_becomes_a_node():
    path = FIXTURES / "player.tscn"
    r = extract_godot_scene(path)
    file_id = _node_by_label(r, "player.tscn")["id"]
    shape = _node_by_label(r, "RectangleShape2D_1")
    assert shape["id"] == _make_id(_file_stem(path), "sub", "RectangleShape2D_1")
    assert (file_id, shape["id"]) in _edge_pairs(r, "contains")


def test_tscn_sub_resource_type_becomes_concept_edge():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    shape_id = _node_by_label(r, "RectangleShape2D_1")["id"]
    concept = _node_by_label(r, "RectangleShape2D")
    assert concept["file_type"] == "concept"
    type_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "type"
    }
    assert (shape_id, concept["id"]) in type_edges


def test_tscn_sub_resource_script_references_script():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    shape_id = _node_by_label(r, "RectangleShape2D_1")["id"]
    script_id = _node_by_label(r, "player.gd")["id"]
    script_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "script"
    }
    assert (shape_id, script_id) in script_edges


def test_tscn_node_property_embeds_sub_resource():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    collision_id = _node_by_label(r, "CollisionShape2D")["id"]
    shape_id = _node_by_label(r, "RectangleShape2D_1")["id"]
    embeds = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "embeds" and e.get("context") == "sub_resource"
    }
    assert (collision_id, shape_id) in embeds


def test_tscn_sub_resource_reference_resolves_forward():
    # RectangleShape2D_1's property names CircleShape2D_1 before its
    # [sub_resource] header appears later in the file.
    r = extract_godot_scene(FIXTURES / "player.tscn")
    rect_id = _node_by_label(r, "RectangleShape2D_1")["id"]
    circle_id = _node_by_label(r, "CircleShape2D_1")["id"]
    embeds = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "embeds" and e.get("context") == "sub_resource"
    }
    assert (rect_id, circle_id) in embeds


def test_tscn_sub_resource_with_no_header_produces_no_edge():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    assert "Missing_1" not in _labels(r)
    embeds = [
        e for e in r["edges"]
        if e["relation"] == "embeds" and e.get("context") == "sub_resource"
    ]
    sprite_id = _node_by_label(r, "Sprite2D")["id"]
    assert not [e for e in embeds if e["source"] == sprite_id]


def test_tscn_nodepath_property_resolves_to_scene_node():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    collision_id = _node_by_label(r, "CollisionShape2D")["id"]
    sprite_id = _node_by_label(r, "Sprite2D")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "target_path"
    }
    assert (collision_id, sprite_id) in ref_edges


def test_tscn_noise_property_key_excluded_from_properties():
    r = extract_godot_scene(FIXTURES / "player.tscn")
    root = _node_by_label(r, "Player")
    # "visible" is the root node's only non-script property, and it's noise.
    assert "properties" not in root


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


def test_tres_resource_scalars_become_properties_attribute():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    file_node = _node_by_label(r, "ammo.tres")
    assert "pickup_quantity=10" in file_node["properties"]
    assert "string_id=ammo_fixture" in file_node["properties"]


def test_tres_member_binding_edge_lands_on_real_member():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    file_id = _node_by_label(r, "ammo.tres")["id"]
    member_id = _make_id(_file_stem(FIXTURES / "player.gd"), "pickup_quantity")
    prop_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "property"
    }
    assert (file_id, member_id) in prop_edges


def test_tres_res_string_property_references_resolved_file():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    file_id = _node_by_label(r, "ammo.tres")["id"]
    bullet_id = _node_by_label(r, "bullet.tscn")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "icon_path"
    }
    assert (file_id, bullet_id) in ref_edges


def test_tres_ext_resource_property_references_target():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    file_id = _node_by_label(r, "ammo.tres")["id"]
    hud_id = _node_by_label(r, "hud.tscn")["id"]
    ref_edges = {
        (e["source"], e["target"]) for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "preview_scene"
    }
    assert (file_id, hud_id) in ref_edges


def test_tres_unresolvable_res_string_produces_nothing():
    r = extract_godot_scene(FIXTURES / "ammo.tres")
    ref_edges = [
        e for e in r["edges"]
        if e["relation"] == "references" and e.get("context") == "missing_icon"
    ]
    assert not ref_edges
    assert "does_not_exist" not in _labels(r)


def test_godot_scene_properties_attribute_capped_at_500_chars(tmp_path):
    value = "x" * 100
    lines = ['[gd_resource type="Resource" format=3]', "", "[resource]"]
    lines += [f"field_{i} = \"{value}\"" for i in range(10)]
    fixture = tmp_path / "big.tres"
    fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")

    r = extract_godot_scene(fixture)
    props = _node_by_label(r, "big.tres")["properties"]
    assert len(props) <= _GODOT_PROPERTIES_CAP + len("; ...")
    assert props.endswith("...")
    assert "field_0=" in props
    assert "field_9=" not in props


def test_godot_scene_node_attributes_are_graphml_safe_scalars():
    for fixture in ("player.tscn", "ammo.tres"):
        r = extract_godot_scene(FIXTURES / fixture)
        for node in r["nodes"]:
            for key, value in node.items():
                assert isinstance(value, (str, int, float, bool)), (fixture, key, value)


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
