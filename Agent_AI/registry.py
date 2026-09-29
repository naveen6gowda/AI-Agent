"""registry.py — single source of truth for the agent's tool surface.

Phase 3: every LangChain tool is declared HERE, once, wrapping the raw
integration functions from the monitor/client modules. Consumers:

  - agent_v5_approval.py binds TOOLS to the LLM and dispatches through
    TOOLS_BY_NAME in its gated tool node;
  - the MCP server (Phase 4) will expose this same registry to external
    clients — same tools, one place to audit the whole surface.

Deliberately NOT here: which tools are destructive. That is policy, and
policy lives in catalog.yaml (Catalog.policy.destructive_tools) — the
graph reads it there. Destructive tools are wired as *_raw variants that
do NOT self-approve; gating happens at the graph level.
"""

from langchain_core.tools import tool

from backup_verifier import verify_backups as _verify_backups
from catalog import load_catalog
from docker_tools import (
    get_container as _get_container,
)
from docker_tools import (
    list_containers as _list_containers,
)
from docker_tools import (
    restart_container_raw as _restart_container_raw,
)
from energy_assistant import (
    discover_energy_entities as _discover_energy_entities,
)
from energy_assistant import (
    read_energy_summary as _read_energy_summary,
)
from esphome_monitor import scan_esphome as _scan_esphome
from guest_monitor import scan_guests as _scan_guests
from presence_assistant import (
    check_climate_state as _check_climate_state,
)
from presence_assistant import (
    check_light_state as _check_light_state,
)
from presence_assistant import (
    check_presence_state as _check_presence_state,
)
from presence_assistant import (
    discover_home_entities as _discover_home_entities,
)
from rag import search as _rag_search
from reachability import sweep_services as _sweep_services
from smart_monitor import scan_disks as _scan_disks
from speedtest_monitor import run_speedtest as _run_speedtest
from tools import (
    call_ha_service_raw as _call_ha_service_raw,
)
from tools import (
    check_proxmox_status as _check_proxmox_status,
)
from tools import (
    get_guest_mem_pct as _get_guest_mem_pct,
)
from tools import (
    get_ha_entity as _get_ha_entity,
)
from tools import (
    list_ha_entities as _list_ha_entities,
)
from tools import (
    list_integration_sensors as _list_integration_sensors,
)
from tools import (
    list_proxmox_guests as _list_proxmox_guests,
)
from tools import (
    list_proxmox_nodes as _list_proxmox_nodes,
)
from tools import (
    restart_lxc_raw as _restart_lxc_raw,
)
from tools import (
    restart_vm_raw as _restart_vm_raw,
)
from tools import (
    send_telegram_alert as _send_telegram_alert,
)
from voice import speak_on_alexa as _speak_on_alexa


# -------------------------------------------------------------------
# 1.  Tools exposed to the LLM (NB: destructive ones use *_raw —
#     gating happens at the graph level, not in the tool)
# -------------------------------------------------------------------
@tool
def get_service_catalog(criticality: str = "") -> dict:
    """Return the operator's catalog of services in this homelab.

    Use this FIRST to learn what exists and how important each service is
    BEFORE querying Proxmox or HA. The catalog tells you:
      - vmid, name, kind, criticality, purpose
      - max_backup_age_h (when to consider a backup stale)
      - restart_policy: ask / auto / never
      - endpoints (host:port + path) for reachability checks

    Args:
        criticality: filter to one of "critical", "high", "medium", "lab".
            Empty string returns everything.
    """
    cat = load_catalog()
    services = cat.filter(criticality=criticality or None)
    return {
        "count": len(services),
        "services": [s.model_dump(exclude_none=True) for s in services],
        "proxmox_host": cat.proxmox_host.model_dump(),
    }


