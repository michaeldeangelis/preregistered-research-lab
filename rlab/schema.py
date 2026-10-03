"""Minimal JSON-schema subset validator (type, required, properties, items, enum, minItems, pattern).

Enough for worker output contracts; returns a list of human-readable errors.
"""

from __future__ import annotations

import re

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def validate(obj, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        kinds = expected if isinstance(expected, list) else [expected]
        ok = any(
            isinstance(obj, _TYPES[k]) and not (k in ("integer", "number") and isinstance(obj, bool))
            for k in kinds
        )
        if not ok:
            return [f"{path}: expected {expected}, got {type(obj).__name__}"]
    if "pattern" in schema and isinstance(obj, str) and not re.search(schema["pattern"], obj):
        errors.append(f"{path}: {obj[:60]!r} does not match {schema['pattern']}")
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                errors.append(f"{path}: missing required '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                errors.extend(validate(obj[key], sub, f"{path}.{key}"))
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(obj):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors
