"""Entry point: ``python -m universal_blender_mcp``.

Subcomandos:
  serve   (padrão) servidor MCP — stdio ou streamable-http
  bridge  apenas a Bridge HTTP (fila de comandos)
  inspect imprime catálogo de ferramentas e configuração
  jobs    lista os últimos jobs persistidos
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_serve(args) -> int:
    from .server import main_serve

    main_serve(args.transport)
    return 0


def _cmd_bridge(args) -> int:
    from .bridge_server import run_bridge
    from .settings import load_settings

    run_bridge(load_settings())
    return 0


def _cmd_inspect(args) -> int:
    """Catálogo e configuração SEM efeitos colaterais (BUG-025).

    Não cria storage/, token da Bridge, jobs.db, logs ou Bridge: apenas
    carrega configurações e registra o catálogo em um contexto nulo.
    """
    import os

    from .authorization import level_from_name, load_policy
    from .settings import load_settings
    from .tool_registry import ServerContext, build_registry

    settings = load_settings(ensure_dirs=False)
    policy = load_policy(settings)
    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=policy, audit=None)
    registry = build_registry(ctx)
    level = level_from_name(
        os.environ.get("BLENDER_MCP_SESSION_LEVEL"), policy.default_level)
    payload = {
        "tools": [
            {"name": s.name, "category": s.category,
             "auth_level": s.auth_level.name.lower(), "risk": s.risk,
             "bridge": s.bridge}
            for s in registry.specs()
        ],
        "count": len(registry),
        "session_level": level.name.lower(),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_jobs(args) -> int:
    from .job_manager import JobManager
    from .settings import load_settings

    settings = load_settings()
    jobs = JobManager(settings.jobs_db)
    rows = jobs.list(status=args.status, limit=args.limit)
    for row in rows:
        print(f"{row['command_id']:<20} {row['status']:<10} {row['operation']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="universal_blender_mcp",
        description="Universal Blender MCP Server (Blender 3.6)",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="servidor MCP (padrão)")
    serve.add_argument("--transport", choices=["stdio", "http", "streamable-http"],
                       default=None)
    serve.set_defaults(func=_cmd_serve)

    bridge = sub.add_parser("bridge", help="executa apenas a Bridge HTTP")
    bridge.set_defaults(func=_cmd_bridge)

    inspect = sub.add_parser("inspect", help="lista ferramentas e configuração")
    inspect.set_defaults(func=_cmd_inspect)

    jobs = sub.add_parser("jobs", help="lista jobs persistidos")
    jobs.add_argument("--status", default=None)
    jobs.add_argument("--limit", type=int, default=20)
    jobs.set_defaults(func=_cmd_jobs)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("serve", "bridge", "inspect", "jobs"):
        argv = ["serve"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
