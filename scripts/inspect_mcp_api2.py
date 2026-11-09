"""Inspeciona handlers do novo SDK: add_request_handler + SessionMessage/stdio."""
import inspect

from mcp.server.lowlevel import server as ll
from mcp.server.lowlevel import helper_types

print("== helper_types:", [n for n in dir(helper_types) if not n.startswith("_")])
print()
sig = inspect.signature(ll.Server.add_request_handler)
print("add_request_handler", sig)
print()
print("run", inspect.signature(ll.Server.run))
print("create_initialization_options",
      inspect.signature(ll.Server.create_initialization_options))
print()
# request types disponíveis
import mcp.types as t

req = [n for n in dir(t) if n.endswith("Request")]
print("Request types:", req)
notif = [n for n in dir(t) if n.endswith("Notification")]
print("Notification types:", notif)
print()
import asyncio
import contextlib


@contextlib.contextmanager
def grab():
    s = ll.Server("probe")
    captured = {}

    orig = s.add_request_handler

    def spy(request_type, handler):
        captured[request_type.__name__ if hasattr(request_type, "__name__")
                else str(request_type)] = handler
        return orig(request_type, handler)

    s.add_request_handler = spy
    try:
        yield s, captured
    finally:
        pass


# como o stdio_server entrega as streams
from mcp.server import stdio as stdio_mod

print("stdio_server:", inspect.signature(stdio_mod.stdio_server))
src = inspect.getsource(stdio_mod.stdio_server)
print(src[:1200])
