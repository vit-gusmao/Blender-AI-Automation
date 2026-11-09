"""Valida manualmente CallToolRequest como o SDK faria."""
import traceback

import mcp.types as t

cases = [
    {"name": "blender_get_capabilities", "arguments": {}},
    {"name": "blender_get_capabilities"},
]
for params in cases:
    for mode, builder in (
        ("kwargs", lambda p: t.CallToolRequest(method="tools/call", **p)),
        ("validate", lambda p: t.CallToolRequest.model_validate(
            {"method": "tools/call", "params": p})),
    ):
        try:
            req = builder(params)
            print(f"OK   {mode}: {list(params)} ->",
                  type(req).__name__)
        except Exception:
            print(f"FAIL {mode}: {list(params)}")
            traceback.print_exc(limit=3)

# e o resultado que devolvemos?
try:
    res = t.CallToolResult(
        content=[t.TextContent(type="text", text="x")], isError=False)
    print("CallToolResult OK:", res.model_dump_json()[:120])
except Exception:
    print("CallToolResult FAIL:")
    traceback.print_exc(limit=3)
