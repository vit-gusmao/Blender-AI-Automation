"""Recursos MCP consultáveis (blender://...) — §6.3 do plano."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .schemas import ResultEnvelope

RESOURCES: List[Dict[str, str]] = [
    {"uri": "blender://runtime/info", "name": "Runtime info",
     "description": "Status da Bridge, do Blender conectado e do servidor.",
     "mimeType": "application/json"},
    {"uri": "blender://scene/summary", "name": "Scene summary",
     "description": "Resumo da cena ativa (scene_get_info).",
     "mimeType": "application/json"},
    {"uri": "blender://scene/objects", "name": "Scene objects",
     "description": "Lista de objetos da cena (scene_list_objects).",
     "mimeType": "application/json"},
    {"uri": "blender://object/{name}", "name": "Object info",
     "description": "Detalhes de um objeto por nome (object_get_info).",
     "mimeType": "application/json"},
    {"uri": "blender://materials", "name": "Materials",
     "description": "Materiais da cena via python_execute_blender (listagem).",
     "mimeType": "application/json"},
    {"uri": "blender://jobs/{job_id}", "name": "Job",
     "description": "Status e resultado de um job.",
     "mimeType": "application/json"},
    {"uri": "blender://project/current", "name": "Current project",
     "description": "Arquivo .blend atual e estado de salvamento.",
     "mimeType": "application/json"},
]

_LIST_MATERIALS_CODE = (
    "import bpy, json\n"
    "result = {'materials': [\n"
    "    {'name': m.name, 'users': m.users}\n"
    "    for m in bpy.data.materials\n"
    "]}\n"
)


def resource_request(uri: str) -> Tuple[str, Dict[str, Any]]:
    """Mapeia um recurso ao contrato da ferramenta equivalente."""
    if uri == "blender://runtime/info":
        return "blender_ping", {}
    if uri in ("blender://scene/summary", "blender://project/current"):
        return "scene_get_info", {"include_collections": False}
    if uri == "blender://scene/objects":
        return "scene_list_objects", {"limit": 500}
    if uri.startswith("blender://object/"):
        name = uri[len("blender://object/"):]
        if not name:
            raise KeyError("nome do objeto ausente")
        return "object_get_info", {"name": name}
    if uri == "blender://materials":
        # O código é 100% construído pelo servidor (constante); a aprovação
        # do servidor sobre esse código fixo permite ao recurso atravessar o
        # mesmo gate da ferramenta em sessão advanced (review F1). O nível
        # ADVANCED continua obrigatório.
        return "python_execute_blender", {"code": _LIST_MATERIALS_CODE,
                                          "approval": True}
    if uri.startswith("blender://jobs/"):
        job_id = uri[len("blender://jobs/"):]
        if not job_id:
            raise KeyError("id do job ausente")
        return "job_get_result", {"job_id": job_id}
    raise KeyError(f"recurso desconhecido: {uri}")


async def read_resource(uri: str, ctx,
                        authorization: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve um blender:// URI. Levanta KeyError para URIs desconhecidas."""
    operation, arguments = resource_request(uri)
    bridge_kwargs = {
        "requested_by": {"resource": uri},
        "authorization": dict(authorization or {}),
    }
    if uri == "blender://runtime/info":
        status = await ctx.bridge.execute(operation, arguments, **bridge_kwargs)
        try:
            bridge_status = ctx.bridge.status()
        except Exception:  # noqa: BLE001
            bridge_status = {"ok": False}
        return {"server": {"name": ctx.settings.server_name,
                           "version": ctx.settings.version},
                "bridge": bridge_status, "blender": status}

    if uri == "blender://scene/summary":
        return await ctx.bridge.execute(operation, arguments, **bridge_kwargs)
    if uri == "blender://scene/objects":
        return await ctx.bridge.execute(operation, arguments, **bridge_kwargs)

    if uri.startswith("blender://object/"):
        name = uri[len("blender://object/"):]
        if not name:
            raise KeyError("nome do objeto ausente")
        return await ctx.bridge.execute(operation, arguments, **bridge_kwargs)

    if uri == "blender://materials":
        return await ctx.bridge.execute(operation, arguments, **bridge_kwargs)

    if uri.startswith("blender://jobs/"):
        job_id = uri[len("blender://jobs/"):]
        job = ctx.jobs.get(job_id)
        if not job:
            return ResultEnvelope.fail(job_id, "NotFound",
                                       f"job {job_id} não encontrado").to_dict()
        return {"job_id": job["command_id"], "status": job["status"],
                "operation": job["operation"], "result": job.get("result"),
                "error": job.get("error")}

    if uri == "blender://project/current":
        return await ctx.bridge.execute(operation, arguments, **bridge_kwargs)

    raise KeyError(f"recurso desconhecido: {uri}")
