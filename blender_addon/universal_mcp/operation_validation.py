"""Shared argument validation used before dry runs and real execution."""
import math
from .operation_schemas import SCHEMAS


def validate_arguments(operation, arguments):
    def check(schema, value, path):
        types = {"object": dict, "array": list, "string": str,
                 "number": (int, float), "integer": int, "boolean": bool}
        kind = schema.get("type")
        if kind in types and (not isinstance(value, types[kind]) or
                              kind in ("integer", "number") and isinstance(value, bool)):
            raise ValueError(path + " must be " + kind)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError(path + " must be finite")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(path + " must be one of " + str(schema["enum"]))
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    raise ValueError(path + "." + key + " is required")
            for key, val in value.items():
                if key in schema.get("properties", {}):
                    check(schema["properties"][key], val, path + "." + key)
        if isinstance(value, list):
            for index, val in enumerate(value):
                check(schema.get("items", {}), val, path + "[" + str(index) + "]")
    schema = SCHEMAS.get(operation, {"type": "object"})
    check(schema, arguments, operation)
    for key in ("location", "scale", "rotation", "color"):
        if key in arguments:
            if operation == "object_transform" and key == "rotation":
                continue  # effective mode also depends on the existing object
            expected = 4 if key == "rotation" and arguments.get("rotation_mode") == "AXIS_ANGLE" else 3
            if len(arguments[key]) != expected:
                raise ValueError(key + " must have " + str(expected) + " components")
