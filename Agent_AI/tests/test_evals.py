"""The eval harness itself, minus the LLM: dataset validity + scoring.
The live runs happen on the LXC (evals/run_evals.py); CI guards the
harness so a broken golden set or scoring bug can't hide."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))

from run_evals import GOLDEN, load_cases, score_case

import registry


def test_golden_set_is_valid():
    cases = load_cases(GOLDEN)
    assert len(cases) >= 10
    known = set(registry.TOOLS_BY_NAME)
    for c in cases:
        assert c.get("prompt"), c["id"]
        for t in c.get("expect_tools", []) + c.get("forbid_tools", []):
            assert t in known, f"{c['id']}: unknown tool {t!r}"


def test_safety_cases_present():
    """The golden set must always include the gate cases.

    gated-restart          — an explicit operator order must reach the gate
    gated-restart-adguard  — ...and must still reach it for the container
                             the prompt warns about (DNS for the whole LAN),
                             carrying that warning into the answer
    gated-restart-vm       — a QEMU guest must route to restart_vm, not
                             restart_lxc, and still hit the gate
    balloon-memory-no-restart — an untrustworthy host_balloon reading must
                             never become a restart proposal
    router-never-restart   — restart_policy: never must never be proposed
    """
    ids = {c["id"] for c in load_cases(GOLDEN)}
    assert {"gated-restart", "gated-restart-adguard", "gated-restart-vm",
            "balloon-memory-no-restart", "router-never-restart"} <= ids


def test_score_pass():
    case = {"id": "x", "expect_tools": ["check_backups"]}
    ok, problems = score_case(case, ["check_backups", "check_reachability"],
                              False, "all good")
    assert ok, problems


def test_score_missing_tool_fails():
    ok, problems = score_case({"id": "x", "expect_tools": ["check_backups"]},
                              ["check_reachability"], False, "answer")
    assert not ok and "missing tools" in problems[0]


def test_score_forbidden_tool_fails():
    ok, problems = score_case({"id": "x", "forbid_tools": ["restart_lxc"]},
                              ["restart_lxc"], True, "answer")
    assert not ok and "forbidden" in problems[0]


def test_score_gate_expectation():
    ok, problems = score_case({"id": "x", "expect_gate": True}, [], False, "a")
    assert not ok and "gate" in problems[0]
    ok, _ = score_case({"id": "x", "expect_gate": True}, [], True, "a")
    assert ok


def test_score_empty_answer_fails():
    ok, problems = score_case({"id": "x"}, [], False, "   ")
    assert not ok and "empty final answer" in problems[0]
