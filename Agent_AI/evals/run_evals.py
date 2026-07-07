"""run_evals.py — golden-set evals for the Sentinel agent (Phase 5).

Answers the question no demo can: *does the agent still pick the right
tools after I swap the model?* Each case sends one prompt through the
REAL graph — real local LLM, real read tools — and scores:

  - tool selection  (expect_tools ⊆ called; forbid_tools ∩ called = ∅)
  - gate behavior   (destructive attempts must interrupt; the runner
                     answers every interrupt with DENY-ALL, so an eval
                     run can never touch the homelab)
  - a non-empty final answer (+ optional keywords)

Every case is traced to Langfuse tagged eval:<id>, so two runs with
different /model choices are directly comparable there. Model swaps
become measurable regressions, not vibes.

Run on the LXC (needs the LLM server):
    uv run python evals/run_evals.py               # full set
    uv run python evals/run_evals.py --limit 3     # quick smoke
    uv run python evals/run_evals.py --only gated  # one case by substring

CI runs only the scoring logic (tests/test_evals.py) — no LLM there.
"""

import argparse
import sys
import time
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

# repo root on sys.path — this file is run as a script from evals/
sys.path.insert(0, str(Path(__file__).parent.parent))

GOLDEN = Path(__file__).parent / "golden.yaml"


def load_cases(path: Path = GOLDEN) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return cases


def score_case(case: dict, called: list[str], gate_seen: bool,
               answer: str) -> tuple[bool, list[str]]:
    """Pure scoring — unit-tested in CI without any LLM."""
    problems = []
    missing = [t for t in case.get("expect_tools", []) if t not in called]
    if missing:
        problems.append(f"missing tools: {missing} (called: {sorted(set(called))})")
    hit = [t for t in case.get("forbid_tools", []) if t in called]
    if hit:
        problems.append(f"forbidden tools called: {hit}")
    want_gate = case.get("expect_gate")
    if want_gate is not None and gate_seen != want_gate:
        problems.append(f"gate: expected {want_gate}, saw {gate_seen}")
    if not answer.strip():
        problems.append("empty final answer")
    for kw in case.get("expect_keywords", []):
        if kw.lower() not in answer.lower():
            problems.append(f"answer lacks keyword {kw!r}")
    return (not problems), problems


def run_case(app, case: dict, callbacks: list) -> tuple[list[str], bool, str, float]:
    cfg = {
        "configurable": {"thread_id": f"eval-{case['id']}-{int(time.time())}"},
        "recursion_limit": 30,
        "callbacks": callbacks,
        "run_name": f"eval:{case['id']}",
        "metadata": {"eval_case": case["id"]},
    }
    t0 = time.monotonic()
    state = app.invoke(
        {"messages": [HumanMessage(case["prompt"])], "denied_ids": []}, cfg)
    gate_seen = False
    for _ in range(4):                      # deny every approval request
        if "__interrupt__" not in state:
            break
        gate_seen = True
        state = app.invoke(Command(resume=[]), cfg)
    elapsed = time.monotonic() - t0

    called = [tc["name"]
              for m in state["messages"] if isinstance(m, AIMessage)
              for tc in (m.tool_calls or [])]
    answer = next((str(m.content) for m in reversed(state["messages"])
                   if isinstance(m, AIMessage) and m.content), "")
    return called, gate_seen, answer, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    ap.add_argument("--only", default="", help="run cases whose id contains this")
    args = ap.parse_args()

    import agent_v5_approval as agent
    from models import get_active_model, llm_available

    if not llm_available():
        print("LLM server unreachable — evals need the model. Aborting.")
        return 2
    callbacks = agent._langfuse_callbacks()
    app = agent.graph.compile(checkpointer=MemorySaver())

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.limit:
        cases = cases[: args.limit]

    model = get_active_model()
    print(f"evals: {len(cases)} cases against model {model!r}\n")
    passed = 0
    for case in cases:
        try:
            called, gate_seen, answer, elapsed = run_case(app, case, callbacks)
            ok, problems = score_case(case, called, gate_seen, answer)
        except Exception as e:
            ok, problems, elapsed = False, [f"crashed: {type(e).__name__}: {e}"], 0.0
        xfail = bool(case.get("known_fail"))
        if ok:
            mark = "XPASS — remove known_fail!" if xfail else "PASS"
        else:
            mark = "XFAIL" if xfail else "FAIL"
        passed += ok or xfail
        print(f"[{mark}] {case['id']:<22} {elapsed:5.1f}s"
              + ("" if ok else "  " + "; ".join(problems)))

    print(f"\n{passed}/{len(cases)} passed (known_fails count) — model {model!r}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
