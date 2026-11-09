"""Contratos padronizados de comandos e resultados (§6.4 do plano).

Envelopes imutáveis usados pela Bridge, pelo servidor MCP e pelo addon:
qualquer modelo/harness recebe o mesmo formato de erro estruturado.
"""
from __future__ import annotations

import time
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATUSES = ("queued", "running", "completed", "failed", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

MUTATING_HINT_KEYS = (
    "object_create", "object_delete", "mesh_edit", "mesh_boolean",
    "project_open", "project_save",
)


def new_command_id() -> str:
    return f"cmd-{uuid.uuid4().hex[:12]}"


@dataclass
class ErrorInfo:
    type: str
    message: str
    traceback: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "traceback": (self.traceback or "")[-2000:],
        }


@dataclass
class ResultEnvelope:
    command_id: str
    status: str
    success: bool
    data: Any = None
    scene_revision: Optional[int] = None
    error: Optional[ErrorInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status,
            "success": self.success,
            "data": self.data,
            "scene_revision": self.scene_revision,
            "error": self.error.to_dict() if self.error else None,
        }

    @staticmethod
    def ok(command_id: str, data: Any = None, scene_revision: Optional[int] = None) -> "ResultEnvelope":
        return ResultEnvelope(command_id, "completed", True, data, scene_revision, None)

    @staticmethod
    def fail(command_id: str, exc_type: str, message: str, traceback: Optional[str] = None,
             status: str = "failed", scene_revision: Optional[int] = None) -> "ResultEnvelope":
        return ResultEnvelope(command_id, status, False, None, scene_revision,
                              ErrorInfo(exc_type, message, traceback))


