"""
sentinel_list.py — the "Indian List" checklist that Alexa fills.

    "Alexa, tell Indian List to add milk"
        → Alexa custom skill → list_server.py → this module
    iPhone / laptop page  → list_server.py → this module
    Telegram / MCP agent  → registry.manage_list → this module

var/list/items.json is the source of truth. Every save also rewrites
var/list/indian-list.xlsx (stdlib writer, no openpyxl dependency) so the
Excel copy on this box is always current. Writes are atomic (tmp + rename)
and serialized with flock, because the list server and the bot are
separate processes. Every change lands in audit.log.
"""

from __future__ import annotations

import difflib
import fcntl
import io
import json
import os
import re
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from tools import _audit

DIR = Path(os.getenv("LIST_DIR", Path(__file__).parent / "var" / "list"))


def items() -> list[dict]:
    try:
        return json.loads((DIR / "items.json").read_text(encoding="utf-8"))["items"]
    except FileNotFoundError:
        return []


@contextmanager
def _edit(event: str, text: str):
    """Locked read-modify-write: yields the rows, saves them on exit."""
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = items()
        yield rows
        _atomic(DIR / "items.json",
                json.dumps({"items": rows}, indent=1, ensure_ascii=False).encode())
        _atomic(DIR / "indian-list.xlsx", xlsx_bytes(rows))
    _audit("list_" + event, {"text": text[:200], "count": len(rows)})


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _norm(s: str) -> str:
    return " ".join(s.lower().strip(" .!?").split())


def split_items(text: str) -> list[str]:
    """'milk, eggs and bread' → three items. Alexa hands us one string.
    ponytail: 'mac and cheese' also splits in two; fine for a shopping list."""
    return [p for p in (_norm(x) for x in re.split(r",| and ", text or "")) if p]


def _find(rows: list[dict], key: str) -> dict | None:
    """Row by id (web page), else the closest name (speech drift: 'tomato'
    finds 'Tomatoes'). Alexa says the matched name back, so a near miss is heard."""
    names = [_norm(r["item"]) for r in rows]
    for r in rows:
        if r["id"] == key:
            return r
    hit = difflib.get_close_matches(_norm(key), names, n=1, cutoff=0.8)
    return rows[names.index(hit[0])] if hit else None


def add(text: str) -> tuple[list[str], list[str]]:
    """Add item(s). Returns (added, already_on_list). A ticked-off item
    with the same name is re-opened instead of duplicated."""
    added, already = [], []
    with _edit("add", text) as rows:
        for name in split_items(text):
            row = next((r for r in rows if _norm(r["item"]) == name), None)
            if row and not row["done"]:
                already.append(row["item"])
                continue
            if row:
                row["done"] = False
            else:
                row = {"id": uuid.uuid4().hex[:8], "item": name[0].upper() + name[1:],
                       "done": False, "added": datetime.now().isoformat(timespec="seconds")}
                rows.append(row)
            added.append(row["item"])
    return added, already


def set_done(text: str, done: bool = True) -> tuple[list[str], list[str]]:
    """Tick (or untick) item(s) by id or name. Returns (updated, not_found)."""
    hits, misses = [], []
    with _edit("done" if done else "undone", text) as rows:
        for key in split_items(text) or [text]:
            row = _find(rows, key)
            if row:
                row["done"] = done
                hits.append(row["item"])
            else:
                misses.append(key)
    return hits, misses


def rename(item_id: str, text: str) -> str | None:
    """Fix an item's spelling from the page. Exact id only, never fuzzy:
    a wrong guess here would silently rewrite a different row."""
    text = " ".join(text.split())
    if not text:
        return None
    with _edit("rename", text) as rows:
        row = next((r for r in rows if r["id"] == item_id), None)
        if row:
            row["item"] = text
    return row["item"] if row else None


def remove(text: str) -> tuple[list[str], list[str]]:
    """Delete item(s) by id or name. Returns (removed, not_found)."""
    hits, misses = [], []
    with _edit("remove", text) as rows:
        for key in split_items(text) or [text]:
            row = _find(rows, key)
            if row:
                rows.remove(row)
                hits.append(row["item"])
            else:
                misses.append(key)
    return hits, misses


def clear_done() -> int:
    """Drop every ticked-off row. Returns how many went."""
    with _edit("clear_done", "") as rows:
        keep = [r for r in rows if not r["done"]]
        n = len(rows) - len(keep)
        rows[:] = keep
    return n


# ---------------------------------------------------------------------
# Excel copy — the minimum valid .xlsx (5 XML parts, inline strings)
# ---------------------------------------------------------------------
_NS = "http://schemas.openxmlformats.org"
_REL = f"{_NS}/officeDocument/2006/relationships"
_PARTS = {
    "[Content_Types].xml":
        f'<Types xmlns="{_NS}/package/2006/content-types">'
        f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels":
        f'<Relationships xmlns="{_NS}/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml":
        f'<workbook xmlns="{_NS}/spreadsheetml/2006/main" xmlns:r="{_REL}">'
        f'<sheets><sheet name="Indian List" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels":
        f'<Relationships xmlns="{_NS}/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
}


def xlsx_bytes(rows: list[dict]) -> bytes:
    def when(iso: str) -> str:   # 12-hour clock, his standing preference
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %-I:%M %p")

    table = [("Item", "Done", "Added")] + [
        (r["item"], "✓" if r["done"] else "", when(r["added"])) for r in rows]
    body = "".join(
        f'<row r="{i}">' + "".join(
            f'<c r="{col}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for col, v in zip("ABC", row, strict=True)) + "</row>"
        for i, row in enumerate(table, 1))
    sheet = (f'<worksheet xmlns="{_NS}/spreadsheetml/2006/main"><cols>'
             '<col min="1" max="1" width="34" customWidth="1"/>'
             '<col min="2" max="2" width="8" customWidth="1"/>'
             '<col min="3" max="3" width="20" customWidth="1"/></cols>'
             f'<sheetData>{body}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in {**_PARTS, "xl/worksheets/sheet1.xml": sheet}.items():
            z.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + xml)
    return buf.getvalue()
