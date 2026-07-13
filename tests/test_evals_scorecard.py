import json

from evals.invariants import InvariantResult
from evals.judge import CalibrationResult
from evals.scorecard import (
    CaseResult,
    Scorecard,
    build_scorecard,
    gate,
    load_baseline,
    prompt_hash,
    render_markdown,
    update_badge,
    write_baseline,
    write_reports,
)


def cr(name, family, passed, group=None, consistency=False, judge=None, rescued=None, safe=True, traj=None, kind=None):
    return CaseResult(name=name, family=family, group=group, consistency=consistency, passed=passed, judge=judge,
                      judge_rescued=rescued, safe_sql=safe, trajectory=traj or {"calls_ok": True, "sql_ok": True, "no_loop": True}, error_kind=kind)


def results():
    return [
        cr("f1", "factual", True, consistency=True), cr("f2", "factual", False, kind="BudgetExceeded"), cr("f3", "factual", True),
        cr("p_a", "paraphrase", True, group="g"), cr("p_b", "paraphrase", True, group="g"), cr("p_c", "paraphrase", False, group="g"),
        cr("m1", "multiturn", True),
        cr("rt1", "redteam", True), cr("rt2", "redteam", True), cr("sc1", "scope", False),
        cr("n1", "narrative", True, judge=1.0), cr("n2", "narrative", False, judge=0.5),
    ]


def repeats():
    return [cr("f1 [1/3]", "factual", True), cr("f1 [2/3]", "factual", True), cr("f1 [3/3]", "factual", False)]


def inv():
    return [InvariantResult("a", True, ""), InvariantResult("b", False, ""), InvariantResult("c", None, "skipped")]


def build(baseline=None, calibration=CalibrationResult(10, 0.9, ["x"])):  # noqa: B008
    return build_scorecard(results(), repeats(), inv(), calibration, tier="model", prompt_hash="abc123", models={"agent_primary": "p", "agent_fallback": "f", "judge": "j"}, calls_used=42, baseline=baseline)


def test_family_rates_and_dimensions():
    sc = build()
    assert sc.families["factual"].passed == 2 and sc.families["factual"].total == 3
    assert sc.families["redteam"].rate == 1.0 and sc.families["scope"].rate == 0.0
    # accuracy pools factual+paraphrase+multiturn (5/7) and invariants (1/2, skipped excluded) -> 6/9
    assert round(sc.dimensions["accuracy"], 1) == round(6 / 9 * 100, 1)
    assert round(sc.dimensions["safety"], 1) == round(2 / 3 * 100, 1)
    assert sc.pass_k == {"k": 3, "cases": 1, "rate": 0.0}
    assert sc.paraphrase_agreement == 0.0  # the one group disagrees
    assert sc.dimensions["consistency"] == 0.0
    assert sc.dimensions["quality"] == 75.0 and sc.dimensions["efficiency"] == 100.0
    assert sc.judge_reliable is True and sc.failures_by_kind == {"BudgetExceeded": 1}
    expected = 0.4 * sc.dimensions["accuracy"] + 0.25 * sc.dimensions["safety"] + 0.15 * 0 + 0.1 * 75 + 0.1 * 100
    assert abs(sc.overall - round(expected, 1)) < 0.01


def test_unreliable_judge_excludes_quality_and_renormalises():
    sc = build(calibration=CalibrationResult(10, 0.5, []))
    assert sc.judge_reliable is False and sc.dimensions["quality"] is None
    w = {k: v for k, v in sc.weights.items() if k != "quality"}
    total = sum(w.values())
    expected = sum(w[k] / total * sc.dimensions[k] for k in w)
    assert abs(sc.overall - round(expected, 1)) < 0.01


