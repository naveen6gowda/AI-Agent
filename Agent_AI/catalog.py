"""
Service catalog loader for HomelabSentinel.

Reads catalog.yaml (next to this file by default) and gives every other
module a typed, validated view of the homelab. Phase 2 monitors (#1, #2,
#4) all consume this — they don't talk to Proxmox directly to discover
inventory; they call into here.

Why pydantic models instead of raw dicts:
  - Typos in YAML fail fast at load time, not three days later in a
    monitor at 3am.
  - Defaults from the YAML's `defaults:` block are merged automatically.
  - The agent's tools get a clean dict (.model_dump()) for the LLM.

USAGE
-----
    from catalog import load_catalog

    cat = load_catalog()
    for svc in cat.services:
        print(svc.name, svc.criticality)

    ha = cat.get("HomeAssistant")              # by name
    ollama = cat.by_vmid(101)                  # by vmid
    critical = cat.critical_services()         # filter helper
"""

from __future__ import annotations

from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

Criticality = Literal["critical", "high", "medium", "lab"]
RestartPolicy = Literal["ask", "auto", "never"]


# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------
class Endpoint(BaseModel):
    """One reachable network endpoint for a service.

    Used by #4 reachability sweep. The `auth_env` field is the name of
    an env var holding a bearer token (e.g. "HA_TOKEN"); if set, the
    sweep will include `Authorization: Bearer <value>` on the probe.
    """
    host: str
    port: int
    proto: Literal["http", "https", "tcp"] = "http"
    path: str = "/"
    name: str = "endpoint"
    verify_tls: bool = True
    auth_env: Optional[str] = None
    expected_status: int = 200


class Service(BaseModel):
    """One VM or LXC in the homelab."""
    vmid: int
    name: str
    kind: Literal["lxc", "qemu"]
    node: str
    criticality: Criticality
    purpose: str = ""
    max_backup_age_h: int = 168
    restart_policy: RestartPolicy = "ask"
    reachability_timeout_s: int = 5
    alert_on_status_change: bool = True
    endpoints: List[Endpoint] = Field(default_factory=list)


class ProxmoxHost(BaseModel):
    """Host-level config — disks, pools, backup storage, SSH access."""
    node: str
    ssh_user: str = "root"
    ssh_host: str
    ssh_port: int = 22
    disks_to_monitor: List[str] = Field(default_factory=list)
    zfs_pools: List[str] = Field(default_factory=list)
    backup_storage: List[str] = Field(default_factory=list)


class Defaults(BaseModel):
    max_backup_age_h: int = 168
    restart_policy: RestartPolicy = "ask"
    reachability_timeout_s: int = 5
    alert_on_status_change: bool = True


class Policy(BaseModel):
    """Approval policy as DATA (Phase 3): which tools the gate interrupts.

    Lives in catalog.yaml next to restart_policy, so safety rules are
    reviewable, diffable config — not code. The default equals the known
    destructive set: an older catalog without a policy block behaves
    exactly as before (never default-allow).
    """
    destructive_tools: List[str] = Field(default_factory=lambda: [
        "restart_lxc", "restart_vm", "restart_docker_container",
        "call_ha_service",
    ])


# ----------------------------------------------------------------------
# Docker monitor config — the "expected down" allowlist.
# ----------------------------------------------------------------------
class ExpectedDown(BaseModel):
    """One container that is SUPPOSED to sit in a non-running state.

    `name` is matched against the container name — exact, or an fnmatch
    glob so a compose project rename doesn't silently re-open the alert.
    `reason` is documentation for whoever reads this file in six months.
    """
    name: str
    reason: str = ""


class DockerConfig(BaseModel):
    """Config for the 10-minute Docker monitor (docker_tools.py).

    Exists because one-shot *init* containers are permanently `exited`
    by design — Dify's `init_permissions` chowns a volume, exits 0 and
    is declared `restart: "no"`, with its dependents gated on
    `service_completed_successfully`. Without this list the monitor
    pages about it every 10 minutes, forever.

    Deliberately an explicit allowlist and NOT a heuristic: a plain
    `docker stop jellyfin` also leaves "Exited (0)" behind, and that one
    must keep alerting.
    """
    expected_down: List[ExpectedDown] = Field(default_factory=list)
    expected_down_labels: Dict[str, str] = Field(default_factory=dict)


# ----------------------------------------------------------------------
# Alerting (alert_state.py) — how the monitors page
# ----------------------------------------------------------------------
class AlertingConfig(BaseModel):
    """Transition-based paging shared by reachability, guests and docker.

    `quiet_hours` is [start, end) in local hours: messages that carry only
    HIGH-criticality events are delivered silently inside it (no sound);
    anything critical always pages with sound. `remind_after_h` repeats a
    still-broken incident that often (0 = never remind).
    """
    quiet_hours: List[int] = Field(default_factory=lambda: [23, 7])
    remind_after_h: float = 6.0


# ----------------------------------------------------------------------
# ESPHome device watch (esphome_monitor.py)
# ----------------------------------------------------------------------
class EspDevice(BaseModel):
    """One ESPHome node as Home Assistant knows it.

    `mac` is the match key when present — it survives a rename in HA,
    which the display name does not. `monitor: false` parks a device:
    still listed by check_esphome_devices, never alerted on.
    """
    name: str
    mac: Optional[str] = None
    ip: Optional[str] = None
    monitor: bool = True
    note: str = ""


