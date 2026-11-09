"""Unit: adaptação de tool calls e fallbacks JSON (§7.3)."""
from __future__ import annotations

from agent_gateway.providers import ToolCall
from agent_gateway.tool_call_adapter import (parse_json_action,
                                             tool_defs_for_provider)


def test_parse_fenced_json_action():
    text = 'Vou criar o cubo:\n```json\n{"tool": "object_create", "arguments": {"type": "CUBE"}}\n```'
    call = parse_json_action(text)
    assert call is not None
    assert call.name == "object_create"
    assert call.arguments == {"type": "CUBE"}


def test_parse_nested_action_key():
    text = '{"action": {"name": "scene_get_info", "parameters": {"limit": 5}}}'
    call = parse_json_action(text)
    assert call.name == "scene_get_info"
    assert call.arguments == {"limit": 5}


def test_parse_plain_text_returns_none():
    assert parse_json_action("apenas um texto sem ação") is None
    assert parse_json_action("") is None


def test_tool_defs_from_mcp_format():
    mcp_tools = [{
        "name": "blender_ping",
        "description": "ping",
        "inputSchema": {"type": "object", "properties": {}},
    }, {"description": "sem nome deve ser descartado"}]
    defs = tool_defs_for_provider(mcp_tools)
    assert len(defs) == 1
    assert defs[0]["input_schema"]["type"] == "object"


def test_action_in_prose_with_deep_nesting_and_escaped_string_braces():
    import json
    arguments = {'nested': {'items': [{'a': {'quoted': 'brace } { and "quote" \\ end'}}]}}
    text = 'before [noise] ' + json.dumps({'tool': 'x', 'arguments': arguments}) + ' after'
    call = parse_json_action(text)
    assert call is not None
    assert call.arguments == arguments


def test_json_array_or_bad_action_arguments_are_not_actions():
    assert parse_json_action('[1, 2]') is None
    assert parse_json_action('{"tool":"x","arguments":[1]}') is None


def test_empty_nonobject_arguments_are_rejected():
    for value in ('[]', 'false', '0', 'null'):
        assert parse_json_action('{"tool":"x","arguments":' + value + '}') is None
