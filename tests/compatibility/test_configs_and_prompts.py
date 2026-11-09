"""Compatibilidade: config de clientes para harnesses (§13)."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

REQUIRED_CLIENTS = ("codex.json", "cursor.json", "claude.json",
                    "opencode.json")


def test_all_client_configs_exist_and_valid():
    for name in REQUIRED_CLIENTS:
        path = REPO / "config" / "clients" / name
        assert path.exists(), f"config de cliente ausente: {name}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "blender-universal"
        # cursor.json expõe mcpServers; os demais expõem command/args
        if "command" in data:
            assert ".venv" in data["command"]
        else:
            server = data["mcpServers"]["blender-universal"]
            assert server["command"].endswith("python.exe")


def test_cursor_config_is_mcpjson_compatible():
    data = json.loads((REPO / "config" / "clients" / "cursor.json")
                      .read_text(encoding="utf-8"))
    servers = data["mcpServers"]["blender-universal"]
    assert servers["args"] == ["-m", "universal_blender_mcp"]
    assert servers["command"].endswith("python.exe")


def test_register_commands_reference_real_entrypoint():
    for name in ("codex.json", "claude.json", "opencode.json"):
        data = json.loads((REPO / "config" / "clients" / name)
                          .read_text(encoding="utf-8"))
        assert "universal_blender_mcp" in data["register_command"]


def test_registered_prompts_resolve_to_consumable_messages():
    from universal_blender_mcp.prompts import PROMPT_DEFS, get_prompt

    objective = "Crie um cubo de teste com material azul."
    for definition in PROMPT_DEFS:
        arguments = ({"objective": objective}
                     if definition["name"] == "blender_task" else {})
        messages = get_prompt(definition["name"], arguments, REPO)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"].strip()
        assert "não encontrado" not in messages[0]["content"]
    task = get_prompt("blender_task", {"objective": objective}, REPO)[0]
    assert objective in task["content"]
    assert "[Descreva o que deve ser criado" not in task["content"]
