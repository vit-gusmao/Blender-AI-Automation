"""Normalização de tool calling + fallbacks para modelos sem suporte (§7.3).

Três níveis:
1. Tool calling nativo     → tool_calls vindos do provider;
2. JSON estruturado        → texto contendo um bloco de ação JSON;
3. Somente texto           → proposta de ação para validação assistida.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .providers import ToolCall

def _json_objects(text: str):
    """Find balanced objects while honoring JSON strings and escapes."""
    stack = []
    spans = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"' and stack:
            quoted = True
        elif char == '{':
            stack.append(index)
        elif char == '}' and stack:
            spans.append((stack.pop(), index + 1))
    for start, end in sorted(spans):
        yield text[start:end]


def parse_json_action(text: str) -> Optional[ToolCall]:
    """Extrai {"tool": ..., "arguments": {...}} de texto livre (nível 2/3)."""
    if not text:
        return None
    for candidate in _json_objects(text):
        try:
            data = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        call = _normalize_action(data)
        if call:
            return call
    return None


def _normalize_action(data: Dict[str, Any]) -> Optional[ToolCall]:
    if not isinstance(data, dict):
        return None
    action = data.get("action") if isinstance(data.get("action"), dict) else data
    name = (action.get("tool") or action.get("name")
            or action.get("tool_name") or data.get("tool"))
    if not isinstance(name, str) or not name:
        return None
    arguments = next((action[key] for key in ('arguments', 'args', 'parameters', 'input')
                      if key in action), {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return ToolCall(id=f"json-{abs(hash(name + str(arguments)[:32])) % 10**8}",
                    name=name, arguments=arguments if isinstance(arguments, dict)
                    else {})


def call_to_json(call: ToolCall) -> Dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def tool_defs_for_provider(mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converte tools/list do MCP para o formato interno {name, description,
    input_schema}."""
    defs = []
    for tool in mcp_tools:
        defs.append({
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "input_schema": tool.get("inputSchema", {"type": "object"}),
        })
    return [d for d in defs if d["name"]]
