"""DuckDBSource concurrency: the eval harness's judge (worker thread) and code graders
(main thread) can both call .query() on the same connection at once (see evals/judge.py
CalibratedJudge.evaluate_async). DuckDB connections aren't safe for concurrent use without
a lock, so this pins that the lock is actually held.
"""

from concurrent.futures import ThreadPoolExecutor

GOLD = "fct_unified_marketing_performance"


def test_concurrent_queries_do_not_error(duck):
    def run(_):
        return duck.query(f"SELECT COUNT(*) AS n FROM {GOLD}")["n"][0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(50)))

    assert len(results) == 50
    assert len(set(results)) == 1  # every thread saw the same, uncorrupted result
