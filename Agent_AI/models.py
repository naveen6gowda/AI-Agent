"""
Model factory for HomelabSentinel.

SINGLE-LLM design (2026-06-30):
  There is now exactly ONE language model in this whole system — the local
  MLX model running on the MacBook, served over an OpenAI-compatible
  endpoint (e.g. `mlx_lm.server` / vllm-mlx, started with --host 0.0.0.0).

  - agent_llm():  the SAME MLX model, used for the multi-tool agent loop
                  (Telegram bot #10, approval gate #12, voice).
  - helper_llm(): the SAME MLX model, used for cheap single-shot helpers
                  (log summaries #1, backup digest #2, "alertable?"
                  classification, RAG generation #15, voice intents #8).

History: we used to run a hybrid — Claude Sonnet via the Anthropic API for
the agent loop, plus a separate local Gemma (llama.cpp) for helpers. Both
the Anthropic path and the separate llama.cpp "helper" box have been removed
on purpose: the account was cost-sensitive and the second box was an extra
moving part that kept timing out. Everything now points at the one MLX model.

Config is env-driven (.env) so you can repoint without code edits:
  MLX_BASE_URL   OpenAI-compatible base, e.g. http://llm.lan:8000/v1
  MLX_MODEL      model id the server reports (curl $MLX_BASE_URL/models)
  MLX_API_KEY    bearer token the server requires

Every file should import agent_llm()/helper_llm() from here and NEVER
instantiate ChatOpenAI/ChatAnthropic directly — that keeps "the one model"
a one-line change.
"""

import os

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()


# -------------------------------------------------------------------
# Config — the one model (MacBook MLX server, OpenAI-compatible)
# -------------------------------------------------------------------
_MLX_BASE_URL = os.getenv("MLX_BASE_URL", "http://llm.lan:8000/v1")
_MLX_MODEL = os.getenv("MLX_MODEL", "mlx-community/Qwen3.5-9B-MLX-8bit")
_MLX_API_KEY = os.getenv("MLX_API_KEY", "not-used")

# The MacBook serves over Wi-Fi and the server is single-request, so cold
# loads / a momentarily busy box can be slow. Give requests room and retry
# transient connection blips instead of surfacing them as hard errors.
_AGENT_TIMEOUT = float(os.getenv("MLX_AGENT_TIMEOUT", "120"))
_HELPER_TIMEOUT = float(os.getenv("MLX_HELPER_TIMEOUT", "90"))
_MAX_RETRIES = int(os.getenv("MLX_MAX_RETRIES", "2"))
# Reasoning models (e.g. qwen3.6-27b-mtp) spend a few hundred tokens thinking
# before any answer, so a tiny helper budget yields EMPTY content. Floor the
# helper cap so the answer survives the reasoning. max_tokens is only a cap —
# non-reasoning models still stop early, so this never wastes generation.
# (Since 2026-07-23 helper_llm also disables thinking outright via
# reasoning_effort="none"; the floor stays as belt-and-braces for models
# that ignore that field.)
_HELPER_MIN_TOKENS = int(os.getenv("MLX_HELPER_MIN_TOKENS", "0"))

# The operator picks the active model at runtime via the Telegram /model
# command; the choice is persisted here so it survives bot restarts and is
# shared by the agent + the monitors (the server keeps one model hot, so we
# don't want them disagreeing). Empty file / missing -> fall back to MLX_MODEL.
_ACTIVE_MODEL_FILE = os.getenv("ACTIVE_MODEL_FILE",
                               os.path.join(os.path.dirname(__file__),
                                            "var", "active_model.txt"))


def get_active_model() -> str:
    """The model id currently selected (via /model), else the MLX_MODEL default."""
    try:
        with open(_ACTIVE_MODEL_FILE) as f:
            chosen = f.read().strip()
        if chosen:
            return chosen
    except OSError:
        pass
    return _MLX_MODEL


def set_active_model(model_id: str) -> None:
    """Persist the operator's /model choice. Used by the Telegram bot.

    Atomic (write + rename): every monitor reads this file on each run, and
    a reader catching it half-written would fall back to MLX_MODEL."""
    tmp = f"{_ACTIVE_MODEL_FILE}.tmp"
    with open(tmp, "w") as f:
        f.write(model_id.strip())
    os.replace(tmp, _ACTIVE_MODEL_FILE)


