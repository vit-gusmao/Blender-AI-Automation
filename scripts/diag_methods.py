"""Show the supported low-level server registration API."""
from __future__ import annotations

import argparse
import inspect

from mcp.server import Server
from mcp.types import CallToolRequestParams, PaginatedRequestParams


parser = argparse.ArgumentParser()
parser.add_argument("--timeout", type=float, default=10.0)
parser.parse_args()
print("Server.add_request_handler:", inspect.signature(Server.add_request_handler))
print("tools/list params:", PaginatedRequestParams.__name__)
print("tools/call params:", CallToolRequestParams.__name__)

