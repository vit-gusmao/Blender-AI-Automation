# SPDX-License-Identifier: MIT
"""Preferências do addon (Edit → Preferences → Add-ons → Universal Blender MCP)."""

import secrets

import bpy
from bpy.types import AddonPreferences
from bpy.props import (BoolProperty, FloatProperty, IntProperty,
                       StringProperty)

from .security import DEFAULT_BLOCKED_PATTERNS


def _default_token():
    return secrets.token_hex(24)


class UniversalMCPPreferences(AddonPreferences):
    bl_idname = "universal_mcp"

    # Connection
    bridge_host: StringProperty(
        name="Endereço", default="127.0.0.1",
        description="Host da Bridge (mantenha 127.0.0.1 — §14)")
    bridge_port: IntProperty(
        name="Porta", default=8765, min=1, max=65535)
    bridge_token: StringProperty(
        name="Token", default="", subtype="PASSWORD",
        description="Token da Bridge (storage/bridge_token do servidor). "
                    "Vazio gera um token local aleatório.")
    poll_wait_s: FloatProperty(
        name="Espera de poll (s)", default=1.0, min=0.1, max=5.0)
    auto_connect: BoolProperty(
        name="Conectar automaticamente ao iniciar", default=False)

    # Segurança
    allow_python: BoolProperty(
        name="Permitir python_execute_blender", default=True,
        description="Execução de Python controlada (nível advanced + políticas)")
    strict_python: BoolProperty(
        name="Modo estrito (bloquear padrões perigosos)", default=True)
    blocked_patterns: StringProperty(
        name="Padrões bloqueados",
        default=";".join(DEFAULT_BLOCKED_PATTERNS),
        description="Separados por ';' — avaliados antes do exec")
    restrict_paths: BoolProperty(
        name="Restringir caminhos de arquivos", default=False,
        description="Quando ativo, open/save/import/export só aceitam os "
                    "caminhos abaixo (além da allowlist do servidor)")
    allowed_paths: StringProperty(
        name="Caminhos autorizados", default="", subtype="DIR_PATH",
        description="Separados por ';'")
    auto_checkpoint: BoolProperty(
        name="Checkpoint automático antes de ações destrutivas", default=True)

    # Assets / storage
    storage_dir: StringProperty(
        name="Pasta compartilhada de storage", default="", subtype="DIR_PATH",
        description="Configure a pasta storage do servidor; vazio usa BLENDER_MCP_ROOT ou configuração do usuário")
    checkpoint_max_keep: IntProperty(name="Máximo de checkpoints", default=10, min=1, max=10000)
    renders_dir: StringProperty(
        name="Pasta de renders", default="", subtype="DIR_PATH",
        description="Vazio = pasta compartilhada de storage/renders")
    checkpoints_dir: StringProperty(
        name="Pasta de checkpoints", default="", subtype="DIR_PATH",
        description="Vazio = pasta compartilhada de storage/checkpoints")

    # Harness próprio
    gateway_url: StringProperty(
        name="Agent Gateway", default="http://127.0.0.1:8767",
        description="URL do harness próprio (modo agente do painel)")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Connection", icon="LINKED")
        row = box.row()
        row.prop(self, "bridge_host")
        row.prop(self, "bridge_port")
        box.prop(self, "bridge_token")
        row = box.row()
        row.prop(self, "poll_wait_s")
        row.prop(self, "auto_connect")

        box = layout.box()
        box.label(text="Permissions", icon="SHADERFX")
        box.prop(self, "allow_python")
        box.prop(self, "strict_python")
        box.prop(self, "blocked_patterns")
        box.prop(self, "restrict_paths")
        if self.restrict_paths:
            box.prop(self, "allowed_paths")
        box.prop(self, "auto_checkpoint")

        box = layout.box()
        box.label(text="Assets", icon="FILE_FOLDER")
        box.prop(self, "storage_dir")
        box.prop(self, "renders_dir")
        box.prop(self, "checkpoints_dir")
        box.prop(self, "checkpoint_max_keep")

        box = layout.box()
        box.label(text="Provider", icon="SETTINGS")
        box.prop(self, "gateway_url")


classes = (UniversalMCPPreferences,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    addon = bpy.context.preferences.addons.get("universal_mcp")
    if addon and not addon.preferences.bridge_token:
        addon.preferences.bridge_token = _default_token()


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