def list_models() -> list:
    """Every model the server advertises at /v1/models (sorted ids).

    Returns [] if the server is unreachable / the key is wrong — callers
    surface that to the operator rather than crashing.
    """
    try:
        r = httpx.get(
            f"{_MLX_BASE_URL}/models",
            headers={"Authorization": f"Bearer {_MLX_API_KEY}"},
            timeout=15.0,
        )
        r.raise_for_status()
        ids = (m["id"] for m in r.json().get("data", []) if m.get("id"))
        # Drop embedding models — they can't run the chat/agent loop, so they
        # shouldn't appear in the /model picker.
        return sorted(i for i in ids if "embed" not in i.lower())
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def probe_model(model_id: str, timeout: float = 180.0):
    """Ask the server to actually RUN a model, not just list it.

    LM Studio lists every downloaded model at /v1/models, including ones this
    machine cannot execute — those JIT-load fine and then every request dies
    with 'Compute error'. Since the active model is global (agent + all
    monitors), persisting such a model bricks the whole system. A 2-token
    completion catches that before the switch.

    Returns None if the model answered, else a short human-readable reason.
    Timeout is generous: a cold JIT load of a big model takes a while.
    """
    try:
        r = httpx.post(
            f"{_MLX_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_MLX_API_KEY}"},
            json={"model": model_id,
                  "messages": [{"role": "user", "content": "Say OK"}],
                  "max_tokens": 8},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        return f"server unreachable: {type(e).__name__}: {e}"
    if r.status_code != 200:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        return str(detail)[:300]
    return None


def _mlx(temperature: float, max_tokens: int, timeout: float, model=None,
         reasoning_effort: str | None = None):
    """Build a ChatOpenAI client pointed at the active local model."""
    return ChatOpenAI(
        base_url=_MLX_BASE_URL,
        api_key=_MLX_API_KEY,
        model=model or get_active_model(),
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=_MAX_RETRIES,
        reasoning_effort=reasoning_effort,
    )


# -------------------------------------------------------------------
# Public factory functions
# -------------------------------------------------------------------
# The agent path needs the same thinking-off switch the helpers got in
# 2026-07-23 — it was never applied here, and that cost 7 of 12 eval cases.
# Measured 2026-09-01 on qwen3.5-9b, final-answer turn after a tool result:
#   reasoning on  -> finish=tool_calls, content="" (it re-calls the tool), or
#                    finish=stop with the whole answer in `reasoning_content`,
#                    which langchain-openai drops -> AIMessage.content == ""
#   reasoning off -> finish=stop, 599 chars of real answer, and the FIRST turn
#                    still selects check_backups correctly, so tool-calling is
#                    unaffected.
# Set MLX_AGENT_REASONING to an effort level (or empty to let the model
# decide) if a future model needs its thinking back — then re-run evals/.
_AGENT_REASONING = os.getenv("MLX_AGENT_REASONING", "none")


def agent_llm(max_tokens: int = 2048, model=None, reasoning_effort: str | None = None):
    """The MLX model, configured for multi-tool agent loops.

    Use this in any file that calls `.bind_tools(...)` or runs a ReAct loop.

    Args:
        max_tokens: response cap. Keep modest; tool-calling responses are
            usually small — big answers come from chaining, not one mega-call.
        model: optional override of MLX_MODEL (rarely needed now that there
            is a single served model).
        reasoning_effort: override the MLX_AGENT_REASONING default. "none"
            disables thinking so the answer lands in `content`.
    """
    effort = _AGENT_REASONING if reasoning_effort is None else reasoning_effort
    return _mlx(temperature=0.0, max_tokens=max_tokens,
                timeout=_AGENT_TIMEOUT, model=model,
                reasoning_effort=effort or None)


def agent_provider() -> str:
    """Backend serving the agent loop. Always "mlx" now (single-LLM design).

    Kept so callers can keep gating Anthropic-only features (e.g. prompt-cache
    breakpoints) off — a local OpenAI-compatible server doesn't understand them.
    """
    return "mlx"


# Cache ONE handler per process: every CallbackHandler owns a background
# flush thread, and the bot calls helper_llm() repeatedly.
_HELPER_TRACING: dict = {"handler": None, "tried": False}


