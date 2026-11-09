"""Ferramentas de câmera, luz, renderização e inspeção de saída."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Dict, List

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, ToolSpec, bridge_tool


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCD):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def inspect_image(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "path": str(path), "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "format": path.suffix.lstrip(".").upper(),
    }
    if not path.exists():
        return info
    try:
        head = path.open("rb").read(1024 * 64)
        size = _png_size(head) or _jpeg_size(head)
        if size:
            info["width"], info["height"] = size
    except OSError:
        pass
    return info


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    # ---- Câmera e luz ------------------------------------------------------------
    bridge_tool(
        ctx, registry, "camera_configure",
        "Configura uma câmera: focal_length, fov, clip, tipo de projeção e "
        "depth of field.",
        "camera_light",
        {"properties": {
            "camera": {"type": "string", "description": "Padrão: câmera ativa"},
            "focal_length": {"type": "number"},
            "fov": {"type": "number", "description": "Field of view em radianos"},
            "type": {"type": "string", "enum": ["PERSP", "ORTHO", "PANO"]},
            "clip_start": {"type": "number"},
            "clip_end": {"type": "number"},
            "dof_enable": {"type": "boolean"},
            "focus_distance": {"type": "number"},
            "aperture_fstop": {"type": "number"},
        }},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "light_create",
        "Cria uma luz (POINT, SUN, SPOT, AREA) com transformações e energia.",
        "camera_light",
        {"properties": {
            "type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
            "name": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}},
            "rotation": {"type": "array", "items": {"type": "number"}},
            "energy": {"type": "number"},
            "color": {"type": "array", "items": {"type": "number"}},
        }, "required": ["type"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "light_configure",
        "Configura uma luz existente: energia, cor, tamanho, spot, sombras.",
        "camera_light",
        {"properties": {
            "light": {"type": "string"},
            "energy": {"type": "number"},
            "color": {"type": "array", "items": {"type": "number"}},
            "size": {"type": "number"},
            "spot_size": {"type": "number"},
            "spot_blend": {"type": "number"},
            "use_shadow": {"type": "boolean"},
        }, "required": ["light"]},
        AuthLevel.SCENE_WRITE, "low",
    )

    # ---- Renderização ---------------------------------------------------------------
    bridge_tool(
        ctx, registry, "render_configure",
        "Configura o render: engine (CYCLES/BLENDER_EEVEE), resolução, "
        "samples, formato de saída, filepath, intervalo de frames.",
        "rendering",
        {"properties": {
            "engine": {"type": "string", "enum": ["CYCLES", "BLENDER_EEVEE"]},
            "resolution_x": {"type": "integer"},
            "resolution_y": {"type": "integer"},
            "resolution_percentage": {"type": "integer"},
            "samples": {"type": "integer"},
            "use_gpu": {"type": "boolean"},
            "filepath": {"type": "string"},
            "file_format": {"type": "string",
                            "enum": ["PNG", "JPEG", "OPEN_EXR", "BMP", "TIFF",
                                     "WEBP", "FFMPEG"]},
            "frame_start": {"type": "integer"},
            "frame_end": {"type": "integer"},
            "film_transparent": {"type": "boolean"},
            "use_stamp": {"type": "boolean"},
        }},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "render_preview",
        "Render rápido de baixa resolução para inspeção (job assíncrono no "
        "modo interface).",
        "rendering",
        {"properties": {
            "filepath": {"type": "string"},
            "percentage": {"type": "integer", "description": "Padrão 33"},
        }},
        AuthLevel.SCENE_WRITE, "high", timeout_s=300,
    )
    bridge_tool(
        ctx, registry, "render_final",
        "Render final (imagem ou animação). Job assíncrono: acompanhe com "
        "job_get_status e confirme com render_inspect.",
        "rendering",
        {"properties": {
            "filepath": {"type": "string"},
            "animation": {"type": "boolean"},
        }},
        AuthLevel.PROJECT_OPS, "high", timeout_s=600,
    )

    # ---- render_inspect: resolvido no servidor -------------------------------------
    async def render_inspect(args: Dict[str, Any]) -> Dict[str, Any]:
        from ..schemas import ResultEnvelope, new_command_id  # noqa: PLC0415

        def envelope(data: Dict[str, Any]) -> Dict[str, Any]:
            return {"command_id": new_command_id(), "status": "completed",
                    "success": True, "data": data, "scene_revision": None,
                    "error": None}

        raw = args.get("path", "")
        p = Path(raw)
        if not p.is_absolute():
            p = ctx.settings.renders_dir / raw
        if not ctx.policy.paths.is_allowed(p, ctx.settings.root):
            # BUG-025: uma negação NÃO pode ser reportada como sucesso.
            return ResultEnvelope.fail(
                new_command_id(), "PathNotAllowed",
                f"caminho fora da allowlist de políticas: {p}").to_dict()
        return envelope(inspect_image(p))

    registry.register(ToolSpec(
        "render_inspect",
        "Verifica no servidor um arquivo de render: existência, tamanho, "
        "formato e dimensões (PNG/JPEG).",
        "validation", AuthLevel.READ_ONLY,
        {"properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=render_inspect, bridge=False, risk="low",
    ))
