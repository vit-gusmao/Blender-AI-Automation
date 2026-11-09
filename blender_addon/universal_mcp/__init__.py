# SPDX-License-Identifier: MIT
"""Universal Blender MCP — addon para Blender 3.6 (camada 4 do plano).

Fluxo (§5.1): Bridge (HTTP) → bridge_client (thread de rede) →
CommandQueue → bpy.app.timers (thread principal) → dispatcher (bpy) →
envelope §6.4 de volta pela Bridge.

Registro: Edit → Preferences → Add-ons → instalar o ZIP gerado por
scripts/package-addon.ps1 → ativar "Universal Blender MCP".

Nota: este módulo NÃO importa bpy no nível superior — os submódulos puros
(como ``security``) permanecem importáveis fora do Blender para testes.
"""
from __future__ import annotations

bl_info = {
    "name": "Universal Blender MCP",
    "author": "Blender RPA (plano docs/Plan-Auto-Blender.txt)",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "3D View → N → Universal MCP",
    "description": "Automação do Blender 3.6 via Universal Blender MCP Server "
                   "(Bridge autenticada, executor na thread principal, "
                   "checkpoints e políticas de segurança)",
    "category": "System",
}

_modules = ("preferences", "operators", "ui_panel")


def register():
    import bpy  # noqa: F401 — só existe dentro do Blender

    import importlib

    # núcleo primeiro (dispatcher registra handlers de render persistentes)
    from . import dispatcher as _dispatcher

    _dispatcher.load_post_register()
    for name in _modules:
        importlib.import_module(f"{__name__}.{name}").register()
    prefs = bpy.context.preferences.addons.get(__name__)
    if prefs and getattr(prefs.preferences, "auto_connect", False):
        bpy.app.timers.register(_auto_connect, first_interval=2.0,
                                persistent=False)


def _auto_connect():
    import bpy
    try:
        bpy.ops.universal_mcp.connect()
    except (RuntimeError, AttributeError):
        pass
    return None  # executa uma única vez


def unregister():
    import importlib

    for name in reversed(_modules):
        importlib.import_module(f"{__name__}.{name}").unregister()
    from . import bridge_client as _bridge_client
    from . import dispatcher as _dispatcher

    _dispatcher.load_post_unregister()
    _bridge_client.stop_client()


if __name__ == "__main__":
    register()