@tool
def get_service(name: str = "", vmid: int = 0) -> dict:
    """Look up one service from the catalog by name OR vmid.

    Use when you know a service's name (e.g. "HomeAssistant") or vmid
    (e.g. 101) and want its full config — criticality, endpoints, policy.
    """
    cat = load_catalog()
    svc = cat.by_vmid(vmid) if vmid else cat.get(name) if name else None
    if not svc:
        return {"error": f"service not found (name={name!r}, vmid={vmid})"}
    return svc.model_dump(exclude_none=True)


@tool
def list_proxmox_nodes() -> dict:
    """List all Proxmox nodes. Use this FIRST if you don't know the node name."""
    return _list_proxmox_nodes()


@tool
def list_proxmox_guests(node: str = "") -> dict:
    """List LXCs and VMs on a Proxmox node. Use to discover vmids before
    calling check_proxmox_status. node="" uses PROXMOX_DEFAULT_NODE."""
    return _list_proxmox_guests(node or None)


@tool
def check_proxmox_status(node: str, vmid: int) -> dict:
    """Get status of a specific Proxmox LXC or VM.

    mem_pct is the REAL value from inside the guest when possible
    (mem_pct_source='guest'). If guest exec fails (qemu-guest-agent
    missing etc.), falls back to host-side and sets a mem_pct_warning."""
    return _check_proxmox_status(node, vmid)


@tool
def get_guest_mem_pct(vmid: int, kind: str = "") -> dict:
    """Get accurate memory utilization from inside a guest's /proc/meminfo.

    Use this when check_proxmox_status returned mem_pct_warning, or when
    you need to double-check a high mem_pct reading. NEVER trust the
    host-side mem_pct from a QEMU VM for restart decisions — it counts
    Linux page cache and looks 85%+ on healthy VMs.

    Args:
        vmid: target vmid
        kind: "lxc" or "qemu". Leave empty to auto-detect from catalog.
    """
    return _get_guest_mem_pct(vmid, kind or None)


@tool
def check_reachability(criticality: str = "") -> dict:
    """Probe every catalogued endpoint in parallel and report up/down.

    Use this when investigating "is X reachable?" or "are all my services
    healthy?" The probes are HTTP GET (or TCP connect, depending on
    endpoint config) — read-only and fast.

    Args:
        criticality: filter to one of "critical", "high", "medium", "lab".
            Empty string = sweep all services with endpoints.

    Returns a structured digest:
        {total, up, down, wrong_code, auth_missing,
         critical_down: [...], high_down: [...], results: [...]}

    For investigation, prefer this over calling get_ha_entity or
    check_proxmox_status one-by-one — one tool call probes everything.
    """
    return _sweep_services(criticality=criticality or None)


@tool
def check_disk_health() -> dict:
    """Run a SMART scan across every disk in catalog.proxmox_host.

    SSHes to the Proxmox host and runs `smartctl -aj` against each disk
    listed in disks_to_monitor. Handles both SATA and NVMe.

    Use when:
      - investigating poor I/O or unexpected reboots
      - the user asks about disk health, drive failures, wear
      - producing a scheduled health report

    Per-disk status is one of: 'healthy', 'warning', 'critical', 'unknown'.
    Alerts include reallocated sectors (SATA), spare/used pct (NVMe),
    temperature, and overall SMART self-assessment.

    Returns: {total, healthy, warning, critical, unknown,
              critical_disks: [...], results: [...]}.
    """
    return _scan_disks()

@tool
def check_internet_speed() -> dict:
    """Run an internet speed test and report download/upload/ping in Mbps.

    Use when the operator asks "how fast is my internet?", "is my connection
    slow?", or to include in a health report. Returns:
      {status: ok|slow|error, download_mbps, upload_mbps, ping_ms, server,
       threshold_mbps}
    status is "slow" when download is below SPEEDTEST_MIN_DOWNLOAD_MBPS
    (default 20). Read-only, but it takes ~30s and uses real bandwidth — call
    it only when asked, not as part of a routine "is everything OK?" sweep.
    """
    return _run_speedtest()