class EspHomeConfig(BaseModel):
    """Config for the ESPHome online/offline watch.

    `offline_grace_s` is how long a node must stay unavailable in HA
    before it counts as offline — long enough to ride out an OTA flash,
    a reboot, or HA restarting and reconnecting every node.
    """
    offline_grace_s: int = 180
    devices: List[EspDevice] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Energy (Feature #6) — optional. If absent, energy_assistant only
# offers discovery.
# ----------------------------------------------------------------------
class Tariff(BaseModel):
    """Cost-per-kWh tariff with optional off-peak window.

    If `off_peak_hours` is set, energy used during those hours of the day
    is billed at `off_peak_per_kwh`; everything else at `price_per_kwh`.
    Hours are 0–23 in local time.
    """
    currency: str = "USD"
    price_per_kwh: float = 0.0
    off_peak_per_kwh: Optional[float] = None
    off_peak_hours: List[int] = Field(default_factory=list)


class WatchedEntity(BaseModel):
    entity_id: str
    name: str = ""


class EnergyConfig(BaseModel):
    """How the energy assistant interprets your HA.

    `main_meter` is the whole-house energy accumulator (kWh, monotonic).
    `watch` lists per-device accumulators you want broken out.
    `tariff` is optional — if set, the digest includes cost estimates.
    """
    main_meter: Optional[str] = None
    tariff: Optional[Tariff] = None
    watch: List[WatchedEntity] = Field(default_factory=list)


class Catalog(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    services: List[Service]
    proxmox_host: ProxmoxHost
    policy: Policy = Field(default_factory=Policy)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    esphome: EspHomeConfig = Field(default_factory=EspHomeConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    energy: Optional[EnergyConfig] = None

    # ---------- lookup helpers (these are why we have a class) -------
    def get(self, name: str) -> Optional[Service]:
        """Find a service by case-insensitive name match."""
        n = name.lower()
        for s in self.services:
            if s.name.lower() == n:
                return s
        return None

    def by_vmid(self, vmid: int) -> Optional[Service]:
        for s in self.services:
            if s.vmid == vmid:
                return s
        return None

    def filter(self, criticality: Optional[Criticality] = None,
               node: Optional[str] = None,
               with_endpoints: bool = False) -> List[Service]:
        out = self.services
        if criticality:
            out = [s for s in out if s.criticality == criticality]
        if node:
            out = [s for s in out if s.node == node]
        if with_endpoints:
            out = [s for s in out if s.endpoints]
        return out

    def critical_services(self) -> List[Service]:
        return self.filter(criticality="critical")


# -------------------------------------------------------------------
# Loader
# -------------------------------------------------------------------
DEFAULT_PATH = Path(__file__).parent / "catalog.yaml"


def _merge_defaults(raw: dict) -> dict:
    """Apply top-level defaults to every service entry that omits a field."""
    defaults = raw.get("defaults", {}) or {}
    inheritable = ("max_backup_age_h", "restart_policy",
                   "reachability_timeout_s", "alert_on_status_change")
    for svc in raw.get("services", []):
        for k in inheritable:
            svc.setdefault(k, defaults.get(k))
            if svc[k] is None:
                svc.pop(k)
    return raw


@lru_cache(maxsize=1)
def load_catalog(path: Optional[str | Path] = None) -> Catalog:
    """Load and validate catalog.yaml. Cached — call clear_cache() to reload."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise FileNotFoundError(f"catalog not found: {p}")
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _merge_defaults(raw)
    try:
        return Catalog.model_validate(raw)
    except ValidationError as e:
        # Re-raise with a friendlier hint about WHERE to look in YAML.
        raise ValueError(
            f"catalog.yaml failed validation:\n{e}\n"
            f"Edit {p} and fix the listed fields."
        ) from e


def clear_cache() -> None:
    """Force the next load_catalog() call to re-read from disk."""
    load_catalog.cache_clear()


# ----------------------------------------------------------------------
# Policy accessors (Phase 3/4) — every gate in the system asks HERE.
# ----------------------------------------------------------------------
KNOWN_DESTRUCTIVE = frozenset(
    {"restart_lxc", "restart_vm", "restart_docker_container", "call_ha_service"})


def destructive_tools() -> frozenset:
    """The effective destructive-tool set: catalog policy UNION the
    built-in known set. Config can add gated tools; no config edit or
    load failure can ever un-gate the known destructive set."""
    try:
        return frozenset(load_catalog().policy.destructive_tools) | KNOWN_DESTRUCTIVE
    except Exception:
        return KNOWN_DESTRUCTIVE


def restart_forbidden(vmid: int) -> Optional[str]:
    """The guest's name when catalog.yaml says restart_policy: never.

    Enforced inside the restart tools themselves, so the rule no longer
    rests on the prompt alone: an approval tapped by mistake (or an MCP
    client that never saw the catalog) cannot reboot the router.
    A catalog that fails to load returns None — the approval gate still
    stands in front of every restart.
    """
    try:
        svc = load_catalog().by_vmid(vmid)
    except Exception:
        return None
    return svc.name if svc and svc.restart_policy == "never" else None


def docker_expected_down(name: str,
                         labels: Optional[Dict[str, str]] = None,
                         exit_code: Optional[int] = None) -> bool:
    """True if this container is ALLOWED to be sitting there non-running.

    Two ways to qualify — a name (glob) in catalog `docker.expected_down`,
    or a label pair from `docker.expected_down_labels`, so a future stack
    can declare itself ignorable without a Sentinel edit.

    Both require a CLEAN exit (code 0). A one-shot that fails its job —
    or a container in `created`/`dead`, which has no exit code — is still
    a finding.

    Fail-safe direction mirrors destructive_tools(): any load or config
    error returns False, so a broken catalog can only ever alert MORE,
    never silence something.
    """
    if exit_code != 0:
        return False
    try:
        cfg = load_catalog().docker
    except Exception:
        return False
    n = (name or "").lstrip("/")
    if any(fnmatch(n, entry.name) for entry in cfg.expected_down):
        return True
    labels = labels or {}
    return any(labels.get(k) == v for k, v in cfg.expected_down_labels.items())
