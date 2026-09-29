"""mcp_server.py — Sentinel's MCP server (Phase 4).

Exposes the SAME tool registry the LangGraph agent binds
(registry.TOOLS) to any MCP client — Claude Code, Claude Desktop,
another agent — over streamable HTTP with bearer-token auth.

Safety is enforced SERVER-SIDE, never delegated to the client:

  - The destructive set comes from catalog.destructive_tools(), the
    exact same gate the in-process agent uses.
  - A destructive call sends a Telegram approval card and blocks until
    the operator answers — DEFAULT-DENY on timeout or silence.
  - Telegram's getUpdates allows a single consumer, and sentinel_bot
    owns it. This server therefore never polls Telegram: it sends the
    card, and the BOT (owner of the update stream) writes the tapped
    decision to var/approvals/<id>.json, which we watch. File IPC,
    single poller preserved.
  - Every call and every decision is appended to var/audit.log.

Config (.env): MCP_SERVER_PORT (default 8765), MCP_SERVER_TOKEN
(required to serve), MCP_APPROVAL_TIMEOUT_S (default 120).

Run:  uv run python mcp_server.py          (systemd: sentinel-mcp.service)
"""

import contextlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import anyio
import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Resource, TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount

from catalog import destructive_tools
from registry import TOOLS, TOOLS_BY_NAME
from tools import _audit

load_dotenv()

# ── Langfuse tracing (optional; same pattern as the agent) ──────────
# One trace per MCP tool call, so external clients (Claude Code, Hermes)
# using this server show up in Langfuse next to the bot's agent traces.
try:
    # isort: off
    import langfuse_compat  # noqa: F401  (must precede langfuse imports)
    from langfuse.decorators import langfuse_context as _lf_ctx
    from langfuse.decorators import observe as _observe
    # isort: on
    _LF_ON = bool(os.getenv("LANGFUSE_PUBLIC_KEY")
                  and os.getenv("LANGFUSE_SECRET_KEY"))
except Exception:
    _LF_ON = False
    _lf_ctx = None

    def _observe(*_a, **_k):
        if _a and len(_a) == 1 and callable(_a[0]) and not _k:
            return _a[0]

        def _decorate(_fn):
            return _fn
        return _decorate
print("[mcp] Langfuse tracing:", "on" if _LF_ON else "off")

_HERE = Path(__file__).parent
APPROVALS_DIR = _HERE / "var" / "approvals"
AUDIT_LOG = _HERE / "var" / "audit.log"
CATALOG_PATH = _HERE / "catalog.yaml"

PORT = int(os.getenv("MCP_SERVER_PORT", "8765"))
APPROVAL_TIMEOUT_S = float(os.getenv("MCP_APPROVAL_TIMEOUT_S", "120"))


# ── approval via the bot (file IPC; see module docstring) ───────────


