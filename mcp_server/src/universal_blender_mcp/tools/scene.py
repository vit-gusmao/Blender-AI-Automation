"""Ferramentas de conexão, inspeção, jobs e validação de cena."""
from __future__ import annotations

import asyncio

from typing import Any, Dict

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, ToolSpec, bridge_tool

OBJECT_TYPES = ["MESH", "CURVE", "SURFACE", "META", "FONT", "ARMATURE", "LATTICE",
                "EMPTY", "GPENCIL", "CAMERA", "LIGHT", "SPEAKER", "LIGHT_PROBE"]


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    # ---- Conexão -------------------------------------------------------------
    bridge_tool(
        ctx, registry, "blender_ping",
        "Verifica a conexão de ponta a ponta com a instância do Blender 3.6 e "
        "retorna a revisão atual da cena.",
        "connection", {"properties": {}}, AuthLevel.READ_ONLY, "low", 30,
    )
    bridge_tool(
        ctx, registry, "blender_get_version",
        "Retorna versões do Blender, Python embutido e da plataforma.",
        "connection", {"properties": {}}, AuthLevel.READ_ONLY, "low", 30,
    )

    async def capabilities(args: Dict[str, Any]) -> Dict[str, Any]:
        from ..authorization import AuthLevel  # noqa: PLC0415
        from ..schemas import new_command_id  # noqa: PLC0415

        reg = ctx.registry_ref
        tools = [
            {
                "name": s.name, "category": s.category,
                "auth_level": s.auth_level.name.lower(), "risk": s.risk,
                "bridge_dispatch": s.bridge,
            }
            for s in reg.specs()
        ]
        session = getattr(ctx, "session_level", None)
        policy = ctx.policy
        policy_data = {
            "session_level": (session.name.lower()
                              if isinstance(session, AuthLevel) else None),
            "python_execution": None if policy is None else {
                "allow_blender": bool(policy.python.allow_blender),
                "allow_external": bool(policy.python.allow_external),
            },
            "external_backend": (None if policy is None
                                 else policy.external_backend),
        }
        return {
            "command_id": new_command_id(), "status": "completed",
            "success": True,
            "data": {
                "tool_count": len(tools), "tools": tools,
                "categories": reg.by_category(),
                "transports": ["stdio", "streamable-http"],
                "blender_target": "3.6",
                "session_level": policy_data["session_level"],
                "policy": policy_data,
            },
            "scene_revision": None, "error": None,
        }

    registry.register(ToolSpec(
        "blender_get_capabilities",
        "Lista todas as ferramentas disponíveis com categoria, nível de "
        "autorização e risco. Use antes de planejar uma tarefa.",
        "connection", AuthLevel.READ_ONLY,
        {"properties": {}}, handler=capabilities, bridge=False, risk="low",
    ))

    # ---- Inspeção --------------------------------------------------------------
    bridge_tool(
        ctx, registry, "scene_get_info",
        "Informações da cena ativa: nome, unidades, frame range, contagem de "
        "objetos por tipo, câmera ativa e filepath do projeto.",
        "inspection",
        {"properties": {"include_collections": {"type": "boolean"}}},
        AuthLevel.READ_ONLY, "low", 30,
    )
    bridge_tool(
        ctx, registry, "scene_list_objects",
        "Lista objetos da cena com nome, tipo, visibilidade e estatísticas.",
        "inspection",
        {"properties": {
            "type": {"type": "string", "description": "Filtrar por tipo (MESH, CAMERA...)"},
            "collection": {"type": "string"},
            "limit": {"type": "integer"},
        }},
        AuthLevel.READ_ONLY, "low", 30,
    )
    bridge_tool(
        ctx, registry, "object_get_info",
        "Detalhes de um objeto: transformações, geometria, modificadores, "
        "materiais, parenting e datablocks.",
        "inspection",
        {"properties": {
            "name": {"type": "string"},
            "include_modifiers": {"type": "boolean"},
            "include_materials": {"type": "boolean"},
        }, "required": ["name"]},
        AuthLevel.READ_ONLY, "low", 30,
    )
    bridge_tool(
        ctx, registry, "scene_get_selection",
        "Objetos selecionados e objeto ativo da cena.",
        "inspection", {"properties": {}}, AuthLevel.READ_ONLY, "low", 30,
    )

    # ---- Validação (§9.3) ------------------------------------------------------
    bridge_tool(
        ctx, registry, "scene_validate",
        "Validação estrutural da cena: malhas inválidas, nomes duplicados, "
        "modificadores quebrados, imagens ausentes e dados órfãos.",
        "validation",
        {"properties": {
            "checks": {"type": "array", "items": {"type": "string",
                        "enum": ["invalid_meshes", "duplicate_names",
                                 "broken_modifiers", "missing_images",
                                 "missing_materials", "orphan_actions"]}},
            "fix": {"type": "boolean", "description": "Aplica correções; requer nível scene_write ou superior"},
        }},
        AuthLevel.READ_ONLY, "low", 60,
    )
    bridge_tool(
        ctx, registry, "object_compare",
        "Compara dois objetos (transformações, geometria, materiais, "
        "modificadores) e retorna as diferenças.",
        "validation",
        {"properties": {
            "a": {"type": "string"}, "b": {"type": "string"},
            "check": {"type": "array", "items": {"type": "string",
                      "enum": ["transform", "geometry", "materials", "modifiers"]}},
        }, "required": ["a", "b"]},
        AuthLevel.READ_ONLY, "low", 60,
    )

    # ---- Jobs (resolvidos no servidor, §9.4) ------------------------------------
    from ..schemas import new_command_id  # noqa: PLC0415

    def _local_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
        return {"command_id": new_command_id(), "status": "completed",
                "success": True, "data": data, "scene_revision": None,
                "error": None}

    async def job_get_status(args: Dict[str, Any]) -> Dict[str, Any]:
        job = ctx.jobs.get(args["job_id"])
        if not job:
            return _local_envelope({"job_id": args["job_id"],
                                    "status": "unknown", "found": False})
        return _local_envelope({
            "job_id": job["command_id"], "status": job["status"],
            "operation": job["operation"], "found": True,
            "created_at": job["created_at"], "finished_at": job["finished_at"]})

    async def job_cancel(args: Dict[str, Any]) -> Dict[str, Any]:
        outcome = await asyncio.to_thread(ctx.bridge.cancel, args["job_id"])
        return _local_envelope(outcome)

    async def job_get_result(args: Dict[str, Any]) -> Dict[str, Any]:
        job = ctx.jobs.get(args["job_id"])
        if not job:
            return _local_envelope({"job_id": args["job_id"],
                                    "status": "unknown", "found": False})
        return _local_envelope({
            "job_id": job["command_id"], "status": job["status"],
            "result": job.get("result"), "error": job.get("error"),
            "found": True})

    for name, desc, handler in (
        ("job_get_status", "Consulta o status de um job/comando submetido.", job_get_status),
        ("job_cancel", "Cancela um job ainda enfileirado (cancelamento cooperativo).", job_cancel),
        ("job_get_result", "Retorna o resultado persistido de um job concluído.", job_get_result),
    ):
        registry.register(ToolSpec(
            name, desc, "jobs", AuthLevel.READ_ONLY,
            {"properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
            handler=handler, bridge=False, risk="low",
        ))
