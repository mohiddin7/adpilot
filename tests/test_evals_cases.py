from evals.cases import EvalCase, check_cases, load_cases, reference_rows, to_dataset


def test_loads_all_families(pack):
    cases = load_cases(pack)
    fams = {c.family for c in cases}
    assert fams == {"factual", "paraphrase", "multiturn", "redteam", "scope", "narrative"}
    assert len({c.name for c in cases}) == len(cases)  # unique names


def test_family_filter_and_limit(pack):
    assert all(c.family == "redteam" for c in load_cases(pack, families={"redteam"}))
    assert len(load_cases(pack, limit=3)) == 3


def test_expected_shape_per_family(pack):
    for c in load_cases(pack):
        if c.family in ("factual", "paraphrase", "multiturn"):
            assert c.expected.sql, c.name
        elif c.family in ("redteam", "scope"):
            assert c.expected.refuse is not None, c.name
        else:
            assert c.expected.rubric, c.name


def test_paraphrase_groups_have_three(pack):
    groups = {}
    for c in load_cases(pack, families={"paraphrase"}):
        groups.setdefault(c.group, []).append(c)
    assert groups and all(len(v) == 3 for v in groups.values())


def test_consistency_sample_size(pack):
    assert len([c for c in load_cases(pack) if c.consistency]) == 10


def test_check_cases_clean(pack, eval_duck):
    assert check_cases(eval_duck, pack, load_cases(pack)) == []


def test_check_cases_detects_stale_value(pack, eval_duck):
    case = next(c for c in load_cases(pack) if c.name == "total_spend")
    stale = case.model_copy(update={"expected": case.expected.model_copy(update={"value": 1.0})})
    assert check_cases(eval_duck, pack, [stale]) == ["total_spend: stored value 1.0 != reference 130244.9"]


def test_reference_rows_and_dataset(pack, eval_duck):
    case = next(c for c in load_cases(pack) if c.name == "spend_by_platform")
    rows = reference_rows(eval_duck, pack, case.expected)
    assert rows[0] == {"platform": "TikTok", "spend": 74266.7}
    ds = to_dataset([case], evaluators=[])
    assert ds.cases[0].name == "spend_by_platform" and ds.cases[0].metadata["family"] == "factual"


def test_eval_case_rejects_missing_question():
    import pytest

    with pytest.raises(ValueError):
        EvalCase(name="x", family="factual", expected={"sql": "SELECT 1"})