def request_operator_approval(name: str, arguments: dict) -> str:
    """Send an approval card; block for the operator's decision.

    Returns "approved", "denied", or "timeout". Anything but an
    explicit tap on Approve — timeout, send failure, malformed file —
    comes back as not-approved. Default-deny, same contract as the
    in-process gate.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
    if not (token and chat_id):
        return "denied"

    rid = uuid.uuid4().hex[:12]
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    decision_file = APPROVALS_DIR / f"{rid}.json"

    text = (
        "🔐 MCP client requests a DESTRUCTIVE action\n\n"
        f"tool: {name}\n"
        f"args: {json.dumps(arguments, default=str)[:500]}\n\n"
        f"No answer in {int(APPROVAL_TIMEOUT_S)}s = denied."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"mcpapp:{rid}:approved"},
        {"text": "⛔ Deny", "callback_data": f"mcpapp:{rid}:denied"},
    ]]}
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return "denied"

    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        if decision_file.exists():
            try:
                decision = json.loads(decision_file.read_text()).get("decision")
            except (OSError, ValueError):
                decision = None
            with contextlib.suppress(OSError):
                decision_file.unlink()
            return decision if decision in ("approved", "denied") else "denied"
        time.sleep(1.0)
    return "timeout"


# ── dispatch — the testable core ────────────────────────────────────


@_observe(name="mcp-tool", capture_input=False, capture_output=True)
def dispatch(name: str, arguments: dict, approval_fn=None) -> dict:
    """Execute one tool call under the server-side gate.

    Destructive tools (per catalog policy) require an explicit
    "approved" from approval_fn before the tool runs; everything else
    is a refusal. Read tools run directly. Never raises — errors come
    back as {"error": ...} dicts, like the tools themselves.
    """
    if _LF_ON and _lf_ctx is not None:
        try:
            _lf_ctx.update_current_trace(
                name=f"mcp:{name}",
                input={"tool": name, "arguments": arguments},
                tags=["mcp", name],
            )
        except Exception:
            pass
    tool_fn = TOOLS_BY_NAME.get(name)
    if tool_fn is None:
        return {"error": f"unknown tool: {name}"}

    if name in destructive_tools():
        decision = (approval_fn or request_operator_approval)(name, arguments)
        _audit("mcp_approval", {"tool": name, "args": arguments,
                                "decision": decision})
        if decision != "approved":
            return {"error": "REFUSED by operator", "decision": decision,
                    "hint": "destructive tools need Telegram approval"}

    _audit("mcp_tool_call", {"tool": name, "args": arguments})
    try:
        result = tool_fn.invoke(arguments)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return result if isinstance(result, dict) else {"result": result}


def _input_schema(lc_tool) -> dict:
    """JSON schema for an MCP Tool from a LangChain tool."""
    try:
        schema = lc_tool.get_input_schema().model_json_schema()
        schema.pop("title", None)
        return schema
    except Exception:
        return {"type": "object", "properties": dict(lc_tool.args)}


# ── MCP wiring ──────────────────────────────────────────────────────

server = Server("sentinel")


@server.list_tools()
async def list_tools() -> list[Tool]:
    gated = destructive_tools()
    out = []
    for t in TOOLS:
        desc = t.description or ""
        if t.name in gated:
            desc = "⚠️ DESTRUCTIVE — pauses for operator approval on Telegram. " + desc
        out.append(Tool(name=t.name, description=desc,
                        inputSchema=_input_schema(t)))
    return out


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # dispatch blocks (tool I/O; approval waits up to 120s) — keep the
    # event loop free.
    result = await anyio.to_thread.run_sync(dispatch, name, arguments or {})
    return [TextContent(type="text", text=json.dumps(result, default=str))]


@server.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(uri="sentinel://catalog", name="Service catalog",
                 description="Homelab topology + criticality + policy (YAML)",
                 mimeType="text/yaml"),
        Resource(uri="sentinel://audit-log", name="Audit log (tail)",
                 description="Last 100 audit events (JSON lines)",
                 mimeType="application/jsonl"),
    ]


@server.read_resource()
async def read_resource(uri) -> str:
    u = str(uri)
    if u == "sentinel://catalog":
        return CATALOG_PATH.read_text(encoding="utf-8")
    if u == "sentinel://audit-log":
        try:
            lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-100:]
        except OSError:
            lines = []
        return "\n".join(lines)
    raise ValueError(f"unknown resource: {u}")


# ── ASGI app: bearer auth in front of streamable HTTP ───────────────


class BearerAuth:
    """Minimal ASGI middleware: every request needs the shared token."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Starlette Mount 307-redirects /mcp -> /mcp/; some MCP
            # clients refuse to follow POST redirects. Normalize here.
            if scope.get("path") == "/mcp":
                scope = dict(scope)
                scope["path"] = "/mcp/"

            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(auth.encode(), f"Bearer {self.token}".encode()):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app() -> BearerAuth:
    token = os.getenv("MCP_SERVER_TOKEN", "")
    if not token:
        raise SystemExit("MCP_SERVER_TOKEN not set — refusing to serve unauthenticated")

    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            print(f"[mcp] sentinel-mcp on :{PORT} — "
                  f"{len(TOOLS)} tools, gate: {sorted(destructive_tools())}")
            yield

    starlette = Starlette(
        routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
    return BearerAuth(starlette, token)


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="warning")
