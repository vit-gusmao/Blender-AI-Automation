"""Servidor MCP principal (camada 3 do plano).

Ferramentas estruturadas (catálogo §6.1), recursos blender:// e templates
de prompt. Autorização aplicada ANTES do despacho (§14): a garantia vem do
código, não do System Prompt.

Compatibilidade de SDK: registra handlers tanto na API nova
(``add_request_handler``) quanto nos decoradores legados
(``@server.list_tools()``...) — o plano exige negociar a versão com cada
cliente (§2) e suportar implementações anteriores quando necessário.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import socket
import traceback
from typing import Any, Dict, List, Optional

from .authorization import LEVEL_NAMES, level_from_name, load_policy
from .audit_logger import AuditLogger
from .bridge_gateway import BridgeGateway
from .job_manager import JobManager
from .schemas import validate_arguments
from .settings import Settings, load_settings
from .tool_registry import (SERVER_AUTHORIZATION_KEY, ServerContext,
                            build_registry)

log = logging.getLogger(__name__)


def _envelope_text(envelope: Dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, default=str)


def _fail_envelope(error_type: str, message: str) -> Dict[str, Any]:
    return {"command_id": "", "status": "failed", "success": False,
            "data": None, "scene_revision": None,
            "error": {"type": error_type, "message": message,
                      "traceback": ""}}


class AuthorizationDenied(PermissionError):
    """Gate de autorização negado, com o tipo de erro estruturado.

    Levantada pelo gate único ``authorize_request`` (tools e resources):
    ``tools/call`` converte em envelope ``isError``; ``resources/read``
    propaga ao transporte como erro de protocolo (BUG-022).
    """

    def __init__(self, error_type: str, reason: str,
                 tool: Optional[str] = None):
        super().__init__(reason)
        self.error_type = error_type
        self.reason = reason
        self.tool = tool


def _classify_denial(reason: str) -> str:
    return "ApprovalRequired" if "aprovação explícita" in reason else "Unauthorized"


def maybe_start_bridge(settings: Settings, jobs: JobManager) -> None:
    """Sobe a Bridge em thread daemon quando configurado e inacessível."""
    probe = BridgeGateway(settings.bridge_url, settings.bridge_token())
    if probe.is_reachable():
        return
    if not settings.autostart_bridge:
        log.warning("Bridge %s inacessível e autostart desativado.",
                    settings.bridge_url)
        return
    from .bridge_server import BridgeServer

    server = BridgeServer(
        settings.bridge_host, settings.bridge_port, settings.bridge_token(),
        jobs, settings.queue_max, settings.command_timeout_s,
        settings.heartbeat_timeout_s,
    )
    server.start(daemon=True)
    log.info("Bridge iniciada automaticamente em %s", server.url)


def _params(request):
    """Extrai params do request (APIs nova e legada)."""
    return getattr(request, "params", request)


def _compat(handler):
    """Adapta a assinatura do handler entre gerações do SDK.

    SDK atual chama ``handler(ctx, typed_params)``; o legado chamava
    ``handler(request)``. O wrapper entrega sempre o request ao handler.
    """
    async def wrapper(a, b=None):
        request = b if b is not None else a
        return await handler(request)
    return wrapper


def create_server(settings: Optional[Settings] = None):
    """Cria o Server MCP (low-level API) + contexto de execução."""
    settings = settings or load_settings()
    policy = load_policy(settings)
    audit = AuditLogger(settings.logs_dir, settings.log_level,
                        log_arguments=policy.audit_arguments,
                        log_all=policy.audit_log_all)
    jobs = JobManager(settings.jobs_db)
    bridge = BridgeGateway(
        settings.bridge_url, settings.bridge_token(),
        command_timeout_s=settings.command_timeout_s,
        poll_interval_s=settings.poll_interval_s,
    )
    maybe_start_bridge(settings, jobs)

    ctx = ServerContext(settings=settings, bridge=bridge, jobs=jobs,
                        policy=policy, audit=audit)
    registry = build_registry(ctx)

    session_level = level_from_name(
        os.environ.get("BLENDER_MCP_SESSION_LEVEL"), policy.default_level
    )
    ctx.session_level = session_level

    from mcp.server import Server
    from mcp.types import (CallToolResult, GetPromptResult, ListPromptsResult,
                           ListResourceTemplatesResult, ListResourcesResult,
                           ListToolsResult, Prompt, PromptArgument,
                           PromptMessage, ReadResourceResult, Resource,
                           ResourceTemplate, TextContent, TextResourceContents,
                           Tool)

    server = Server(settings.server_name)

    # -- gate único de autorização (tools e resources, §14) -----------------

    def authorize_request(kind: str, name: str, args: Dict[str, Any]):
        """Aplica os MESMOS gates a ``tools/call`` e ``resources/read``.

        BUG-022: um recurso é mapeado à ferramenta equivalente e autorizado
        com critérios idênticos — nível, política de python, validação de
        argumentos e aprovação explícita — antes de qualquer resolução.
        Levanta ``KeyError`` para URI de recurso desconhecida e
        ``AuthorizationDenied`` quando qualquer gate nega.

        Retorna ``(spec, tool_name, tool_args)`` já mapeados.
        """
        if kind == "resource":
            from .resources import resource_request  # noqa: PLC0415 — ciclo
            tool_name, tool_args = resource_request(name)
        else:
            tool_name, tool_args = name, dict(args)
        spec = registry.get(tool_name)
        if spec is None:
            raise AuthorizationDenied(
                "UnknownTool" if kind == "tool" else "Unauthorized",
                (f"ferramenta desconhecida: {name} "
                 f"(use blender_get_capabilities)" if kind == "tool" else
                 f"recurso sem ferramenta equivalente registrada: {name}"),
                tool_name)
        decision = policy.authorize(tool_name, spec.auth_level, session_level)
        if not decision.allowed:
            raise AuthorizationDenied("Unauthorized", decision.reason,
                                      tool_name)
        if (tool_name == "python_execute_blender"
                and not policy.python.allow_blender) or (
            tool_name == "python_execute_external"
            and not (policy.python.allow_external and policy.external_allowed)
        ):
            raise AuthorizationDenied(
                "PolicyDenied", "execução de código desabilitada por política",
                tool_name)
        errors = validate_arguments(spec.input_schema(), dict(tool_args))
        if errors:
            raise AuthorizationDenied("InvalidArguments", "; ".join(errors),
                                      tool_name)
        approval = policy.require_approval(
            tool_name, dict(tool_args), {"level": session_level})
        if not approval.allowed:
            raise AuthorizationDenied(_classify_denial(approval.reason),
                                      approval.reason, tool_name)
        return spec, tool_name, dict(tool_args)

    # -- tools ------------------------------------------------------------

    async def list_tools(request, context=None):
        return ListToolsResult(tools=[
            Tool(name=spec.name, description=spec.description,
                 inputSchema=spec.input_schema())
            for spec in registry.specs()
        ])

    async def call_tool(request, context=None):
        params = _params(request)
        name = params.name
        arguments = dict(params.arguments or {})

        def error_result(error_type: str, message: str) -> CallToolResult:
            envelope = _fail_envelope(error_type, message)
            content = TextContent(type="text",
                                  text=_envelope_text(envelope))
            return CallToolResult(content=[content], isError=True)

        try:
            spec, tool_name, _mapped_args = authorize_request(
                "tool", name, arguments)
        except AuthorizationDenied as exc:
            if exc.error_type != "InvalidArguments":
                audit.audit("tool_denied", tool=name, reason=exc.reason)
            return error_result(exc.error_type, exc.reason)

        dispatch_arguments = dict(arguments)
        explicitly_approved = dispatch_arguments.pop("approval", None) is True
        dispatch_arguments.pop(SERVER_AUTHORIZATION_KEY, None)
        dispatch_arguments[SERVER_AUTHORIZATION_KEY] = {
            "level": LEVEL_NAMES[int(session_level)],
            "approved": explicitly_approved,
        }

        audit.audit("tool_call", tool=tool_name, risk=spec.risk, arguments=arguments)
        try:
            result = await spec.handler(dispatch_arguments)
            is_envelope = (isinstance(result, dict)
                           and "command_id" in result and "success" in result)
            envelope = result if is_envelope else {
                "command_id": "", "status": "completed", "success": True,
                "data": result, "scene_revision": None, "error": None,
            }
            audit.audit("tool_result", tool=name,
                        status=envelope.get("status"),
                        success=bool(envelope.get("success")))
        except Exception as exc:  # noqa: BLE001 — erro estruturado p/ o modelo
            envelope = _fail_envelope(type(exc).__name__, str(exc))
            envelope["error"]["traceback"] = traceback.format_exc()[-2000:]
            audit.audit("tool_error", tool=name, error=str(exc))
        content = TextContent(type="text", text=_envelope_text(envelope))
        return CallToolResult(content=[content],
                              isError=not envelope.get("success", False))

    # SDK novo: params_type = classe dos PARAMS; legado: envelope Request.
    try:
        from mcp.types import (CallToolRequestParams, GetPromptRequestParams,
                               PaginatedRequestParams,
                               ReadResourceRequestParams)
        _list = PaginatedRequestParams  # tools/resources/prompts listagens
        params_types = {
            "tools/list": _list,
            "tools/call": CallToolRequestParams,
            "resources/list": _list,
            "resources/templates/list": _list,
            "resources/read": ReadResourceRequestParams,
            "prompts/list": _list,
            "prompts/get": GetPromptRequestParams,
        }
    except ImportError:  # SDK legado
        params_types = {}

    if hasattr(server, "add_request_handler") and params_types:
        server_add = server.add_request_handler
        server_add("tools/list", params_types["tools/list"],
                   _compat(list_tools))
        server_add("tools/call", params_types["tools/call"], _compat(call_tool))
    else:  # SDK legado — decoradores
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)

    # -- resources ----------------------------------------------------------

    from .resources import RESOURCES  # noqa: PLC0415 — evita ciclo no import

    async def list_resources(request, context=None):
        return ListResourcesResult(resources=[
            Resource(uri=r["uri"], name=r["name"],
                     description=r["description"], mimeType=r["mimeType"])
            for r in RESOURCES if "{" not in r["uri"]
        ])

    async def list_resource_templates(request, context=None):
        return ListResourceTemplatesResult(resourceTemplates=[
            ResourceTemplate(uriTemplate=r["uri"], name=r["name"],
                             description=r["description"])
            for r in RESOURCES if "{" in r["uri"]
        ])

    async def read_resource(request, context=None):
        from .resources import read_resource as resolve  # noqa: PLC0415

        uri = str(_params(request).uri)
        try:
            _spec, tool_name, tool_args = authorize_request("resource", uri,
                                                            {})
        except KeyError as exc:  # URI desconhecida → erro de protocolo
            raise ValueError(str(exc)) from exc
        except AuthorizationDenied as exc:
            audit.audit("resource_denied", uri=uri, tool=exc.tool,
                        reason=exc.reason)
            raise
        audit.audit("resource_read", uri=uri, tool=tool_name,
                    arguments=tool_args)
        # Metadados confiáveis: approved=True somente quando o PRÓPRIO
        # mapeamento do servidor carrega a aprovação do código fixo (ex.:
        # blender://materials); o nível da sessão continua obrigatório.
        trusted_approved = bool(tool_args.get("approval"))
        try:
            data = await resolve(uri, ctx, authorization={
                "level": LEVEL_NAMES[int(session_level)],
                "approved": trusted_approved})
        except KeyError as exc:
            audit.audit("resource_error", uri=uri, tool=tool_name,
                        error=str(exc))
            raise ValueError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — erro estruturado p/ transporte
            audit.audit("resource_error", uri=uri, tool=tool_name,
                        error=str(exc))
            raise
        audit.audit("resource_result", uri=uri, tool=tool_name, success=True)
        return ReadResourceResult(contents=[TextResourceContents(
            uri=uri, mimeType="application/json",
            text=json.dumps(data, ensure_ascii=False, default=str))])

    if hasattr(server, "add_request_handler") and params_types:
        server_add("resources/list", params_types["resources/list"],
                   _compat(list_resources))
        server_add("resources/templates/list",
                   params_types["resources/templates/list"],
                   _compat(list_resource_templates))
        server_add("resources/read", params_types["resources/read"],
                   _compat(read_resource))
    else:
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
        server.list_resource_templates()(list_resource_templates)

    # -- prompts -------------------------------------------------------------

    from .prompts import PROMPT_DEFS  # noqa: PLC0415

    async def list_prompts(request, context=None):
        out = []
        for p in PROMPT_DEFS:
            args = ([PromptArgument(
                name="objective",
                description="Objetivo da tarefa (prompt blender_task)",
                required=False)]
                if p["name"] == "blender_task" else [])
            out.append(Prompt(name=p["name"], description=p["description"],
                              arguments=args))
        return ListPromptsResult(prompts=out)

    async def get_prompt(request, context=None):
        from .prompts import get_prompt as resolve  # noqa: PLC0415

        params = _params(request)
        try:
            messages = resolve(params.name,
                               dict(params.arguments or {}),
                               settings.root)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        return GetPromptResult(
            description=f"Prompt {params.name} — Universal Blender MCP",
            messages=[PromptMessage(
                role=m["role"],
                content=TextContent(type="text", text=m["content"]))
                for m in messages])

    if hasattr(server, "add_request_handler") and params_types:
        server_add("prompts/list", params_types["prompts/list"],
                   _compat(list_prompts))
        server_add("prompts/get", params_types["prompts/get"],
                   _compat(get_prompt))
    else:
        server.list_prompts()(list_prompts)
        server.get_prompt()(get_prompt)

    server.blender_ctx = ctx  # type: ignore[attr-defined]
    server.blender_registry = registry  # type: ignore[attr-defined]
    server.blender_session_level = session_level  # type: ignore[attr-defined]
    return server


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class _BearerMiddleware:
    """Exige ``Authorization: Bearer <token>`` em todo request HTTP.

    Comparação em tempo constante (BUG-032). O stdio NÃO passa por aqui:
    sua fronteira continua sendo o nível de sessão do servidor.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = ("Bearer " + token).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        value = b""
        for key, header_value in scope.get("headers") or []:
            if key == b"authorization":
                value = header_value
                break
        if not hmac.compare_digest(bytes(value), self._expected):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body",
                        "body": b'{"error": "authentication required"}'})
            return
        await self.app(scope, receive, send)


