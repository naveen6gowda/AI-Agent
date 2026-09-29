"""Compat shim: langfuse<3 imports from the legacy `langchain` namespace.

langfuse 2.x's LangChain callback does
    from langchain.callbacks.base import BaseCallbackHandler
    from langchain.schema.agent import AgentAction, AgentFinish
    from langchain.schema.document import Document
but langchain 1.x removed those paths (and we only depend on
langchain-core anyway). Every symbol it needs lives in langchain_core,
so register alias modules in sys.modules BEFORE langfuse.callback loads.

Import this module before `langfuse.callback` / `langfuse.decorators`
anywhere the LangChain callback handler should work. Without it the
handler import fails and graph/tool spans are silently dropped — that
is exactly the bug this file fixes (2026-07-15).

Delete this file once we move to langfuse v3 (whose LangChain handler
imports from langchain_core directly) — and its importers with it.
"""

import sys
import types

import langchain_core
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.documents import Document
from langchain_core.load.serializable import Serializable


def _alias(name: str, **attrs) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        mod.__doc__ = "langfuse_compat alias of langchain_core symbols"
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# Only shim when the real package is absent — never shadow an installed
# langchain that might provide these paths itself.
try:
    import langchain  # noqa: F401
except ImportError:
    _root = _alias("langchain", __version__=langchain_core.__version__)
    _root.callbacks = _alias("langchain.callbacks")
    _root.callbacks.base = _alias("langchain.callbacks.base",
                                  BaseCallbackHandler=BaseCallbackHandler)
    _root.schema = _alias("langchain.schema")
    _root.schema.agent = _alias("langchain.schema.agent",
                                AgentAction=AgentAction,
                                AgentFinish=AgentFinish)
    _root.schema.document = _alias("langchain.schema.document",
                                   Document=Document)
    _root.load = _alias("langchain.load")
    _root.load.serializable = _alias("langchain.load.serializable",
                                     Serializable=Serializable)
