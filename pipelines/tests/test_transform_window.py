import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

t = common.load_script("02_run_transformations")
W = ("2026-09-21", "2026-09-23")


def test_bounded_gold_merge_filters_bronze_and_prunes_gold():
    sql = t.SqlBuilder.gold_merge("Google", W)
    assert "WHERE date BETWEEN '2026-09-21' AND '2026-09-23'" in sql              # bronze source
    assert "AND target.date BETWEEN DATE '2026-09-21' AND DATE '2026-09-23'" in sql  # gold partitions


def test_unbounded_sql_is_unchanged():
    assert "BETWEEN" not in t.SqlBuilder.gold_merge("Google")
    assert "BETWEEN" not in t.SqlBuilder.quarantine_merge("Google")


def test_bounded_quarantine_merge_filters_bronze():
    assert "WHERE date BETWEEN '2026-09-21' AND '2026-09-23'" in t.SqlBuilder.quarantine_merge("TikTok", W)


def test_transaction_bounds_every_platform():
    assert t.SqlBuilder.incremental_transaction(W).count("AND target.date BETWEEN") == 3


def test_window_rejects_non_dates():
    with pytest.raises(ValueError):
        t.SqlBuilder.gold_merge("Google", ("2026-09-21", "x' OR 1=1 --"))


def test_main_requires_both_bounds():
    with pytest.raises(SystemExit) as exc:
        t.main(["--start", "2026-09-21"])
    assert exc.value.code == 2
