"""Templates de prompts MCP (§6.3) — carregados de prompts/skills/*.md."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

PROMPT_DEFS: List[Dict[str, str]] = [
    {"name": "blender_modeling", "description": "Guia de modelagem no Blender 3.6 via ferramentas MCP."},
    {"name": "blender_materials", "description": "Guia de materiais, shaders e texturas."},
    {"name": "blender_rigging", "description": "Guia de armatures, ossos e pesos."},
    {"name": "blender_animation", "description": "Guia de keyframes, actions, fcurves e NLA."},
    {"name": "blender_rendering", "description": "Guia de câmera, luz, render e validação visual."},
    {"name": "blender_task", "description": "Template de Task Prompt universal (§11) parametrizado."},
]

_SKILL_FILES = {
    "blender_modeling": "modeling.md",
    "blender_materials": "materials.md",
    "blender_rigging": "rigging.md",
    "blender_animation": "animation.md",
    "blender_rendering": "rendering.md",
}


def _load(root: Path, relative: str) -> str:
    path = root / "prompts" / relative
    if path.exists():
        return path.read_text(encoding="utf-8")
    try:  # pacote instalado: cópia empacotada dos prompts (BUG-025)
        from importlib import resources

        package = resources.files("universal_blender_mcp")
        for part in ("prompts", *relative.split("/")):
            package = package.joinpath(part)
        return package.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError,
            UnicodeDecodeError):
        return f"arquivo prompts/{relative} não encontrado"


def get_prompt(name: str, arguments: Optional[Dict[str, str]],
               root: Path) -> List[Dict[str, str]]:
    """Retorna mensagens [{role, content}] de um prompt MCP."""
    arguments = arguments or {}
    if name in _SKILL_FILES:
        return [{"role": "user",
                 "content": _load(root, f"skills/{_SKILL_FILES[name]}")}]
    if name == "blender_task":
        template = _load(root, "task_prompt.template.md")
        objective = arguments.get("objective", "[descreva o objetivo]")
        filled = template.replace(
            "[Descreva o que deve ser criado, alterado, automatizado ou "
            "analisado no Blender.]", objective)
        return [{"role": "user", "content": filled}]
    raise KeyError(f"prompt desconhecido: {name}")
