"""The Chat page's conversations, kept in this browser (localStorage `adpilot.chat.v1`, via
components/local_history). Pure: read whatever the browser holds (another version, a hand edit, junk) without
raising, and trim what goes back. Nothing here reaches the server."""

from __future__ import annotations

import json
import re

MAX_CONVERSATIONS = 30
MAX_BYTES = 200_000
MAX_ROWS = 200
TITLE_CHARS = 80
_ID = re.compile(r"^[0-9a-f]{32}$")
_ANSWER = {"answer_md": str, "chart": dict, "data": list, "caveats": list, "sql": str, "trace_id": str}


def _answer(a) -> dict | None:
    if not isinstance(a, dict):
        return None
    out = {k: v for k, v in a.items() if k in _ANSWER and isinstance(v, _ANSWER[k])}
    out["data"] = [r for r in out.get("data", [])[:MAX_ROWS] if isinstance(r, dict)]
    out["caveats"] = [c for c in out.get("caveats", []) if isinstance(c, str)]
    return out


def _turn(t) -> dict | None:
    if not isinstance(t, dict) or t.get("role") not in ("user", "assistant") or not isinstance(t.get("content"), str):
        return None
    out = {"role": t["role"], "content": t["content"]}
    if t["role"] == "assistant":
        if isinstance(t.get("error"), str):
            out["error"] = t["error"]
        elif (a := _answer(t.get("answer"))) is not None:
            out["answer"] = a
    return out


def parse(raw) -> list[dict]:
    """The browser's copy → conversations, newest first. Whatever doesn't fit the shape is dropped, never raised."""
    try:
        convs = json.loads(raw).get("conversations") if isinstance(raw, str) and raw else []
    except (ValueError, AttributeError):
        return []
    out = []
    for c in convs if isinstance(convs, list) else []:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not _ID.match(c["id"]):
            continue
        turns = [t for t in map(_turn, c["turns"]) if t] if isinstance(c.get("turns"), list) else []
        if turns:
            out.append({"id": c["id"], "title": str(c.get("title") or "")[:TITLE_CHARS] or "Untitled",
                        "updated": str(c.get("updated") or "")[:10], "turns": turns})
    return out[:MAX_CONVERSATIONS]


def remember(convs: list[dict], conv_id: str, turns: list[dict], today: str) -> list[dict]:
    """This conversation first (added or updated), titled by its first question; the rest as they were."""
    clean = [t for t in map(_turn, turns) if t]
    rest = [c for c in convs if c["id"] != conv_id]
    if not clean:
        return rest
    first = next((t["content"] for t in clean if t["role"] == "user"), "")
    return [{"id": conv_id, "title": " ".join(first.split())[:TITLE_CHARS] or "Untitled", "updated": today,
             "turns": clean}, *rest]


def forget(convs: list[dict], conv_id: str) -> list[dict]:
    return [c for c in convs if c["id"] != conv_id]


def _size(convs: list[dict]) -> int:
    return len(json.dumps({"conversations": convs}, default=str).encode())


def dumps(convs: list[dict]) -> str:
    """What the browser keeps: the newest MAX_CONVERSATIONS under MAX_BYTES, the oldest dropped first. A newest
    conversation too big on its own keeps its words and drops its tables; if even that is too big, nothing is kept."""
    keep = list(convs[:MAX_CONVERSATIONS])
    while len(keep) > 1 and _size(keep) > MAX_BYTES:
        keep.pop()
    if keep and _size(keep) > MAX_BYTES:
        keep = [{**keep[0], "turns": [{**t, "answer": {**t["answer"], "data": []}} if "answer" in t else t
                                      for t in keep[0]["turns"]]}]
        keep = keep if _size(keep) <= MAX_BYTES else []
    return json.dumps({"conversations": keep}, default=str)
