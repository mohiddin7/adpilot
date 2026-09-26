"""Filter parsing and the WHERE builder: every viewer value is validated, then quoted for its dialect."""

from datetime import date

import pytest

from adpilot.dashboard.config import load_dashboard
from adpilot.dashboard.filters import FilterError, Filters, build_where, parse_filters, sql_literal

D = [("date_from", "2024-01-01"), ("date_to", "2024-01-30")]


@pytest.fixture(scope="module")
def cfg(pack):
    return load_dashboard(pack)


@pytest.mark.parametrize("value", ["it's", "a''b", "back\\slash", "x'); DROP TABLE t; --", "Mother's Day \\' sale"])
def test_duckdb_literals_round_trip_as_data(duck, value):
    assert duck.query(f"SELECT {sql_literal(value, 'duckdb')} AS v")["v"].iloc[0] == value


def test_bigquery_literals_use_backslash_escapes():
    assert sql_literal("it's", "bigquery") == "'it\\'s'"
    assert sql_literal("a\\b", "bigquery") == "'a\\\\b'"
    assert sql_literal("x\\", "bigquery") == "'x\\\\'"  # a trailing backslash cannot swallow the closing quote


def test_an_unknown_dialect_has_no_quoting():
    with pytest.raises(ValueError, match="dialect"):
        sql_literal("x", "postgres")


def test_parse_reads_dates_categoricals_and_ranges(cfg):
    flt = parse_filters(cfg, "deep_dive", [*D, ("platform", "Google"), ("platform", "Facebook"),
                                           ("spend_min", "10"), ("cpa_max", "50.5")])
    assert (flt.date_from, flt.date_to) == (date(2024, 1, 1), date(2024, 1, 30))
    assert flt.values("platform") == ("Facebook", "Google")
    assert set(flt.ranges) == {("spend", 10.0, None), ("cpa", None, 50.5)}


@pytest.mark.parametrize("items, reason", [
    ([("date_to", "2024-01-30")], "date_from is required"),
    ([("date_from", "2024-13-01"), ("date_to", "2024-01-30")], "must be a date"),
    ([("date_from", "2024-01-30"), ("date_to", "2024-01-01")], "after"),
    ([("date_from", "2023-01-01"), ("date_to", "2024-01-30")], "longer than"),
    ([*D, ("date_from", "2024-01-02")], "more than once"),
    ([*D, ("platfrom", "Google")], "unknown filter"),
    ([*D, ("sub_group_name", "x")], "unknown filter"),  # a deep-dive filter on the overview page
    ([*D, ("platform", "Bing")], "must be one of"),
    ([*D, ("campaign_name", "a\nb")], "printable"),
    ([*D, *[("campaign_name", f"c{i}") for i in range(26)]], "at most"),
])
def test_parse_rejects(cfg, items, reason):
    with pytest.raises(FilterError, match=reason):
        parse_filters(cfg, "overview", items)


@pytest.mark.parametrize("items, reason", [
    ([*D, ("spend_min", "nan")], "finite"),
    ([*D, ("spend_min", "inf")], "finite"),
    ([*D, ("spend_min", "ten")], "must be a number"),
    ([*D, ("spend_min", "9"), ("spend_max", "1")], "above"),
])
def test_parse_rejects_bad_ranges(cfg, items, reason):
    with pytest.raises(FilterError, match=reason):
        parse_filters(cfg, "deep_dive", items)


def test_where_applies_each_filter_only_to_its_tables(cfg):
    flt = parse_filters(cfg, "deep_dive", [*D, ("platform", "Google"), ("severity", "CRITICAL")])
    gold = build_where(cfg, "gold", "date", flt, "duckdb")
    anomalies = build_where(cfg, "anomalies", "date", flt, "duckdb")
    assert gold.startswith("date BETWEEN DATE '2024-01-01' AND DATE '2024-01-30'")
    assert "platform IN ('Google')" in gold and "severity" not in gold
    assert "platform IN ('Google')" in anomalies and "severity IN ('CRITICAL')" in anomalies


def test_only_narrows_to_the_given_columns(cfg):
    flt = parse_filters(cfg, "deep_dive", [*D, ("platform", "Google"), ("campaign_name", "x")])
    where = build_where(cfg, "gold", "date", flt, "duckdb", only={"platform"})
    assert "platform" in where and "campaign_name" not in where


def test_a_very_long_selection_is_refused(cfg):
    names = tuple("c" * 100 + str(i) for i in range(25))
    flt = Filters(date(2024, 1, 1), date(2024, 1, 30), (("campaign_name", names),))
    with pytest.raises(FilterError, match="too many"):
        build_where(cfg, "gold", "date", flt, "duckdb")


def test_an_injection_attempt_is_a_value_that_matches_nothing(cfg, duck, pack):
    flt = parse_filters(cfg, "overview", [*D, ("campaign_name", "x') OR 1=1 --")])
    where = build_where(cfg, "gold", "date", flt, "duckdb")
    df = duck.query(f"SELECT COUNT(*) AS n FROM {pack.table_ref('gold', 'duckdb')} WHERE {where}")
    assert df["n"].iloc[0] == 0
