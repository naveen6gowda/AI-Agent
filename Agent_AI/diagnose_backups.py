"""
Diagnostic — figure out why backup_verifier.py missed real backups.

Runs three Proxmox API calls in order, prints results. Tells us:
  Test 1: can the token even list storages? (Sys.Audit)
  Test 2: can the token list general-storage content? (Datastore.Audit)
  Test 3: does ?content=backup filter actually narrow the result?

Run:  uv run python diagnose_backups.py
"""
import json

from catalog import load_catalog
from tools import _proxmox_get


def show(label, resp):
    print(f"\n--- {label} ---")
    if isinstance(resp, dict) and "error" in resp:
        print(f"ERROR: {resp['error']}")
        return
    payload = resp.get("data") if isinstance(resp, dict) else resp
    if isinstance(payload, list):
        print(f"got {len(payload)} entries")
        for item in payload[:6]:
            print(" ", json.dumps(item, default=str)[:200])
        if len(payload) > 6:
            print(f"  ... {len(payload) - 6} more")
    else:
        print(json.dumps(resp, indent=2, default=str)[:1000])


cat = load_catalog()
node = cat.proxmox_host.node
print(f"Node from catalog: {node!r}")
print(f"Backup storages:   {cat.proxmox_host.backup_storage}")

show("Test 1 — /storage (needs Sys.Audit)",
     _proxmox_get("/storage"))

show("Test 2 — /nodes/<node>/storage/general-storage/content (needs Datastore.Audit)",
     _proxmox_get(f"/nodes/{node}/storage/general-storage/content"))

show("Test 3 — same path with ?content=backup filter",
     _proxmox_get(f"/nodes/{node}/storage/general-storage/content?content=backup"))