@tool
def check_backups() -> dict:
    """Verify backup freshness for every catalogued service.

    For each service, looks up the most recent backup in
    catalog.proxmox_host.backup_storage and compares the age to that
    service's max_backup_age_h. Status per service:

        fresh    — last backup within max_age_h
        stale    — last backup older than max_age_h
        missing  — no backup found for this vmid
        skipped  — max_age_h is 0 (operator opted out)

    Use when:
      - the user asks "are my backups OK?"
      - investigating data-loss / recovery readiness
      - producing a daily / weekly digest

    Returns: {total, fresh, stale, missing, skipped,
              critical_problems: [...], problems: [...] (critical+high),
              results: [...]}. An `error` key means the backup storage
              could not be read — freshness is UNKNOWN, not "missing".
    """
    return _verify_backups()


@tool
def discover_energy_entities() -> dict:
    """List every HA sensor that looks energy- or power-related.

    Returns two buckets: 'energy' (kWh-accumulating — usable for totals)
    and 'power' (instantaneous W — informational). Use this when the
    operator asks "what energy sensors do I have?" or before they've
    configured catalog.yaml -> energy.
    """
    return _discover_energy_entities()


@tool
def check_energy(hours: int = 24) -> dict:
    """Summarize energy usage over the last `hours`.

    Uses catalog.yaml -> energy. Reads main meter delta + per-device
    deltas via HA's history API, applies tariff (if configured), and
    computes coverage (% of main meter explained by watched devices).

    Returns:
      - {"error": "..."} if catalog has no energy section yet
      - {"unconfigured": True, ...} if section exists but has no targets
      - full summary {period_hours, main_kwh, devices[], cost, coverage}

    Use when the operator asks about energy use, cost, top consumers, or
    for a daily / weekly digest.
    """
    return _read_energy_summary(hours=hours)


@tool
def discover_home_entities() -> dict:
    """List every HA entity relevant to presence + HVAC + lights.

    Buckets: persons, device_trackers, motion, door_window, occupancy,
    climate, light, switch, lock. Use this to learn what's available
    BEFORE proposing actions.
    """
    return _discover_home_entities()


@tool
def check_presence_state() -> dict:
    """Who is home vs away, derived from HA person.* entities.

    Returns {presence: home|away|mixed|unknown, home[], away[], details[]}.
    Call this BEFORE proposing climate/light changes — actions should
    respect whether anyone is home.
    """
    return _check_presence_state()


@tool
def check_climate_state() -> dict:
    """Snapshot of every climate.* entity.

    Per entity: mode (off/heat/cool/auto), current_temp, target_temp,
    hvac_action (idle/heating/cooling), preset_mode, fan_mode.
    Use to know what HVAC is doing before suggesting a change.
    """
    return _check_climate_state()


@tool
def check_light_state(include_off: bool = True) -> dict:
    """Snapshot of every light.* entity (on/off + brightness).

    With include_off=False, only currently-on lights are returned —
    useful for "what's still on?" queries when nobody's home.
    """
    return _check_light_state(include_off=include_off)


@tool
def get_ha_entity(entity_id: str) -> dict:
    """Get current state of a Home Assistant entity."""
    return _get_ha_entity(entity_id)


@tool
def list_ha_entities(domain: str = "", limit: int = 30) -> dict:
    """List HA entities, optionally filtered by domain (sensor, light, ...)."""
    return _list_ha_entities(domain or None, limit)


@tool
def list_esphome_sensors(device: str = "", environmental_only: bool = True) -> dict:
    """List ESPHome devices and their sensors with CURRENT readings.

    Use this for "what's the temperature/humidity/CO2 in <room>?" or
    "show all my sensor nodes". Groups every ESPHome entity under its
    physical device (e.g. "Office Monitor" -> temperature, humidity, CO2).

    Args:
        device: optional substring to one device (e.g. "office", "bedroom").
        environmental_only: default True -> only ambient sensors
            (temperature, humidity, air quality). Set False ONLY when the
            user explicitly wants diagnostics too (wifi signal, uptime, ip);
            the full list is ~5x larger and much slower to summarize.
    """
    return _list_integration_sensors("esphome", device or None, environmental_only)


