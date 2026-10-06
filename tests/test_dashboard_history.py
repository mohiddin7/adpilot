"""The browser-kept chat list: parsing whatever the browser holds, and trimming what goes back."""

import json

import pytest
from lib import history

A, B = "a" * 32, "b" * 32
TURNS = [{"role": "user", "content": "What was spend by platform?"},
         {"role": "assistant", "content": "x", "answer": {"answer_md": "Spend was $5K.", "data": [{"spend": 1.0}],
                                                          "caveats": [], "sql": "SELECT 1", "trace_id": "t", "extra": 1}}]


def test_a_saved_conversation_round_trips():
    convs = history.remember([], A, TURNS, "2026-10-05")
    back = history.parse(history.dumps(convs))
    assert back == convs
    assert back[0]["title"] == "What was spend by platform?" and back[0]["updated"] == "2026-10-05"
    assert "extra" not in back[0]["turns"][1]["answer"]  # only the answer fields the page draws


def test_remembering_moves_the_conversation_first():
    convs = history.remember(history.remember([], A, TURNS, "d1"), B, TURNS, "d2")
    assert [c["id"] for c in history.remember(convs, A, TURNS, "d3")] == [A, B]


def test_forget_removes_only_that_conversation():
    convs = history.remember(history.remember([], A, TURNS, "d"), B, TURNS, "d")
    assert [c["id"] for c in history.forget(convs, A)] == [B]


def test_the_list_keeps_the_newest_thirty():
    convs = []
    for i in range(35):
        convs = history.remember(convs, f"{i:032x}", TURNS, "d")
    kept = history.parse(history.dumps(convs))
    assert len(kept) == 30 and kept[0]["id"] == f"{34:032x}"


def test_the_list_stays_under_200_kb_by_dropping_the_oldest():
    big = [{**TURNS[1], "answer": {**TURNS[1]["answer"], "data": [{"note": "x" * 900}] * 150}}]
    convs = []
    for i in range(5):
        convs = history.remember(convs, f"{i:032x}", TURNS[:1] + big, "d")
    raw = history.dumps(convs)
    assert len(raw.encode()) <= history.MAX_BYTES
    assert history.parse(raw)[0]["id"] == f"{4:032x}"


def test_a_newest_conversation_too_big_alone_keeps_its_words():
    huge = [{**TURNS[1], "answer": {**TURNS[1]["answer"], "data": [{"note": "x" * 1500}] * 200}}]
    raw = history.dumps(history.remember([], A, TURNS[:1] + huge, "d"))
    kept = history.parse(raw)
    assert kept[0]["turns"][1]["answer"]["data"] == [] and kept[0]["turns"][1]["answer"]["answer_md"]


def test_answers_keep_at_most_200_rows():
    rows = [{**TURNS[1], "answer": {**TURNS[1]["answer"], "data": [{"spend": 1.0}] * 500}}]
    assert len(history.parse(history.dumps(history.remember([], A, TURNS[:1] + rows, "d")))[0]["turns"][1]["answer"]["data"]) == 200


@pytest.mark.parametrize("raw", [None, "", "{not json", "[]", "5", '{"conversations": "x"}',
                                 '{"conversations": [{"id": "../etc", "turns": []}]}',
                                 '{"conversations": [{"id": "' + A + '", "turns": [{"role": "system", "content": "x"}]}]}',
                                 json.dumps({"conversations": [{"id": A, "turns": [{"role": "user", "content": 5}]}]}),
                                 {"unavailable": True}, "[" * 200_000, "x" * (4 * history.MAX_BYTES + 1)])
def test_anything_unexpected_in_the_browser_reads_as_no_conversations(raw):
    """Review focus 2: another version, a hand edit or junk never raises."""
    assert history.parse(raw) == []


def _with_chart(chart):
    return history.parse(json.dumps({"conversations": [{"id": A, "turns": [
        {"role": "assistant", "content": "x", "answer": {"answer_md": "m", "data": [{"a": 1}], "chart": chart}}]}]}))[0]["turns"][0]["answer"]


@pytest.mark.parametrize("chart", [{}, {"chart_type": "bar"}, {"chart_type": "bar", "x": 1, "y": "b"},
                                   {"chart_type": "bar", "x": "a", "y": "b", "color": 5}])
def test_a_stored_chart_that_cannot_be_drawn_is_dropped(chart):
    assert "chart" not in _with_chart(chart)


def test_a_valid_stored_chart_is_kept():
    chart = {"chart_type": "bar", "x": "a", "y": "b", "color": None, "title": "T"}
    assert _with_chart(chart)["chart"] == chart


def test_duplicate_ids_keep_the_first():
    turns = [{"role": "user", "content": "q"}]
    raw = json.dumps({"conversations": [{"id": A, "title": "first", "turns": turns},
                                        {"id": A, "title": "second", "turns": turns}]})
    assert [c["title"] for c in history.parse(raw)] == ["first"]
