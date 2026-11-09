"""Validação pós-execução (§9.3): verificações objetivas via ferramentas MCP."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class ValidationEngine:
    """Critérios suportados:
    - {"kind": "object_exists", "name": "..."}
    - {"kind": "object_count_min", "count": N}
    - {"kind": "file_exists", "path": "..."}
    - {"kind": "tool_success", "tool": "...", "arguments": {...}}
    """

    def __init__(self, mcp, root=None, path_policy=None):
        self.mcp = mcp
        self.root = Path(root).resolve() if root is not None else None
        self.path_policy = path_policy
        if self.root is not None and path_policy is None:
            from universal_blender_mcp.authorization import load_policy
            from universal_blender_mcp.settings import load_settings
            self.path_policy = load_policy(load_settings(self.root)).paths

    async def evaluate(self, criteria: List[Dict[str, Any]]) -> Dict[str, Any]:
        results = []
        for criterion in criteria:
            kind = criterion.get("kind")
            try:
                if kind == "object_exists":
                    results.append(await self._object_exists(criterion["name"]))
                elif kind == "object_count_min":
                    results.append(await self._object_count_min(int(criterion["count"])))
                elif kind == "file_exists":
                    results.append(self._file_exists(criterion["path"]))
                elif kind == "tool_success":
                    results.append(await self._tool_success(
                        criterion["tool"], criterion.get("arguments", {})))
                else:
                    results.append({"kind": kind, "passed": False,
                                    "detail": "critério desconhecido"})
            except (KeyError, RuntimeError, ValueError, TypeError, OSError) as exc:
                results.append({"kind": kind, "passed": False,
                                "detail": f"erro: {exc}"})
        passed = all(r["passed"] for r in results)
        return {"passed": passed, "results": results}

    async def _object_exists(self, name: str) -> Dict[str, Any]:
        out = await self.mcp.call_tool('object_get_info', {'name': name})
        found = bool(out.get('parsed', {}).get('success')) and not out.get('is_error')
        return {"kind": "object_exists", "name": name, "passed": found,
                "detail": f"{'encontrado' if found else 'ausente'} na cena"}

    async def _object_count_min(self, count: int) -> Dict[str, Any]:
        out = await self.mcp.call_tool("scene_get_info", {})
        total = (out["parsed"].get("data") or {}).get("object_count", 0)
        success = bool(out.get('parsed', {}).get('success')) and not out.get('is_error')
        return {"kind": "object_count_min", "passed": success and total >= count,
                "detail": f"cena tem {total} objetos (mínimo {count})"}

    def _file_exists(self, path: str) -> Dict[str, Any]:
        if (self.root is None or self.path_policy is None or not self.path_policy.allow or
                not self.path_policy.is_allowed(path, self.root)):
            return {'kind': 'file_exists', 'passed': False, 'detail': 'PolicyDenied: file criterion outside allowed paths'}
        candidate = Path(path)
        candidate = candidate if candidate.is_absolute() else self.root / candidate
        exists = candidate.exists()
        return {"kind": "file_exists", "passed": exists,
                "detail": path}

    async def _tool_success(self, tool: str,
                            arguments: Dict[str, Any]) -> Dict[str, Any]:
        out = await self.mcp.call_tool(tool, arguments)
        parsed = out.get("parsed", {})
        success = bool(parsed.get("success")) and not out.get("is_error")
        return {"kind": "tool_success", "tool": tool, "passed": success,
                "detail": json.dumps(parsed.get("error") or "ok",
                                     ensure_ascii=False)[:300]}
