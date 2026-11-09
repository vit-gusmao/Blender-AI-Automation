"""Ferramentas de materiais e texturas."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "material_create",
        "Cria (ou reutiliza) um material com Principled BSDF configurando "
        "base_color, metallic, roughness, emission e alpha.",
        "materials",
        {"properties": {
            "name": {"type": "string"},
            "base_color": {"type": "array", "items": {"type": "number"},
                           "description": "RGBA 0..1"},
            "metallic": {"type": "number"},
            "roughness": {"type": "number"},
            "emission_color": {"type": "array", "items": {"type": "number"}},
            "emission_strength": {"type": "number"},
            "alpha": {"type": "number"},
        }, "required": ["name"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "material_assign",
        "Atribui um material a um objeto (slot opcional).",
        "materials",
        {"properties": {
            "object": {"type": "string"},
            "material": {"type": "string"},
            "slot": {"type": "integer"},
        }, "required": ["object", "material"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "material_set_property",
        "Define uma propriedade arbitrária de um material "
        "(ex.: diffuse_color, pass_index) ou de seu Principled BSDF "
        "(use path 'principled.<input>').",
        "materials",
        {"properties": {
            "material": {"type": "string"},
            "property": {"type": "string"},
            "value": {},
        }, "required": ["material", "property", "value"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "uv_unwrap",
        "Aplica UV unwrap em um objeto (SMART_PROJECT, SPHERE_PROJECT, "
        "CYLINDER_PROJECT, CUBE_PROJECT, UNWRAP) com margem.",
        "materials",
        {"properties": {
            "object": {"type": "string"},
            "method": {"type": "string", "enum": ["SMART_PROJECT", "SPHERE_PROJECT",
                                                  "CYLINDER_PROJECT", "CUBE_PROJECT",
                                                  "UNWRAP"]},
            "margin": {"type": "number"},
            "angle_limit": {"type": "number"},
        }, "required": ["object"]},
        AuthLevel.SCENE_WRITE, "medium", timeout_s=180,
    )
    bridge_tool(
        ctx, registry, "texture_load",
        "Carrega um arquivo de imagem como textura (caminho deve estar na "
        "allowlist de políticas).",
        "materials",
        {"properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
        }, "required": ["path"]},
        AuthLevel.PROJECT_OPS, "medium",
    )
    bridge_tool(
        ctx, registry, "texture_assign",
        "Conecta uma textura carregada a um input do Principled BSDF de um "
        "material (Base Color, Roughness, Normal...).",
        "materials",
        {"properties": {
            "material": {"type": "string"},
            "texture": {"type": "string"},
            "input": {"type": "string", "description": "Nome do input do BSDF"},
            "non_color": {"type": "boolean", "description": "Usa colorspace Non-Color"},
        }, "required": ["material", "texture"]},
        AuthLevel.SCENE_WRITE, "low",
    )
