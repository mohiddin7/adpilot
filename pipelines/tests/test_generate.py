import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

gen = common.load_script("00_generate_synthetic")
ingest = common.load_script("01_validate_and_ingest")
TODAY = date(2026, 9, 24)


def _gen(start, end, as_of=None):
    return gen.generate(start, end, as_of or end + timedelta(days=3), today=TODAY)


def test_headers_match_the_real_exports():
    for p, spec in gen.PLATFORMS.items():
        want = list(pd.read_csv(gen.RAW_DIR / spec["csv"], nrows=0).columns)
        assert list(_gen(date(2024, 3, 1), date(2024, 3, 7))[p].columns) == want


def test_every_row_passes_ingest_validation_and_the_gold_filter():
    for p, df in _gen(date(2024, 1, 31), date(2024, 12, 31)).items():
        gate = ingest.DataQualityGate(cost_col=gen.PLATFORMS[p]["cost"])
        bad = [v for v in (gate.validate(r) for r in df.astype(str).to_dict("records")) if v]
        assert not bad, bad[:3]
        assert (df.clicks <= df.impressions).all() and (df.conversions <= df.clicks).all()
        assert (df.select_dtypes("number") >= 0).all().all()


def test_same_arguments_same_bytes():
    a, b = _gen(date(2024, 5, 1), date(2024, 5, 31)), _gen(date(2024, 5, 1), date(2024, 5, 31))
    assert all(a[p].to_csv(index=False) == b[p].to_csv(index=False) for p in a)


def test_extending_the_window_never_changes_earlier_rows():
    short = _gen(date(2024, 5, 1), date(2024, 6, 30), as_of=date(2024, 12, 1))
    long = _gen(date(2024, 5, 1), date(2024, 11, 1), as_of=date(2024, 12, 1))
    for p in short:
        head = long[p][long[p].date <= "2024-06-30"].reset_index(drop=True)
        pd.testing.assert_frame_equal(short[p], head)


def test_window_start_does_not_change_values():
    a, b = _gen(date(2024, 3, 1), date(2024, 3, 31)), _gen(date(2024, 3, 20), date(2024, 3, 31))
    for p in a:
        pd.testing.assert_frame_equal(a[p][a[p].date >= "2024-03-20"].reset_index(drop=True), b[p])


@pytest.mark.parametrize("end", [TODAY, TODAY + timedelta(days=1)])
def test_refuses_today_or_later(end):
    with pytest.raises(ValueError, match="before today"):
        gen.generate(end - timedelta(days=2), end, end + timedelta(days=1), today=TODAY)


def test_refuses_dates_before_history_start_and_as_of_not_after_end():
    with pytest.raises(ValueError):
        gen.generate(date(2024, 1, 30), date(2024, 2, 5), date(2024, 2, 9), today=TODAY)
    with pytest.raises(ValueError):
        gen.generate(date(2024, 2, 1), date(2024, 2, 5), date(2024, 2, 5), today=TODAY)


def test_restatement_is_monotone_final_at_age_three_and_changes_only_conversions():
    d = date(2024, 6, 10)
    by_age = {age: _gen(d, d, as_of=d + timedelta(days=age)) for age in (1, 2, 3, 10)}
    for p in gen.PLATFORMS:
        conv = [by_age[a][p].conversions.sum() for a in (1, 2, 3, 10)]
        assert conv[0] <= conv[1] <= conv[2] == conv[3]
        assert len({tuple(by_age[a][p].campaign_id) for a in by_age}) == 1       # never adds or drops a row
        fixed = [c for c in by_age[1][p].columns if c not in ("conversions", "conversion_value")]
        pd.testing.assert_frame_equal(by_age[1][p][fixed], by_age[10][p][fixed])
    assert len(gen.MATURITY) == common.RESTATING_DAYS      # the pipeline's cutoff matches the restatement


def test_leap_day_and_day_366():
    assert "2024-02-29" in set(_gen(date(2024, 2, 28), date(2024, 3, 1))["google"].date)
    assert all(len(f) for f in _gen(date(2024, 12, 30), date(2024, 12, 31)).values())


def test_calibration_shows_through():
    frames = _gen(date(2024, 10, 1), date(2025, 1, 31), as_of=date(2025, 3, 1))
    conv = pd.concat(frames.values()).groupby("date").conversions.sum()
    conv.index = pd.to_datetime(conv.index)
    assert conv[conv.index.dayofweek >= 5].mean() < conv[conv.index.dayofweek < 5].mean()
    assert conv["2024-12-01":"2024-12-20"].mean() > 1.3 * conv["2025-01-05":"2025-01-31"].mean()
    assert conv["2024-12-02"] > conv["2024-11-25"]            # Cyber Monday 2024 vs the Monday before


def test_roster_launches_and_pauses_with_unique_ids():
    r = gen.roster("facebook", date(2026, 9, 22))
    ids = [c.campaign_id for c in r]
    assert len(ids) == len(set(ids))
    assert sum(c.end is None for c in r) == 4                  # the running count stays at the real 4
    assert sum(c.start > gen.HISTORY_START for c in r) == sum(c.end is not None for c in r) > 0


def test_refuses_an_as_of_after_today():
    with pytest.raises(ValueError):
        gen.generate(date(2026, 9, 20), date(2026, 9, 21), TODAY + timedelta(days=1), today=TODAY)


def test_creative_fatigue_resets_are_staggered_across_campaigns():
    real = [c for p in gen.PLATFORMS for c in gen.roster(p, date(2024, 12, 31)) if c.start == gen.HISTORY_START]
    assert len({gen._creative_phase(c.campaign_id) for c in real}) > 1


def test_every_row_passes_each_platforms_extra_gold_filter():
    # 02's gold_where_extra (frequency/reach, quality_score/search_impression_share, the TikTok video funnel), read
    # from where 02 defines it and evaluated as pandas: a row failing it would silently never reach gold.
    specs = common.load_script("02_run_transformations").SqlBuilder._PLATFORM_CONFIG
    for p, df in _gen(date(2024, 1, 31), date(2024, 12, 31)).items():
        sql = specs[gen.PLATFORMS[p]["label"]]["gold_where_extra"]
        expr = re.sub(r"\bOR\b", "or", re.sub(r"\bAND\b", "and", re.sub(r"(?<![<>!])=", "==", sql)))
        bad = df[~df.eval(expr)]
        assert bad.empty, (p, sql, bad.head(3).to_dict("records"))