def validate_command_envelope(command: Dict[str, Any]) -> List[str]:
    """Reject malformed commands before reserving their durable identity."""
    for field_name in ("command_id", "operation"):
        value = command.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return [f"{field_name} deve ser string não vazia"]
    if not isinstance(command.get("arguments", {}), dict):
        return ["arguments deve ser objeto"]
    key = command.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not key.strip()):
        return ["idempotency_key deve ser string não vazia ou null"]
    for field_name in ("timeout_s", "created_at", "deadline_at"):
        if field_name not in command or (field_name == "timeout_s" and command[field_name] is None):
            continue
        value = command[field_name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{field_name} deve ser número finito"]
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or (field_name == "timeout_s" and value <= 0):
            return [f"{field_name} deve ser número finito" + (" positivo" if field_name == "timeout_s" else "")]
    return []


def validate_result_envelope(payload: Any) -> List[str]:
    """Validate addon results before they can change durable or in-memory state."""
    required = {"command_id", "status", "success", "data", "scene_revision", "error"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        return ["envelope de resultado incompleto"]
    cid, status = payload["command_id"], payload["status"]
    if not isinstance(cid, str) or not cid.strip():
        return ["command_id deve ser string não vazia"]
    if not isinstance(status, str) or status not in ("running", *TERMINAL_STATUSES):
        return ["status de resultado inválido"]
    success = payload["success"]
    if not isinstance(success, bool) or success != (status in ("running", "completed")):
        return ["success inconsistente com status"]
    final = payload.get("final", status in TERMINAL_STATUSES)
    if not isinstance(final, bool) or final != (status in TERMINAL_STATUSES):
        return ["final inconsistente com status"]
    revision = payload["scene_revision"]
    if revision is not None and (not isinstance(revision, int)
                                 or isinstance(revision, bool) or revision < 0):
        return ["scene_revision deve ser inteiro não negativo ou null"]
    error = payload["error"]
    if success:
        if error is not None:
            return ["resultado bem-sucedido não pode conter error"]
    elif (not isinstance(error, dict)
          or not isinstance(error.get("type"), str) or not error["type"].strip()
          or not isinstance(error.get("message"), str)):
        return ["error deve conter type e message"]
    return []


def build_command(operation: str, arguments: Optional[Dict[str, Any]] = None,
                  command_id: Optional[str] = None, scene_revision: Optional[int] = None,
                  dry_run: bool = False, idempotency_key: Optional[str] = None,
                  timeout_s: Optional[float] = None,
                  requested_by: Optional[Dict[str, Any]] = None,
                  authorization: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "command_id": command_id or new_command_id(),
        "operation": operation,
        "arguments": arguments or {},
        "scene_revision": scene_revision,
        "dry_run": bool(dry_run),
        "idempotency_key": idempotency_key,
        "timeout_s": timeout_s,
        "requested_by": requested_by or {},
        "authorization": dict(authorization or {}),
        "created_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Validação recursiva de JSON Schema (subconjunto usado pelos ToolSpecs)
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict, "null": type(None),
}


def validate_arguments(schema: Dict[str, Any], values: Dict[str, Any]) -> List[str]:
    """Valida argumentos de uma ferramenta, mantendo extras na raiz.

    ToolSpecs historicamente aceitam extensões no objeto raiz. Objetos que
    declaram ``additionalProperties: false`` continuam estritos, e chamadores
    que precisam de um contrato totalmente fechado podem usar
    :func:`validate_value` com ``allow_extra=False``.
    """
    if not isinstance(values, dict):
        return [f"argumentos devem ser objeto, recebido {type(values).__name__}"]
    if not isinstance(schema, dict):
        return ["schema raiz inválido: deve ser objeto"]
    schema_type = schema.get("type")
    if schema_type and schema_type not in ("object", None):
        return [f"schema raiz deve ser object, recebido {schema_type}"]
    return validate_value(schema, values, allow_extra=True)


def validate_value(schema: Dict[str, Any], value: Any, path: str = "$",
                   allow_extra: bool = True) -> List[str]:
    """Valida ``value`` recursivamente contra o subconjunto JSON Schema.

    O subconjunto cobre os contratos efetivamente publicados pelos ToolSpecs:
    ``type``, ``enum``, objetos/required/properties/additionalProperties,
    arrays/items e limites numéricos inclusivos ou exclusivos. Schemas
    estruturalmente inválidos falham fechados em vez de desativar validação.
    """
    if not isinstance(schema, dict):
        return [f"{path}: schema inválido; esperado objeto"]

    errors: List[str] = []
    expected = schema.get("type")
    if expected is not None and expected not in _TYPE_MAP:
        return [f"{path}: schema inválido; type desconhecido: {expected!r}"]

    enum = schema.get("enum")
    if "enum" in schema and not isinstance(enum, list):
        errors.append(f"{path}: schema inválido; enum deve ser array")

    properties = schema.get("properties", {})
    if "properties" in schema and not isinstance(properties, dict):
        errors.append(f"{path}: schema inválido; properties deve ser objeto")

    required = schema.get("required", [])
    if ("required" in schema
            and (not isinstance(required, list)
                 or any(not isinstance(item, str) for item in required))):
        errors.append(
            f"{path}: schema inválido; required deve ser array de strings")

    items = schema.get("items")
    if "items" in schema and not isinstance(items, dict):
        errors.append(f"{path}: schema inválido; items deve ser objeto")

    additional = schema.get("additionalProperties", True)
    if ("additionalProperties" in schema
            and not isinstance(additional, (bool, dict))):
        errors.append(
            f"{path}: schema inválido; additionalProperties deve ser boolean ou objeto")

    bounds: Dict[str, float] = {}
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if keyword not in schema:
            continue
        bound = schema[keyword]
        if (not isinstance(bound, (int, float)) or isinstance(bound, bool)
                or not math.isfinite(bound)):
            errors.append(
                f"{path}: schema inválido; {keyword} deve ser número finito")
        else:
            bounds[keyword] = bound

    if errors:
        return errors

    if expected is not None and not _matches_type(expected, value):
        return [
            f"{path}: deve ser {expected}, recebido {type(value).__name__}"
        ]

    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: enum inválido; deve ser um de {enum}")

    if expected == "object":
        for name in required:
            if name not in value:
                if path == "$":
                    errors.append(f"campo obrigatório ausente: '{name}'")
                else:
                    errors.append(
                        f"{_child_path(path, name)}: campo obrigatório ausente")

        for name, child in value.items():
            child_path = _child_path(path, name)
            child_schema = properties.get(name)
            if child_schema is not None:
                errors.extend(validate_value(
                    child_schema, child, child_path, allow_extra=allow_extra))
                continue
            if isinstance(additional, dict):
                errors.extend(validate_value(
                    additional, child, child_path, allow_extra=allow_extra))
            elif not allow_extra or additional is False:
                errors.append(f"{child_path}: campo não permitido")

    elif expected == "array" and isinstance(items, dict):
        for index, child in enumerate(value):
            errors.extend(validate_value(
                items, child, f"{path}[{index}]", allow_extra=allow_extra))

    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)):
        if "minimum" in bounds and value < bounds["minimum"]:
            errors.append(
                f"{path}: valor abaixo de minimum {bounds['minimum']}")
        if "maximum" in bounds and value > bounds["maximum"]:
            errors.append(
                f"{path}: valor acima de maximum {bounds['maximum']}")
        if "exclusiveMinimum" in bounds and value <= bounds["exclusiveMinimum"]:
            errors.append(
                f"{path}: valor deve ser maior que exclusiveMinimum "
                f"{bounds['exclusiveMinimum']}")
        if "exclusiveMaximum" in bounds and value >= bounds["exclusiveMaximum"]:
            errors.append(
                f"{path}: valor deve ser menor que exclusiveMaximum "
                f"{bounds['exclusiveMaximum']}")
    elif expected in ("integer", "number") and _matches_type(expected, value):
        errors.append(f"{path}: deve ser número finito")

    return errors


def _matches_type(expected: str, value: Any) -> bool:
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPE_MAP[expected])


def _child_path(path: str, name: str) -> str:
    if name.isidentifier():
        return f"{path}.{name}"
    return f"{path}[{name!r}]"
