"""Valida o surface de tools/call por versão e mostra o erro concreto."""
import mcp_types
from mcp_types.methods import CLIENT_REQUESTS, _REQUEST_STUB, _body

versions = sorted({v for (_, v) in CLIENT_REQUESTS})
print("versões:", versions)
for version in versions:
    model = CLIENT_REQUESTS.get(("tools/call", version))
    print("\n==", version, "->", model)
    if model is None:
        continue
    for params in ({"name": "blender_get_capabilities", "arguments": {}},
                   {"name": "blender_get_capabilities"}):
        try:
            model.model_validate({**_REQUEST_STUB, **_body("tools/call", params)},
                                 by_name=False)
            print("  OK  ", list(params))
        except Exception as exc:  # noqa: BLE001
            print("  FAIL", list(params), "->", str(exc).splitlines()[1:4])