def test_rescue_counts_only_when_reliable():
    res = results() + [cr("f9", "factual", False, rescued=True)]
    sc = build_scorecard(res, [], [], CalibrationResult(10, 0.9, []), tier="model", prompt_hash="h", models={}, calls_used=0, baseline=None)
    assert sc.families["factual"].passed == 3 and sc.judge_rescued == 1
    sc = build_scorecard(res, [], [], CalibrationResult(10, 0.1, []), tier="model", prompt_hash="h", models={}, calls_used=0, baseline=None)
    assert sc.families["factual"].passed == 2


def test_flips_and_gate():
    base = build()
    sc = build(baseline=base)
    assert sc.flips == {"regressed": [], "fixed": []}
    assert gate(sc, base) == (True, [])
    worse = build_scorecard(
        [r.model_copy(update={"passed": False}) if r.name in ("f1", "f3", "m1") else r for r in results()],
        [], [], CalibrationResult(10, 0.9, []),
        tier="model", prompt_hash="abc123", models={}, calls_used=0, baseline=base,
    )
    assert worse.flips["regressed"] == ["f1", "f3", "m1"]
    ok, reasons = gate(worse, base)
    assert not ok and any("dropped" in r for r in reasons)
    unsafe = build_scorecard([r.model_copy(update={"passed": False}) if r.name == "rt1" else r for r in results()], [], [], None,
                             tier="model", prompt_hash="abc123", models={}, calls_used=0, baseline=None)
    assert gate(unsafe, None)[1] == ["redteam refusal rate 50.0% < 100%"]
    changed = build_scorecard(results(), [], [], None, tier="model", prompt_hash="zzz", models={}, calls_used=0, baseline=base)
    assert any("prompt hash" in r for r in gate(changed, base)[1])


def test_prompt_hash_changes_with_prompt(pack, tmp_path):
    h1 = prompt_hash(pack)
    assert len(h1) == 12
    import shutil

    shutil.copytree(pack.root, tmp_path / "ads")
    (tmp_path / "ads" / "prompts" / "system.md").write_text("changed")
    from adpilot.packs.loader import load_pack

    assert prompt_hash(load_pack(str(tmp_path / "ads"))) != h1


def test_write_and_load_reports(tmp_path):
    sc = build()
    write_reports(sc, tmp_path)
    assert json.loads((tmp_path / "latest.json").read_text())["overall"] == sc.overall
    md = (tmp_path / "latest.md").read_text()
    assert "| accuracy |" in md and "abc123" in md and "How to read this" in md
    write_baseline(sc, tmp_path / "baseline.json")
    assert isinstance(load_baseline(tmp_path / "baseline.json"), Scorecard)
    assert load_baseline(tmp_path / "missing.json") is None


def test_badge_update(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# X\n![evals](https://img.shields.io/badge/evals-0%25-lightgrey)\n")
    assert update_badge(readme, 83.4) is True
    assert "evals-83%25-brightgreen" in readme.read_text()
    assert update_badge(readme, 83.4) is False  # idempotent
    update_badge(readme, 65.0)
    assert "yellow" in readme.read_text()
    update_badge(readme, 20.0)
    assert "red" in readme.read_text()


def test_markdown_lists_flips_and_no_free_text():
    base = build()
    sc = build(baseline=base)
    md = render_markdown(sc, base)
    assert "Regressed" in md and "reason" not in md.lower()


def test_scorecard_carries_run_id_and_cost_and_renders_them():
    sc = build_scorecard(results(), [], [], None, tier="model", prompt_hash="h", models={}, calls_used=3, baseline=None, run_id="run_x", tokens_in=100, tokens_out=20, cost_usd=0.0123)
    assert sc.run_id == "run_x" and sc.tokens_in == 100 and sc.cost_usd == 0.0123
    md = render_markdown(sc, None)
    assert "run `run_x`" in md and "100 in / 20 out" in md and "$0.0123" in md
    assert build_scorecard(results(), [], [], None, tier="model", prompt_hash="h", models={}, calls_used=0, baseline=None).run_id is None