def _helper_tracing_callbacks() -> list:
    """Langfuse callback for helper calls, so monitor digests show up too.

    Returns [] when Langfuse isn't configured, when the integration is
    broken, or when we're already inside an agent trace (the agent's own
    handler captures nested calls — attaching a second one would create
    a duplicate standalone trace).
    """
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    try:
        # isort: off
        import langfuse_compat  # noqa: F401  (must precede langfuse.callback)
        from langfuse.decorators import langfuse_context
        # isort: on
        if langfuse_context.get_current_trace_id() is not None:
            return []
        if not _HELPER_TRACING["tried"]:
            _HELPER_TRACING["tried"] = True
            from pathlib import Path

            import __main__
            script = Path(getattr(__main__, "__file__", "") or "interactive").stem
            from langfuse.callback import CallbackHandler
            _HELPER_TRACING["handler"] = CallbackHandler(
                trace_name=f"helper:{script}",
                tags=["helper", script],
            )
    except Exception as e:
        print(f"[models] Langfuse helper tracing unavailable: {type(e).__name__}: {e}")
        return []
    h = _HELPER_TRACING["handler"]
    return [h] if h is not None else []


def helper_llm(temperature: float = 0.0, max_tokens: int = 512):
    """The MLX model, configured for cheap single-shot helpers.

    Same model as agent_llm() — just different defaults. Suitable for log
    summarization, "is this alertable?" classification, RAG answer
    generation, and voice intent extraction.

    Args:
        temperature: 0.0 for deterministic summarization / classification,
            0.3–0.7 for some variety. Default 0 — most helpers want consistency.
        max_tokens: response cap. Helpers should be short.
    """
    # Helpers want the answer, not the thinking: without this, qwen3.6
    # models burn the whole token budget in reasoning_content and return
    # EMPTY content (callers then see "[helper_llm returned empty]").
    # Verified A/B 2026-07-23: default -> 768 reasoning tokens + empty
    # content (finish=length); "none" -> clean one-sentence answer in 5 s.
    llm = _mlx(temperature=temperature,
               max_tokens=max(max_tokens, _HELPER_MIN_TOKENS),
               timeout=_HELPER_TIMEOUT,
               reasoning_effort="none")
    callbacks = _helper_tracing_callbacks()
    if callbacks:
        llm = llm.with_config({"callbacks": callbacks})
    return llm


# -------------------------------------------------------------------
# Availability + fallback (Phase 5)
# -------------------------------------------------------------------
class LLMUnavailable(RuntimeError):
    """The LLM server (and the fallback, if configured) is unreachable.

    Raised instead of a raw connection error so callers can give the
    operator an honest, actionable message. The monitors never see
    this: they are deterministic and do not need the LLM to detect
    problems — only summaries degrade.
    """


# A second OpenAI-compatible endpoint to try when the primary is down
# (e.g. a llama.cpp box, a second workstation, or a cloud proxy). Unset
# by default: local-first, no silent cloud spend.
_FALLBACK_BASE_URL = os.getenv("MLX_FALLBACK_BASE_URL", "")
_FALLBACK_MODEL = os.getenv("MLX_FALLBACK_MODEL", "")
_FALLBACK_API_KEY = os.getenv("MLX_FALLBACK_API_KEY", "not-used")


def llm_available(timeout: float = 3.0) -> bool:
    """Quick probe of the primary server — cheap enough to gate a turn on."""
    try:
        r = httpx.get(f"{_MLX_BASE_URL}/models",
                      headers={"Authorization": f"Bearer {_MLX_API_KEY}"},
                      timeout=timeout)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def has_fallback() -> bool:
    return bool(_FALLBACK_BASE_URL and _FALLBACK_MODEL)


def fallback_llm(temperature: float = 0.0, max_tokens: int = 2048,
                 timeout: float | None = None):
    """ChatOpenAI client for the fallback endpoint. Only call if
    has_fallback() — raises LLMUnavailable otherwise."""
    if not has_fallback():
        raise LLMUnavailable("no fallback LLM configured (MLX_FALLBACK_BASE_URL)")
    return ChatOpenAI(
        base_url=_FALLBACK_BASE_URL,
        api_key=_FALLBACK_API_KEY,
        model=_FALLBACK_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout or _AGENT_TIMEOUT,
        max_retries=1,
    )
