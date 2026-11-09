# SPDX-License-Identifier: MIT
"""Operadores do addon (botões do painel N → Universal MCP)."""
from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, Dict

import bpy
from bpy.types import Operator

from . import bridge_client, dispatcher, snapshot_manager
from .storage_paths import storage_root

_task_results = queue.Queue()
_task_pending = 0


def task_prompt(text_block):
    return text_block.as_string()


def _consume_task_results():
    global _task_pending
    while True:
        try:
            result = _task_results.get_nowait()
        except queue.Empty:
            break
        bpy.context.window_manager.universal_mcp_task_result = json.dumps(
            result, ensure_ascii=False, default=str)[-20000:]
        _task_pending = max(0, _task_pending - 1)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 0.25 if _task_pending else None


def _prefs():
    return bpy.context.preferences.addons["universal_mcp"].preferences


class UNIVERSALMCP_OT_connect(Operator):
    bl_idname = "universal_mcp.connect"
    bl_label = "Conectar"
    bl_description = "Conecta à Bridge e inicia o processamento de comandos"

    def execute(self, context):
        prefs = _prefs()
        bridge_client.start_client(prefs)
        if not bpy.app.timers.is_registered(_timer):
            # tick roda na thread principal (§5.1) e consome a CommandQueue
            bpy.app.timers.register(
                _timer, first_interval=0.5, persistent=True)
        self.report({"INFO"}, "Conectando à Bridge "
                    f"{prefs.bridge_host}:{prefs.bridge_port}…")
        return {"FINISHED"}


def _timer():
    client = bridge_client.get_client()
    if client is None or not client.is_alive():
        return None  # encerra o timer quando desconectado
    return dispatcher.tick(client.queue)


class UNIVERSALMCP_OT_disconnect(Operator):
    bl_idname = "universal_mcp.disconnect"
    bl_label = "Desconectar"
    bl_description = "Encerra a conexão com a Bridge"

    def execute(self, context):
        if not bridge_client.stop_client():
            self.report({"ERROR"}, "A thread de rede ainda está encerrando")
            return {"CANCELLED"}
        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
        self.report({"INFO"}, "Desconectado.")
        return {"FINISHED"}


class UNIVERSALMCP_OT_ping(Operator):
    bl_idname = "universal_mcp.ping"
    bl_label = "Testar execução"
    bl_description = "Executa blender_ping localmente (diagnóstico)"

    def execute(self, context):
        command = {"command_id": "ui-ping", "operation": "blender_ping",
                   "arguments": {}, "dry_run": False}
        envelope = dispatcher.execute_command(command)
        if envelope["success"]:
            self.report({"INFO"},
                        f"Pong OK — Blender {envelope['data']['blender_version']}, "
                        f"revisão {envelope['scene_revision']}")
        else:
            self.report({"ERROR"}, f"Falha: {envelope['error']}")
        return {"FINISHED"}


class UNIVERSALMCP_OT_checkpoint(Operator):
    bl_idname = "universal_mcp.checkpoint"
    bl_label = "Criar checkpoint"
    bl_description = "Salva uma cópia datada do projeto atual"

    def execute(self, context):
        out = snapshot_manager.create_checkpoint(label="manual")
        self.report({"INFO"}, f"Checkpoint: {out['checkpoint']}")
        return {"FINISHED"}


class UNIVERSALMCP_OT_open_prefs(Operator):
    bl_idname = "universal_mcp.open_prefs"
    bl_label = "Preferências"
    bl_description = "Abre as preferências do addon"

    def execute(self, context):
        bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
        # localiza a área de preferências e filtra pelo addon
        window = context.window_manager.windows[-1] \
            if context.window_manager.windows else context.window
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "USER_PREFERENCE":
                    space = area.spaces.active
                    space.search_filter = "Universal Blender MCP"
                    return {"FINISHED"}
        return {"FINISHED"}


class UNIVERSALMCP_OT_open_logs(Operator):
    bl_idname = "universal_mcp.open_logs"
    bl_label = "Abrir pasta de logs"
    bl_description = "Abre a pasta de logs do projeto no Explorer"

    def execute(self, context):
        logs = storage_root(_prefs()) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(str(logs))  # noqa: S606 — Windows Explorer
            return {"FINISHED"}
        except OSError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UNIVERSALMCP_OT_send_task(Operator):
    bl_idname = "universal_mcp.send_task"
    bl_label = "Executar tarefa no agente próprio"
    bl_description = ("Envia o Task Prompt do bloco de texto selecionado ao "
                      "Universal Agent Gateway (§7.2)")

    _result_box: Dict[str, Any] = {}

    def execute(self, context):
        global _task_pending
        text_block = context.window_manager.universal_mcp_task_text
        if text_block is None:
            self.report({"ERROR"}, "Selecione (ou crie) o bloco de texto do "
                                   "Task Prompt")
            return {"CANCELLED"}
        prompt = task_prompt(text_block)
        if not prompt.strip():
            self.report({"ERROR"}, "Task Prompt vazio.")
            return {"CANCELLED"}
        gateway = _prefs().gateway_url.rstrip("/")

        def worker():
            try:
                req = urllib.request.Request(
                    f"{gateway}/task",
                    data=json.dumps({"prompt": prompt}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                _task_results.put(out)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                _task_results.put({"error": str(exc)})

        _task_pending += 1
        context.window_manager.universal_mcp_task_result = "Tarefa enviada; aguardando resultado…"
        if not bpy.app.timers.is_registered(_consume_task_results):
            bpy.app.timers.register(_consume_task_results, first_interval=0.25)
        threading.Thread(target=worker, daemon=True,
                         name="mcp-send-task").start()
        self.report({"INFO"}, "Tarefa enviada ao gateway — acompanhe no painel.")
        return {"FINISHED"}


class UNIVERSALMCP_OT_refresh_status(Operator):
    bl_idname = "universal_mcp.refresh_status"
    bl_label = "Atualizar"
    bl_description = "Atualiza o status da conexão"

    def execute(self, context):
        return {"FINISHED"}


classes = (
    UNIVERSALMCP_OT_connect,
    UNIVERSALMCP_OT_disconnect,
    UNIVERSALMCP_OT_ping,
    UNIVERSALMCP_OT_checkpoint,
    UNIVERSALMCP_OT_open_prefs,
    UNIVERSALMCP_OT_open_logs,
    UNIVERSALMCP_OT_send_task,
    UNIVERSALMCP_OT_refresh_status,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    if bpy.app.timers.is_registered(_consume_task_results):
        bpy.app.timers.unregister(_consume_task_results)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
