"""Inspeciona a API do SDK mcp instalado (decoradores/dispatch)."""
import inspect

import mcp
from mcp import server as server_mod

print("mcp version:", getattr(mcp, "__version__", "?"))
print("server module file:", server_mod.__file__)

ServerCls = getattr(server_mod, "Server", None)
print("Server class:", ServerCls)
if ServerCls:
    methods = [m for m, _ in inspect.getmembers(ServerCls, inspect.isfunction)
               if not m.startswith("_")]
    print("Server methods:", methods)

try:
    from mcp.server import fastmcp
    print("fastmcp:", fastmcp.FastMCP)
    fm_methods = [m for m, _ in inspect.getmembers(fastmcp.FastMCP, inspect.isfunction)
                  if not m.startswith("_")]
    print("FastMCP methods:", fm_methods[:40])
except Exception as exc:  # noqa: BLE001
    print("fastmcp err:", exc)

try:
    from mcp.server import stdio
    print("stdio ok:", [n for n in dir(stdio) if not n.startswith("_")])
except Exception as exc:  # noqa: BLE001
    print("stdio err:", exc)

import importlib

for candidate in ("mcp.server.lowlevel", "mcp.server.lowlevel.server"):
    try:
        mod = importlib.import_module(candidate)
        print(candidate, "->", [n for n in dir(mod) if not n.startswith("_")][:20])
    except Exception as exc:  # noqa: BLE001
        print(candidate, "err:", exc)
