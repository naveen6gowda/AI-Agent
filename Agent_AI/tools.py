import httpx
import os
from dotenv import load_dotenv
load_dotenv()

def check_proxmox_status(node: str, vmid: int) -> dict:
    """Check status of an LXC/VM on Proxmox."""
    # Stub for now — wire to real API later
    fake_data = {
        100: {"name": "openclaw", "status": "running", "mem_pct": 87, "uptime_h": 142},
        101: {"name": "homeassistant", "status": "running", "mem_pct": 45, "uptime_h": 200},
        102: {"name": "mqtt-broker", "status": "stopped", "mem_pct": 0, "uptime_h": 0},
    }
    return fake_data.get(vmid, {"error": "not found"})

def get_ha_entity(entity_id: str) -> dict:
    """Get state of a Home Assistant entity."""
    fake_states = {
        "binary_sensor.mailbox_lid": {"state": "off", "last_changed": "2026-05-15T08:12:00"},
        "sensor.mailbox_battery": {"state": "3.6", "unit": "V"},
    }
    return fake_states.get(entity_id, {"error": "unknown entity"})

def restart_lxc(node: str, vmid: int) -> dict:
    """Restart an LXC container. DESTRUCTIVE — requires confirmation upstream."""
    return {"action": "restart_lxc", "vmid": vmid, "result": "restart_initiated"}

def send_telegram_alert(message: str) -> dict:
    """Send an alert via Telegram bot."""
    print(f"[TELEGRAM] {message}")
    return {"sent": True, "message": message}