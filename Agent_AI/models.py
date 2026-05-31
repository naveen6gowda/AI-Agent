"""
Model factory for HomelabSentinel.

Decided hybrid strategy:
  - agent_llm():  Claude Sonnet via Anthropic API. Multi-tool agent loops.
                  Used by: agent_v2/v3/v4, future Telegram bot (#10),
                  approval gate (#12).
  - helper_llm(): Local Gemma 3 4B via llama.cpp (OpenAI-compatible server
                  running in the "ollama"-named LXC). Single-shot tasks
                  with NO tool calling.
                  Used by: log summarization (#1), backup status digest
                  (#2), classification ("alertable?"), RAG generation (#15).

Why this split: Gemma 4B can't reliably call structured tools across
multi-turn loops — it'd hallucinate tool names or break the JSON schema
ToolNode expects. But for "summarize this log in one sentence" it's
excellent and free. Claude is reliable but ~$0.005/agent-run; we don't
want it summarizing every log line.

Every agent file should import from here, NOT instantiate ChatAnthropic
or ChatOpenAI directly. That way the day you bump the LXC RAM and want
qwen2.5:7b to run the agent locally, you change ONE line below.
"""

import os
from typing import Optional

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

load_dotenv()


# -------------------------------------------------------------------
# Config (env-driven so non-Python folks can tweak without code edits)
# -------------------------------------------------------------------
_ANTHROPIC_AGENT_MODEL = os.getenv("ANTHROPIC_AGENT_MODEL", "claude-sonnet-4-6")

# Defaults reflect this homelab's deployment (LXC 101 "ollama", llama-server
# started with `--alias active` on port 8383). Overridable via .env.
_LLAMACPP_BASE_URL = os.getenv("LLAMACPP_BASE_URL", "http://192.168.178.75:8383/v1")
_LLAMACPP_MODEL = os.getenv("LLAMACPP_MODEL", "active")
_LLAMACPP_API_KEY = os.getenv("LLAMACPP_API_KEY", "not-used")


# -------------------------------------------------------------------
# Public factory functions
# -------------------------------------------------------------------
def agent_llm(max_tokens: int = 2048, model: Optional[str] = None):
    """Return the LLM used for multi-tool agent loops.

    Use this in any file that calls `.bind_tools(...)` or runs a ReAct
    loop. Tool calling needs to be reliable across many turns — that's
    why we don't point this at the local model.

    Args:
        max_tokens: response cap. Keep modest; tool-calling responses
            are usually small. Big answers come from chaining, not one
            mega-response.
        model: optional override (e.g. "claude-opus-4-7" for hard cases,
            "claude-haiku-4-5-20251001" for speed/cost). Defaults to
            ANTHROPIC_AGENT_MODEL env var.
    """
    return ChatAnthropic(
        model=model or _ANTHROPIC_AGENT_MODEL,
        max_tokens=max_tokens,
    )


def helper_llm(temperature: float = 0.0, max_tokens: int = 512):
    """Return the local llama.cpp LLM for cheap single-shot helpers.

    Suitable for:
      - log summarization (#1)
      - classification ("is this metric alertable?")
      - RAG answer generation (#15) — embedding is separate
      - voice intent extraction (#8)

    NOT suitable for:
      - tool calling (Gemma 4B is unreliable at structured output)
      - multi-turn agent loops
      - anything where a wrong answer triggers a destructive action

    Args:
        temperature: 0.0 for deterministic summarization / classification,
            0.3–0.7 if you want some variety (creative writing, draft
            generation). Default 0 because almost all helper tasks here
            want consistency.
        max_tokens: response cap. Helpers should be short.
    """
    return ChatOpenAI(
        base_url=_LLAMACPP_BASE_URL,
        api_key=_LLAMACPP_API_KEY,
        model=_LLAMACPP_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60.0,
        # llama-server runs Gemma with --reasoning-budget 256, so by default
        # it spends the whole completion on `reasoning_content` and returns
        # `content=""`. Helper tasks here are single-shot (summarize,
        # classify) and don't benefit from chain-of-thought, so suppress it.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
