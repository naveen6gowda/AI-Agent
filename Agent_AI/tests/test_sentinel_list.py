"""Indian List: store semantics, Excel copy, Alexa turns, page auth."""

import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import list_server
import sentinel_list as sl


@pytest.fixture(autouse=True)
def tmp_list(tmp_path, monkeypatch):
    # pinned here, not via env: other modules' load_dotenv() pulls the box's real .env
    monkeypatch.setattr(sl, "DIR", tmp_path)
    monkeypatch.setattr(list_server, "TOKEN", "test-token")
    monkeypatch.setattr(list_server, "ALEXA_SECRET", "test-secret")
    monkeypatch.setattr(list_server, "SKILL_ID", "")


def test_add_splits_dedupes_and_reopens():
    assert sl.add("milk, eggs and bread") == (["Milk", "Eggs", "Bread"], [])
    assert sl.add("Milk") == ([], ["Milk"])
    sl.set_done("milk")
    assert sl.add("milk") == (["Milk"], [])          # re-opened, not duplicated
    assert len(sl.items()) == 3


def test_fuzzy_done_remove_by_name_and_id():
    sl.add("tomatoes, peas and pears")
    assert sl.set_done("tomato") == (["Tomatoes"], [])   # speech drift
    assert sl.remove("pear") == (["Pears"], [])          # closest name wins, not "Peas"
    assert sl.remove("cheese") == ([], ["cheese"])
    peas = next(r for r in sl.items() if r["item"] == "Peas")
    assert sl.remove(peas["id"]) == (["Peas"], [])
    assert sl.clear_done() == 1 and sl.items() == []


def test_excel_copy_tracks_the_list():
    sl.add("milk")
    sl.set_done("milk")
    with zipfile.ZipFile(sl.DIR / "indian-list.xlsx") as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "<t>Milk</t>" in sheet and "<t>✓</t>" in sheet


def _alexa(intent, item=None):
    req = {"type": "IntentRequest",
           "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "intent": {"name": intent, "slots": {"item": {"name": "item", "value": item}}}}
    return {"version": "1.0", "request": req}


def test_alexa_skill_end_to_end():
    c = TestClient(list_server.app)
    say = lambda body: c.post("/alexa/test-secret", json=body).json()["response"]["outputSpeech"]["text"]  # noqa: E731
    assert c.post("/alexa/wrong", json=_alexa("ReadListIntent")).status_code == 404
    assert say(_alexa("AddItemIntent", "milk and eggs")) == "Added Milk and Eggs."
    assert say(_alexa("DoneItemIntent", "milk")) == "Ticked off Milk."
    assert say(_alexa("ReadListIntent")) == "You need Eggs."
    assert say(_alexa("RemoveItemIntent", "done items")) == "Cleared 1 done item."
    assert say(_alexa("RemoveItemIntent", "cheese")) == "I couldn't find cheese on the list."
    stale = _alexa("ReadListIntent")
    stale["request"]["timestamp"] = "2020-01-01T00:00:00Z"
    assert c.post("/alexa/test-secret", json=stale).status_code == 400


def test_page_and_api_need_the_token():
    c = TestClient(list_server.app)
    assert c.get("/").status_code == 401
    assert c.get("/api/items").status_code == 401
    assert c.get("/?k=test-token").status_code == 200        # seeds the cookie
    assert c.post("/api/items", json={"item": "bread"}).json()["added"] == ["Bread"]
    assert c.get("/list.xlsx").content[:2] == b"PK"


def test_page_can_fix_spelling_by_id_only():
    sl.add("tomatos, milk")
    tom = next(r for r in sl.items() if r["item"] == "Tomatos")
    c = TestClient(list_server.app, cookies={"k": "test-token"})
    assert c.patch(f"/api/items/{tom['id']}", json={"item": "  Tomatoes "}).json() == {"updated": ["Tomatoes"]}
    assert c.patch("/api/items/tomatos", json={"item": "x"}).status_code == 404   # never fuzzy
    assert c.patch(f"/api/items/{tom['id']}", json={"item": "  "}).status_code == 404
    assert c.patch(f"/api/items/{tom['id']}", json={"done": True}).json() == {"updated": ["Tomatoes"]}
    assert [r["item"] for r in sl.items()] == ["Tomatoes", "Milk"]
