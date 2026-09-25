"""The only model call in the brief: plain-language wording over facts that code already computed.

The call has no tools, so it cannot fetch a number; each text slot is checked on its own and any slot that
cites a number not in its facts, runs too long or comes back empty falls back to the template from analyses.py.
A failed call means every slot is a template, and the brief is still complete and correct.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from adpilot.brief.analyses import Item
from adpilot.core.agent import _classify
from adpilot.core.audit import (
    RunContextInfo,
    build_record,
    new_trace_id,
    primary_model_name,
    summarize_messages,
)
from adpilot.core.guardrails import redact_output
from adpilot.core.models import NoNullSchemas
from adpilot.core.tools import AgentDeps

# Not a digit glued to a letter ("Q3" is not a figure), and a comma only as a thousands separator.
_NUM = re.compile(r"(?<![A-Za-z_\d])\d+(?:,\d{3})*(?:\.\d+)?")
LIMITS = {"headline": 100, "story": 500, "title": 90, "checked": 300, "do": 200}

INSTRUCTIONS = """\
You write the daily ad brief for the person who owns the budget. They are busy and not an analyst.
- Plain words, short sentences. No jargon: say "cost per sale", not CPA; "ad price", not CPM.
- Use only numbers that appear in the facts, copied exactly as written there. Never compute, round or add a number.
- headline: one sentence that says how many things need the reader today (the facts give the count).
- items: for each item, by its item_id: title (an imperative, under 90 characters), checked (the likely cause, in
  plain words, under 300), do (one concrete move, under 200). Start from the item's template and make it read well.
- story: at most 3 short sentences (under 400 characters) on what matters most today and why. Empty if nothing
  needs the reader."""


class WrittenItem(BaseModel):
    item_id: str
    title: str
    checked: str
    do: str


class Written(BaseModel):  # field order is generation order: a rambling story must not crowd out the items
    headline: str
    items: list[WrittenItem]
    story: str


def template_headline(n: int) -> str:
    if n == 0:
        return "Nothing needs you today."
    return f"{n} thing{'s' if n > 1 else ''} need{'' if n > 1 else 's'} you today. Everything else was checked."


def clean(text: str) -> str:
    """One paragraph of plain text: no links, headings, mentions or markdown emphasis."""
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"(?m)^\s*#+\s*", "", text).replace("@", "")
    text = re.sub(r"[*`<>\[\]|]", "", text)  # underscores stay: campaign names use them; render escapes them
    return " ".join(text.split())


def grounded(text: str, source: str) -> bool:
    """Every number in `text` matches a number in `source` at the text's own precision."""
    have = [float(n.replace(",", "")) for n in _NUM.findall(source)]
    for tok in _NUM.findall(text):
        value, places = float(tok.replace(",", "")), len(tok.split(".")[1]) if "." in tok else 0
        if not any(round(h, places) == value for h in have):
            return False
    return True


def _slot(text: str | None, template: str, source: str, limit: int) -> tuple[str, bool]:
    """(text, used the model's)."""
    text = clean(text or "")
    if text and len(text) <= limit and grounded(text, source):
        return text, True
    return template, False


def facts(items: list[Item], ahead: list[str], followed: list[str]) -> dict:
    return {
        "needs_you_count": len(items),
        "items": [{"item_id": i.id, "subject": i.subject, "happened": i.happened, "confidence": i.confidence,
                   "numbers": i.numbers, "template": {"title": i.title, "checked": i.checked, "do": i.do}} for i in items],
        "looking_ahead": ahead,
        "following_up": followed,
    }


def write(deps: AgentDeps, model: Model | None, items: list[Item], ahead: list[str], followed: list[str], run_id: str
          ) -> tuple[dict, str | None, list[str]]:
    """Returns ({"headline", "story", item_id: {"title", "checked", "do"}}, model used, caveats)."""
    out: dict = {"headline": template_headline(len(items)), "story": ""}
    out.update({i.id: {"title": i.title, "checked": i.checked, "do": i.do} for i in items})
    if model is None:
        return out, None, []
    brief_facts = facts(items, ahead, followed)
    all_text = json.dumps(brief_facts)
    agent: Agent[None, Written] = Agent(model, output_type=Written, instructions=INSTRUCTIONS, retries=1,
                                        name="adpilot-brief", capabilities=[NoNullSchemas()],
                                        model_settings={"max_tokens": 1500})
    started, t0 = datetime.now(UTC), time.perf_counter()
    written, messages, usage, kind, detail = None, [], None, None, ""
    try:
        result = agent.run_sync(json.dumps(brief_facts, indent=1))
        written, messages = result.output, result.new_messages()
        usage = result.usage() if callable(result.usage) else result.usage
    except Exception as exc:  # noqa: BLE001 — a failed call means template wording, never a lost brief
        kind, detail = _classify(exc)
    deps.audit.record(build_record(
        trace_id=new_trace_id(), ts=started, latency_s=time.perf_counter() - t0,
        question="brief writer over " + ", ".join(i.id for i in items),
        answer_md=redact_output(written.model_dump_json())[0] if written else "", sql=None, refused=False,
        confidence=1.0 if written else 0.0, caveats=[f"{kind}: brief writer failed ({detail[:160]})"] if kind else [],
        messages=messages, usage=usage, model_requested=primary_model_name(model),
        context=RunContextInfo(source="brief", run_id=run_id, case_name="writer"),
        pack_name=deps.pack.name, prompt_hash=deps.pack.prompt_hash,
    ))
    if written is None:
        return out, None, [f"BriefWriterFailed: {kind}"]
    used = []
    out["headline"], ok = _slot(written.headline, out["headline"], all_text + f" {len(items)}", LIMITS["headline"])
    used.append(ok)
    if items:
        out["story"], ok = _slot(written.story, "", all_text, LIMITS["story"])
        used.append(ok)
    by_id = {w.item_id: w for w in written.items}
    for i in items:
        w = by_id.get(i.id)
        source = json.dumps(brief_facts["items"][items.index(i)]) + " 7 14"
        for name in ("title", "checked", "do"):
            out[i.id][name], ok = _slot(getattr(w, name) if w else None, out[i.id][name], source, LIMITS[name])
            used.append(ok)
    caveats = [] if all(used) else [f"BriefTemplated: {used.count(False)} of {len(used)} slot(s) from templates"]
    return out, summarize_messages(messages).model_used if any(used) else None, caveats
