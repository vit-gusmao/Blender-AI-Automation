"""Ferramentas de projeto: abrir, salvar e checkpoints (§14)."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "project_open",
        "Abre um arquivo .blend (feche e salve antes o que precisa). O "
        "caminho deve estar na allowlist.",
        "files",
        {"properties": {
            "path": {"type": "string"},
            "create_checkpoint": {"type": "boolean",
                                  "description": "Checkpoint do projeto atual antes de abrir"},
        }, "required": ["path"]},
        AuthLevel.PROJECT_OPS, "high", timeout_s=180,
    )
    bridge_tool(
        ctx, registry, "project_save",
        "Salva o projeto atual (ou em novo caminho), com opção de pack de "
        "recursos externos.",
        "files",
        {"properties": {
            "path": {"type": "string", "description": "Vazio = salvar no atual"},
            "incremental": {"type": "boolean"},
            "pack": {"type": "boolean", "description": "Empacota recursos externos"},
        }},
        AuthLevel.PROJECT_OPS, "medium", timeout_s=180,
    )
    bridge_tool(
        ctx, registry, "project_checkpoint",
        "Cria um checkpoint (cópia datada) do projeto atual em "
        "storage/checkpoints e retorna o caminho.",
        "files",
        {"properties": {
            "label": {"type": "string"},
            "directory": {"type": "string"},
        }},
        AuthLevel.PROJECT_OPS, "low", timeout_s=180,
    )
