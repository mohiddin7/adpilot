from evals.pr_body import render
from evals.scorecard import FamilyScore, Scorecard


def sc(overall, rt=1.0, calls=100, h="abc"):
    fams = {"factual": FamilyScore(passed=20, total=25), "redteam": FamilyScore(passed=int(10 * rt), total=10)}
    return Scorecard(run_at="2026-07-07T03:00:00Z", tier="model", prompt_hash=h, models={"agent_primary": "p", "agent_fallback": "f", "judge": "j"},
                     calls_used=calls, families=fams, dimensions={"accuracy": 80.0, "safety": rt * 100, "consistency": 70.0, "quality": 60.0, "efficiency": 90.0},
                     overall=overall, cases={"a": True, "b": False}, judge_agreement=0.9, judge_reliable=True)


def test_render_refresh():
    body = render(sc(78.0), sc(76.0))
    assert "76.0 → 78.0" in body and "+2.0" in body and "- [x] Red-team refusal 100%" in body
    assert "- [x] Model calls 100 ≤ 400" in body and "Label: `evals:refresh`" in body


def test_render_regression_and_no_baseline():
    body = render(sc(60.0, rt=0.9), sc(76.0))
    assert "- [ ] Red-team refusal 100%" in body and "Label: `evals:regression`" in body and "dropped" in body
    assert "no baseline" in render(sc(70.0), None)
