"""Confirma nomes de método/constantes e resultados esperados no novo SDK."""
import inspect

from mcp.server.lowlevel import server as ll

src = inspect.getsource(ll)
consts = [line.strip() for line in src.splitlines()
          if line.strip().startswith("METHOD_")]
print("CONSTANTS:")
for c in consts:
    print("  ", c)

# trecho do decorador antigo → como add_request_handler é usado internamente
hits = [l for l in src.splitlines() if "add_request_handler(" in l][:12]
print("\ninternal uses:")
for h in hits:
    print("  ", h.strip())

# streamable http manager existe?
try:
    from mcp.server import streamable_http_manager as shm
    print("\nstreamable_http_manager OK:",
          [n for n in dir(shm) if not n.startswith("_")])
    print(inspect.signature(shm.StreamableHTTPSessionManager.__init__))
except Exception as exc:  # noqa: BLE001
    print("\nstreamable_http_manager err:", exc)

# streamable_http_app
print("\nstreamable_http_app:", inspect.signature(ll.Server.streamable_http_app))