@tool
def check_esphome_devices() -> dict:
    """Online/offline status of every ESPHome device (the ESP32 boards) —
    the same ONLINE/OFFLINE the ESPHome dashboard shows, read from Home
    Assistant.

    Use for "are my ESP devices online?", "is the hall clock connected?",
    "which sensor nodes are down?". For their READINGS (temperature,
    humidity, CO2...) use list_esphome_sensors instead.

    Per device: status — online | offline (unavailable in HA longer than
    grace_s) | dropping (unavailable for less than that: usually an OTA
    flash or reboot, not a problem yet) | missing (in the catalog, gone from
    HA) | unknown; monitor — true = Sentinel sends a Telegram message when
    it goes offline or comes back, false = parked by the operator, no
    alerts; plus ip, firmware, model and how long it has been unavailable.
    A parked device's status can be meaningless (read its `note`) — call it
    parked rather than asserting it is online. `untracked` lists nodes HA
    has that catalog.yaml doesn't (drift for the operator to add).
    Read-only; there is no restart tool for ESP devices.
    """
    try:
        return _scan_esphome()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@tool
def check_all_guests(deep: bool = True) -> dict:
    """Health of EVERY Proxmox guest in one call: state, memory, CPU, uptime.

    Use this FIRST for "how are my VMs?", "is anything using too much
    memory?", "is everything running?" — one call replaces a
    check_proxmox_status per guest.

    Each guest gets a verdict:
      ok              running, memory fine
      stopped         not running (a finding when criticality is critical/high)
      high_mem        memory >= threshold from a TRUSTWORTHY source
      mem_unreliable  reading exists but comes from host_balloon or the guest
                      agent failed — a MONITORING gap, NOT a memory problem.
                      Never recommend a restart for this; recommend
                      installing qemu-guest-agent instead.
      missing         catalogued but no longer on Proxmox (catalog drift)
      unknown         the check itself failed

    Also returns untracked_guests: guests live on Proxmox that are absent
    from catalog.yaml (the other direction of drift).

    Args:
        deep: True (default) reads real memory from inside each running
            guest. False skips SSH — faster, but QEMU memory is then
            unreliable for every VM.
    """
    return _scan_guests(deep=deep)


@tool
def restart_lxc(node: str, vmid: int) -> dict:
    """Restart an LXC *container* (kind='lxc'). DESTRUCTIVE — gated by
    operator approval. For a QEMU VM use restart_vm instead; this refuses a
    vmid the catalog says is qemu rather than posting to the wrong endpoint."""
    return _restart_lxc_raw(node, vmid)


@tool
def restart_vm(node: str, vmid: int) -> dict:
    """Restart a QEMU *virtual machine* (kind='qemu') — graceful ACPI reboot.
    DESTRUCTIVE — gated by operator approval.

    Most of this homelab is QEMU (OPNSense, HomeAssistant, Debian13). Check
    the catalog's restart_policy first: 'never' means propose an alert, not a
    restart. Confirm the guest's state with check_all_guests or
    check_proxmox_status before proposing, and never justify a restart with a
    mem_pct whose source is 'host_balloon'."""
    return _restart_vm_raw(node, vmid)


@tool
def call_ha_service(domain: str, service: str, entity_id: str = "",
                    service_data: dict = None) -> dict:
    """Call a HA service (e.g. light.turn_on, climate.set_temperature).

    DESTRUCTIVE — gated by operator approval."""
    return _call_ha_service_raw(domain, service, entity_id or None, service_data)


@tool
def send_telegram_alert(message: str) -> dict:
    """Send a Telegram message to the operator. Not gated (non-destructive)."""
    return _send_telegram_alert(message)


