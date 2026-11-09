"""Unit: envelopes e validação de argumentos (contrato §6.4)."""
from __future__ import annotations

from universal_blender_mcp import schemas
from universal_blender_mcp.schemas import (ResultEnvelope, build_command,
                                           new_command_id,
                                           validate_arguments)


def test_build_command_defaults():
    command = build_command("object_create", {"type": "CUBE"})
    assert command["operation"] == "object_create"
    assert command["arguments"] == {"type": "CUBE"}
    assert command["dry_run"] is False
    assert command["command_id"].startswith("cmd-")
    assert command["idempotency_key"] is None


def test_build_command_preserves_server_authorization_metadata():
    command = build_command(
        "python_execute_blender", {"code": "result = 1"},
        authorization={"level": "advanced", "approved": True})

    assert command["authorization"] == {
        "level": "advanced", "approved": True}


def test_envelope_ok_and_fail():
    ok = ResultEnvelope.ok("cmd-1", {"x": 1}, scene_revision=3)
    assert ok.to_dict() == {
        "command_id": "cmd-1", "status": "completed", "success": True,
        "data": {"x": 1}, "scene_revision": 3, "error": None}
    fail = ResultEnvelope.fail("cmd-2", "ValueError", "ruim", "tb...")
    d = fail.to_dict()
    assert d["success"] is False
    assert d["error"]["type"] == "ValueError"
    assert d["error"]["message"] == "ruim"


def test_validate_required_and_types():
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}},
            "slot": {"type": "integer"},
            "linked": {"type": "boolean"},
        },
    }
    assert validate_arguments(schema, {"name": "A"}) == []
    assert validate_arguments(schema, {}) == ["campo obrigatório ausente: 'name'"]
    assert validate_arguments(schema, {"name": 1}) != []
    assert validate_arguments(schema, {"name": "A", "slot": True}) != []  # bool≠int
    errs = validate_arguments(
        schema, {"name": "A", "location": [0, "x", 2]})
    assert any("location[1]" in e for e in errs)


def test_validate_enum_in_items():
    schema = {"type": "object", "properties": {
        "ops": {"type": "array", "items": {"type": "string",
                                           "enum": ["ADD", "REMOVE"]}}}}
    assert validate_arguments(schema, {"ops": ["ADD"]}) == []
    assert validate_arguments(schema, {"ops": ["XX"]}) != []


def test_validate_value_applies_scalar_enum_at_any_depth():
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["SAFE", "FAST"]},
                },
                "required": ["mode"],
            },
        },
        "required": ["config"],
    }

    assert schemas.validate_value(
        {"type": "string", "enum": ["A", "B"]}, "A") == []
    assert any("$" in error and "enum" in error
               for error in schemas.validate_value(
                   {"type": "string", "enum": ["A", "B"]}, "C"))
    assert any("$.config.mode" in error
               for error in validate_arguments(schema, {"config": {}}))
    assert any("$.config.mode" in error and "enum" in error
               for error in validate_arguments(
                   schema, {"config": {"mode": "UNKNOWN"}}))


def test_validate_arrays_of_objects_recursively():
    schema = {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["EXTRUDE", "BEVEL"]},
                        "count": {"type": "integer"},
                    },
                    "required": ["op", "count"],
                },
            },
        },
        "required": ["operations"],
    }

    errors = validate_arguments(schema, {
        "operations": [
            {"op": "EXTRUDE", "count": 1},
            {"op": "UNKNOWN", "count": True},
            {"count": 2},
        ],
    })

    assert any("$.operations[1].op" in error and "enum" in error
               for error in errors)
    assert any("$.operations[1].count" in error and "integer" in error
               for error in errors)
    assert any("$.operations[2].op" in error and "obrigatório" in error
               for error in errors)


def test_validate_numbers_reject_booleans_and_enforce_bounds():
    schema = {
        "type": "object",
        "properties": {
            "integer": {"type": "integer", "minimum": 1, "maximum": 3},
            "number": {"type": "number", "minimum": 0.5, "maximum": 2.5},
            "positive": {"type": "number", "exclusiveMinimum": 0},
        },
    }

    errors = validate_arguments(schema, {
        "integer": True,
        "number": False,
        "positive": 0,
    })
    assert any("$.integer" in error and "integer" in error for error in errors)
    assert any("$.number" in error and "number" in error for error in errors)
    assert any("$.positive" in error and "exclusiveMinimum" in error
               for error in errors)
    assert any("minimum" in error
               for error in validate_arguments(schema, {"integer": 0}))
    assert any("maximum" in error
               for error in validate_arguments(schema, {"number": 3.0}))


def test_validate_extra_fields_follow_object_contract():
    strict = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }

    assert validate_arguments(
        {"type": "object", "properties": {}}, {"legacy": 1}) == []
    assert any("$.extra" in error and "não permitido" in error
               for error in schemas.validate_value(
                   strict, {"known": "ok", "extra": 1}))
    assert any("$.extra" in error and "não permitido" in error
               for error in schemas.validate_value(
                   {"type": "object", "properties": {}},
                   {"extra": 1}, allow_extra=False))


def test_malformed_schema_nodes_fail_closed():
    malformed = (
        ({"type": "unknown"}, None),
        ({"type": "object", "properties": []}, {}),
        ({"type": "object", "required": "name"}, {}),
        ({"type": "array", "items": "string"}, []),
        ({"type": "number", "minimum": "zero"}, 1),
    )

    for schema, value in malformed:
        errors = schemas.validate_value(schema, value)
        assert errors, schema
        assert any("schema inválido" in error for error in errors), errors


def test_new_command_id_unique():
    assert len({new_command_id() for _ in range(200)}) == 200
