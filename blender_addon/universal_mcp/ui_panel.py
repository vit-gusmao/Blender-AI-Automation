# SPDX-License-Identifier: MIT
"""Painel N da 3D View — aba "Universal MCP" (§5 do plano)."""
from __future__ import annotations

import bpy
from bpy.props import PointerProperty, StringProperty
from bpy.types import Panel

from . import bridge_client, dispatcher, snapshot_manager
from .compatibility.blender_36 import version_info


class UNIVERSALMCP_PT_main(Panel):
    bl_idname = "VIEW3D_PT_universal_mcp"
    bl_label = "Universal MCP"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Universal MCP"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        client = bridge_client.get_client()

        # -- Connection -----------------------------------------------------
        box = layout.box()
        box.label(text="Connection", icon="LINKED")
        if client is not None and client.is_alive():
            connected = client.status()
            icon = "CHECKMARK" if connected["connected"] else "ERROR"
            box.label(text=f"Bridge: {connected['url']}", icon=icon)
            if connected.get("last_error"):
                box.label(text=connected["last_error"][:60], icon="ERROR")
            row = box.row(align=True)
            row.label(text=f"Comandos: {connected['commands_received']}")
            row.label(text=f"Resultados: {connected['results_sent']}")
            box.operator("universal_mcp.disconnect", icon="X")
        else:
            prefs = context.preferences.addons["universal_mcp"].preferences
            box.label(text=f"Destino: {prefs.bridge_host}:{prefs.bridge_port}",
                      icon="NETWORK")
            box.operator("universal_mcp.connect", icon="PLAY")

        # -- Provider / prompts ---------------------------------------------
        box = layout.box()
        box.label(text="Provider", icon="SETTINGS")
        box.label(text="Modelo: selecionado pelo harness", icon="INFO")
        box.label(text="System Prompt: prompts/system_prompt.md")

        # -- Task Prompt (modo agente próprio) -------------------------------
        box = layout.box()
        box.label(text="Task Prompt", icon="TEXT")
        box.template_ID(wm, "universal_mcp_task_text", new="text.new")
        box.operator("universal_mcp.send_task", icon="RNA_ADD")
        if wm.universal_mcp_task_result:
            box.label(text="Resultado da tarefa", icon="INFO")
            for line in wm.universal_mcp_task_result.splitlines():
                for start in range(0, len(line), 72):
                    box.label(text=line[start:start + 72])

        # -- Execution --------------------------------------------------------
        box = layout.box()
        box.label(text="Execution", icon="RENDER_ANIMATION")
        row = box.row(align=True)
        row.operator("universal_mcp.ping", text="Testar", icon="DOT")
        row.operator("universal_mcp.refresh_status", text="", icon="FILE_REFRESH")
        if dispatcher.PENDING_RENDERS:
            box.label(text=f"Renders pendentes: {len(dispatcher.PENDING_RENDERS)}",
                      icon="RENDER_RESULT")

        # -- Inspector ---------------------------------------------------------
        box = layout.box()
        box.label(text="Inspector", icon="VIEWZOOM")
        scene = context.scene
        stats = (
            f"Objetos: {len(scene.objects)}",
            f"Câmera: {scene.camera.name if scene.camera else '—'}",
        )
        for line in stats:
            box.label(text=line)
        selected = context.selected_objects
        if selected:
            box.label(text=f"Selecionados: {len(selected)}")
            active = context.view_layer.objects.active
            box.label(text=f"Ativo: {active.name if active else '—'}")

        # -- Checkpoints ---------------------------------------------------------
        box = layout.box()
        box.label(text="Checkpoints", icon="FILE_BACKUP")
        box.operator("universal_mcp.checkpoint", icon="PLUS")
        try:
            latest = snapshot_manager.latest_checkpoint()
            if latest:
                box.label(text=latest.split("\\")[-1][:40])
        except Exception:  # noqa: BLE001 — diagnóstico opcional
            pass

        # -- Logs / Assets / Diagnostics -------------------------------------------
        row = layout.row(align=True)
        row.operator("universal_mcp.open_logs", text="Logs", icon="TEXT")
        row.operator("universal_mcp.open_prefs", text="", icon="PREFERENCES")
        box = layout.box()
        box.label(text="Diagnostics", icon="INFO")
        info = version_info()
        box.label(text=f"Blender {info['blender_version']} "
                       f"(Python {info['python_version']})")
        box.label(text=f"Modo: {'background' if info['background'] else 'interface'}")
        box.label(text=f"Operações: {len(dispatcher.operation_names())}")


classes = (UNIVERSALMCP_PT_main,)


def register():
    bpy.types.WindowManager.universal_mcp_task_text = PointerProperty(
        type=bpy.types.Text)
    bpy.types.WindowManager.universal_mcp_task_result = StringProperty(
        name="Resultado da tarefa", default="")
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.universal_mcp_task_text
    del bpy.types.WindowManager.universal_mcp_task_result