class _BodyGuardMiddleware:
    """Limite de tamanho e schema de request para POST JSON (BUG-032).

    Corpo maior que ``max_bytes`` → 413; JSON não-objeto (ou inválido) em
    POST → 400. GET/DELETE (SSE/encerramento de sessão) não possuem corpo
    relevante e passam direto.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send, status: int, message: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(message)).encode())]})
        await send({"type": "http.response.body", "body": message})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        for key, value in scope.get("headers") or []:
            if key == b"content-length":
                declared = value.decode("ascii", "replace").strip()
                if declared.isdigit() and int(declared) > self.max_bytes:
                    return await self._reject(
                        send, 413, b'{"error": "request body exceeds byte limit"}')
                break

        chunks: list[bytes] = []
        total = 0
        oversized = False
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                total += len(body)
                if total > self.max_bytes:
                    oversized = True
                    break
                chunks.append(body)
                if not message.get("more_body"):
                    break
            elif message["type"] == "http.disconnect":
                return
            else:
                break
        if oversized:
            return await self._reject(
                send, 413, b'{"error": "request body exceeds byte limit"}')

        data = b"".join(chunks)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            return await self._reject(
                send, 400, b'{"error": "invalid JSON request"}')
        if not isinstance(parsed, dict):
            return await self._reject(
                send, 400, b'{"error": "JSON request must be an object"}')

        buffered = bytearray(data)

        async def replay_receive():
            if buffered:
                body = bytes(buffered)
                buffered.clear()
                return {"type": "http.request", "body": body,
                        "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    try:
        return all(ipaddress.ip_address(info[4][0]).is_loopback
                   for info in infos)
    except ValueError:
        return False


async def run_stdio(server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


async def run_streamable_http(server, settings: Settings) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover — depende do ambiente
        raise RuntimeError(
            "transport streamable-http requer 'uvicorn' instalado "
            "(pip install uvicorn): " + str(exc)
        ) from exc

    if not hasattr(server, "streamable_http_app"):
        raise RuntimeError(
            "SDK mcp instalado não suporta streamable_http_app; use stdio")

    # BUG-032: padrão seguro — loopback pode gerar token local; binding fora
    # de loopback exige token EXPLICITAMENTE configurado (env ou arquivo).
    loopback = _is_loopback_host(settings.http_host)
    token = settings.http_auth_token(generate=loopback)
    if not token:
        raise SystemExit(
            "binding MCP HTTP fora de loopback exige autenticação "
            f"configurada (variável {settings.http_auth_token_env} ou "
            f"arquivo {settings.http_token_file})")

    app = server.streamable_http_app(json_response=True)
    app = _BearerMiddleware(
        _BodyGuardMiddleware(app, settings.http_max_body_bytes), token)
    config = uvicorn.Config(app, host=settings.http_host,
                            port=settings.http_port, log_level="info")
    await uvicorn.Server(config).serve()


def main_serve(transport: Optional[str] = None) -> None:
    settings = load_settings()
    server = create_server(settings)
    transport = transport or settings.transport
    level = server.blender_session_level  # type: ignore[attr-defined]
    log.info("servidor %s v%s | transporte=%s | nível de sessão=%s | %d ferramentas",
             settings.server_name, settings.version, transport,
             LEVEL_NAMES.get(int(level)),
             len(server.blender_registry))  # type: ignore[attr-defined]
    if transport == "stdio":
        asyncio.run(run_stdio(server))
    elif transport in ("http", "streamable-http"):
        asyncio.run(run_streamable_http(server, settings))
    else:
        raise SystemExit(f"transporte desconhecido: {transport}")
