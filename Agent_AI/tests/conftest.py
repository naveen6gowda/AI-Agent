"""Test environment defaults — set BEFORE any app module is imported.

The client modules (tools.py, docker_tools.py, models.py) read their config
into module-level constants at import time, so these must exist first. Each
module's own load_dotenv() uses override=False, so values set here win over
the real .env on the production box — tests see the same deterministic
config everywhere (dev LXC, CI). The HTTP tests never touch the network:
respx intercepts by URL built from the modules' own constants.

Langfuse keys are forced empty so @observe tracing stays off during tests.
"""

import os
import tempfile

_DEFAULTS = {
    # keep synthetic approval/tool events out of the production audit trail
    "AUDIT_LOG_PATH": os.path.join(tempfile.gettempdir(),
                                   "sentinel-test-audit.log"),
    "PROXMOX_HOST": "pve.test",
    "PROXMOX_PORT": "8006",
    "PROXMOX_TOKEN_ID": "ci@pve!tester",
    "PROXMOX_TOKEN_SECRET": "dummy-secret",
    "PROXMOX_VERIFY_TLS": "false",
    "PROXMOX_DEFAULT_NODE": "pve",
    "HA_URL": "http://ha.test:8123",
    "HA_TOKEN": "dummy-token",
    "PORTAINER_URL": "https://docker.test:9443",
    "PORTAINER_API_KEY": "dummy-key",
    "PORTAINER_ENDPOINT_ID": "2",
    "PORTAINER_VERIFY_TLS": "false",
    "TELEGRAM_BOT_TOKEN": "000000:ci-dummy",
    "TELEGRAM_CHAT_ID": "1",
    "MLX_BASE_URL": "http://llm.test:1234/v1",
    "MLX_MODEL": "ci-model",
    "MLX_API_KEY": "dummy",
    "FIREFLY_URL": "http://firefly.test:8212",
    "FIREFLY_TOKEN": "dummy-firefly",
    "FINANCE_ASSET_ACCOUNT": "N26",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "LANGSMITH_TRACING": "false",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# Public mirror: the real catalog.yaml is private (gitignored). Tests that
# check "the real catalog" fall back to the committed template, so CI still
# validates the file people copy.
import catalog as _catalog  # noqa: E402

if not _catalog.DEFAULT_PATH.exists():
    _catalog.DEFAULT_PATH = _catalog.DEFAULT_PATH.with_name("catalog.example.yaml")
