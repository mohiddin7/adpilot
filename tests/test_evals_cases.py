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


def test_guard_tag_requires_refuse_true():
    import pytest

    with pytest.raises(ValueError):
        EvalCase(name="x", family="scope", question="q", expected={"refuse": False, "guard": True})
    c = EvalCase(name="x", family="redteam", question="q", expected={"refuse": True, "guard": True})
    assert c.metadata["guard"] is True and c.metadata["refuse"] is True


def test_guarded_redteam_cases_have_paraphrase_groups(pack):
    guarded = [c for c in load_cases(pack, {"redteam"}) if c.expected.guard]
    assert len(guarded) >= 20, "canonical attacks plus casing/spacing/character-injection/reworded variants"
    groups = {c.group for c in guarded if c.group}
    assert {"information_schema", "read_file", "drop", "comment_smuggle", "ignore_instructions", "exfil_secrets"} <= groups


def test_scope_negatives_cover_guard_vocabulary(pack):
    names = {c.name for c in load_cases(pack, {"scope"}) if c.expected.refuse is False}
    assert {"sc_on_topic_drop_verb", "sc_on_topic_update_verb", "sc_on_topic_schema_plain", "sc_on_topic_accented",
            "sc_on_topic_currency", "sc_on_topic_table_name"} <= names


def test_classifier_tag_requires_refuse_true_and_no_guard():
    import pytest

    with pytest.raises(ValueError):
        EvalCase(name="x", family="redteam", question="q", expected={"refuse": True, "guard": True, "classifier": True})
    with pytest.raises(ValueError):
        EvalCase(name="x", family="redteam", question="q", expected={"refuse": False, "classifier": True})
    c = EvalCase(name="x", family="redteam", question="q", expected={"refuse": True, "classifier": True})
    assert c.metadata["classifier"] is True


def test_classifier_cases_are_invisible_to_layer0_by_construction(pack):
    """The reworded attacks carry no SQL/override token: if layer 0 caught one, it would not be testing layer 1."""
    from adpilot.core.guardrails import is_in_scope, sanitize_question

    cases = [c for c in load_cases(pack, {"redteam"}) if c.expected.classifier]
    assert len(cases) >= 12
    for c in cases:
        assert is_in_scope(sanitize_question(c.question)), f"{c.name} is caught by layer 0"