@tool
def search_docs(query: str, k: int = 4) -> dict:
    """Search the operator's homelab documentation (notes / runbooks / wiki
    kept in docs/) using local BM25 retrieval — no cloud tokens spent here.

    Use this for procedural, config, policy, or "how do I …" questions about
    THIS homelab that the live-state tools can't answer — e.g. "how do I
    restart the bot", "what's the backup retention", "which storage holds
    backups", network/VLAN layout, conventions. Returns the top-k matching
    doc chunks, each with its source file + heading + score; synthesize your
    answer from them and cite the source file. If count == 0, the docs aren't
    indexed yet — tell the operator to add markdown to docs/ and run
    `uv run python rag.py --ingest`.
    """
    return _rag_search(query, k=k)


@tool
def list_docker_containers(all_containers: bool = True) -> dict:
    """List Docker containers on the Debian13 host (via Portainer) with per-
    container state + health, plus a rolled-up digest.

    Use for "are my containers up?", "what Docker services are running?", or
    before deciding whether to restart one. Returns:
      {total, running, stopped, unhealthy,
       stopped_containers: [...], unhealthy_containers: [...],
       containers: [{name, image, state, status, health, id}]}
    state is running/exited/paused/created/restarting/dead; health is
    healthy/unhealthy/starting/"" (from the container's healthcheck).
    all_containers=False returns only running ones.
    """
    return _list_containers(all_containers=all_containers)


@tool
def check_docker_container(name: str) -> dict:
    """Status of ONE Docker container by name (exact, case-insensitive; falls
    back to a unique substring match). Use to confirm a specific container's
    state/health before recommending a restart. Returns its
    {name, state, status, health, id} or an error (incl. ambiguous matches)."""
    return _get_container(name)


@tool
def restart_docker_container(name: str) -> dict:
    """Restart a Docker container on the Debian13 host (via Portainer).
    DESTRUCTIVE — gated by operator approval. Check the container's state with
    check_docker_container / list_docker_containers FIRST, and prefer
    restarting only a container that is unhealthy, exited, or that the user
    explicitly named. Note AdGuard runs here and is the LAN's DNS resolver —
    restarting it briefly interrupts DNS for the whole network."""
    return _restart_container_raw(name)


@tool
def speak_on_alexa(text: str) -> dict:
    """Speak a short message aloud on the operator's Echo Dot (Alexa Media
    Player TTS). Not gated (non-destructive). Use only when the operator
    asks you to say something on the speaker / out loud / on Alexa. Keep it
    to a sentence or two — it's read aloud."""
    return _speak_on_alexa(text)


@tool
def check_commute(force: bool = True) -> dict:
    """Check the operator's morning commute (S2 S-Bahn toward Munich and bus
    700 toward the city, both from YourVillage) for current delays or
    cancellations. Read-only; makes NO Alexa announcement. force=True checks
    even outside the weekday 06:00-09:00 commute window."""
    from db_train_monitor import check_commute as _check_commute
    return {"alerts": _check_commute(force=force)}


@tool
def next_departures(limit: int = 4) -> dict:
    """Next departures of the operator's commute services (S2 S-Bahn toward
    the city and bus 700 toward the city, from YourVillage) with
    realtime delays. Read-only. The 'summary' field is a spoken-style
    one-liner; 'departures' has the structured list."""
    from db_train_monitor import next_departures as _next_departures
    return _next_departures(limit=limit)


_TOOLS = [
    get_service_catalog, get_service,
    list_proxmox_nodes, list_proxmox_guests,
    check_proxmox_status, get_guest_mem_pct, check_all_guests,
    check_reachability, check_disk_health, check_backups,
    discover_energy_entities, check_energy,
    discover_home_entities, check_presence_state,
    check_climate_state, check_light_state,
    get_ha_entity, list_ha_entities, list_esphome_sensors,
    check_esphome_devices,
    search_docs, speak_on_alexa,
    check_commute, next_departures,
    list_docker_containers, check_docker_container,
    check_internet_speed,
    restart_lxc, restart_vm, restart_docker_container, call_ha_service,
    send_telegram_alert,
]
_TOOLS_BY_NAME = {t.name: t for t in _TOOLS}

# Public aliases — the agent and the MCP server import these.
TOOLS = _TOOLS
TOOLS_BY_NAME = _TOOLS_BY_NAME
