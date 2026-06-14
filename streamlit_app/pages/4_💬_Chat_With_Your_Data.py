"""
pages/4_💬_Chat_With_Your_Data.py — Conversational analytics with visible
chain-of-thought reasoning.

Pipeline (from lib/cot_chat.py):
  1. Sanitize input (length, control chars, prompt-injection detection)
  2. Scope guard (rule-based off-topic rejection)
  3. Intent classifier (LLM): factual | suggestion | analytical | off_topic
  4a. factual    → SQL agent → validate → execute → narrate
  4b. suggestion → data planner (LLM) → validate → execute → suggestion synthesizer

Every reasoning step is shown to the user. SQL is always visible. Source data
is always available in tabular form.

This is the only page WITHOUT the floating page-chatbot — chat lives here.
"""
import logging

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Chat With Your Data — Marketing Analytics",
    page_icon="💬", layout="wide",
)
log = logging.getLogger(__name__)

try:
    from lib.cot_chat import ChatResponse, ReasoningStep
    from lib.chart_builder import ChartSpec, build_figure
    from lib.llm_client import LLMClient
    from lib import config, glossary
    from lib.page_style import inject_page_style
    from lib.formatters import fmt_currency, fmt_number
    from lib.sql_validator import html_escape_for_display
except ImportError as exc:
    st.error(f"Library import error: {exc}")
    st.stop()

# Apply shared dashboard styling
inject_page_style()

# ── Sidebar: safety status ────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🛡 Safety Layers")
    layers = [
        ("Input length cap",          "≤600 chars"),
        ("Control-char strip",        "active"),
        ("Prompt-injection detect",   "active"),
        ("Scope guard (rule-based)",  "active"),
        ("SQL validator (9 checks)",  "active"),
        ("Table allowlist",           f"{len(config.ALLOWED_TABLES)} tables"),
        ("Statement count check",     "max 1"),
        ("Forbidden keywords",        "23 blocked"),
        ("Row limit",                 f"{config.MAX_RESULT_ROWS}"),
        ("Byte limit / query",        f"{config.MAX_BYTES_CHAT // 1_000_000} MB"),
        ("HTML escape on display",    "active"),
    ]
    for label, val in layers:
        st.markdown(
            f"<div style='font-size:12px; padding:4px 0;'>"
            f"<span style='color:rgba(255,255,255,0.65);'>✓ {label}</span> "
            f"<span style='color:rgba(255,255,255,0.4); float:right;'>{val}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    st.divider()
    if st.button("🗑️ Clear chat history", width='stretch'):
        st.session_state["chat_messages"] = []
        st.session_state["last_sql"] = None
        st.rerun()

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("### Chat With Your Data")
st.markdown(
    "<div style='color:rgba(255,255,255,0.45); margin-top:-12px; margin-bottom:20px; font-size:13px;'>"
    "Ask in plain English. For strategic questions, the assistant uses "
    "<b>chain-of-thought</b> — gathers relevant data first, then synthesizes recommendations."
    "</div>",
    unsafe_allow_html=True,
)

# ── LLM availability ──────────────────────────────────────────────────────────
llm = LLMClient()
if not llm.is_available:
    st.error(
        "**LLM not configured.** Set `LLM_ENDPOINT_URL`, `LLM_BEARER_TOKEN`, and "
        "`LLM_TARGET_MODEL` in `.streamlit/secrets.toml` to enable chat."
    )
    st.stop()

# ── Session state ─────────────────────────────────────────────────────────────
if "chat_messages" not in st.session_state:
    st.session_state["chat_messages"] = []
if "last_sql" not in st.session_state:
    st.session_state["last_sql"] = None
if "last_data" not in st.session_state:
    st.session_state["last_data"] = None  # pd.DataFrame, kept out of chat_messages

# ── Example chips ─────────────────────────────────────────────────────────────
EXAMPLES = [
    ("📊 Factual",     "What was total spend by platform?"),
    ("⭐ Factual",     "Which campaign has the worst Cost per Acquisition?"),
    ("⚠️ Factual",     "Show me all anomalies sorted by severity"),
    ("💡 Suggestion",  "How should we improve TikTok performance?"),
    ("📈 Visualize",   "Chart that as a bar chart"),
]

st.markdown(
    "<div style='font-size:11px; color:rgba(255,255,255,0.45); text-transform:uppercase; "
    "letter-spacing:0.08em; margin-bottom:6px;'>Try one of these</div>",
    unsafe_allow_html=True,
)
chip_cols = st.columns(len(EXAMPLES))
pending = None
for col, (label, q) in zip(chip_cols, EXAMPLES):
    with col:
        if st.button(label, help=q, width='stretch', key=f"chip_{label}"):
            pending = q

st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)


# ── Render a single message ───────────────────────────────────────────────────
def _render_reasoning_steps(steps: list) -> None:
    if not steps:
        return
    with st.expander(f"🧠 Reasoning ({len(steps)} steps)", expanded=False):
        for i, step in enumerate(steps, 1):
            icon = "✓" if step.status == "ok" else "⚠" if step.status == "pending" else "✗"
            color = "#34A853" if step.status == "ok" else "#F59E0B" if step.status == "pending" else "#FE2C55"
            st.markdown(
                f"<div style='padding:6px 0; border-left:2px solid {color}; padding-left:10px; "
                f"margin-bottom:6px;'>"
                f"<div style='font-size:12px; color:{color}; font-weight:600;'>"
                f"{icon} Step {i}: {step.label}</div>"
                f"<div style='font-size:13px; color:rgba(255,255,255,0.75);'>{step.detail}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )


def _render_assistant_message(resp_dict: dict, msg_key: str = "msg") -> None:
    """Render an assistant message stored in session state."""
    with st.chat_message("assistant"):
        # Intent badge
        intent = resp_dict.get("intent") or ""
        if intent:
            badge_colors = {
                "factual":    ("#60A5FA", "rgba(37,99,235,0.12)"),
                "suggestion": ("#34D399", "rgba(5,150,105,0.12)"),
                "analytical": ("#A78BFA", "rgba(124,58,237,0.12)"),
                "visualize":  ("#F472B6", "rgba(219,39,119,0.12)"),
                "off_topic":  ("#F59E0B", "rgba(245,158,11,0.12)"),
                "refused":    ("#FE2C55", "rgba(254,44,85,0.12)"),
            }
            text_c, bg_c = badge_colors.get(intent, ("#888", "rgba(255,255,255,0.06)"))
            st.markdown(
                f"<span style='background:{bg_c}; color:{text_c}; font-size:10px; "
                f"padding:2px 8px; border-radius:10px; font-weight:600; letter-spacing:0.04em;'>"
                f"{intent.upper()}</span>",
                unsafe_allow_html=True,
            )

        # Content
        st.markdown(resp_dict["content"])

        # Reasoning steps
        steps = resp_dict.get("steps", [])
        _render_reasoning_steps(steps)

        # Data preview — reconstruct DataFrame for chart + table
        df_data = resp_dict.get("data")
        if isinstance(df_data, list) and df_data:
            df_data = pd.DataFrame(df_data)

        # Chart (visualize intent) — built via the SAME validated spec,
        # no LLM-generated code is ever executed.
        chart_spec_dict = resp_dict.get("chart_spec")
        if chart_spec_dict and isinstance(df_data, pd.DataFrame) and not df_data.empty:
            try:
                spec = ChartSpec(**chart_spec_dict)
                fig = build_figure(spec, df_data, config.PLATFORM_COLORS)
                st.plotly_chart(fig, width='stretch', config={"displayModeBar": False},
                               key=f"chat_chart_{msg_key}")
            except Exception as exc:
                log.warning("Chart render failed: %s", exc)
                st.info("Couldn't render the chart — see the data table below.")

        if isinstance(df_data, pd.DataFrame) and not df_data.empty:
            with st.expander(f"📊 Data ({len(df_data)} rows)", expanded=False):
                st.dataframe(df_data, width='stretch', hide_index=True)

        # SQL transparency
        if resp_dict.get("sql"):
            with st.expander("🔧 SQL executed"):
                st.code(resp_dict["sql"], language="sql")

        # Duration
        if resp_dict.get("duration_ms"):
            st.markdown(
                f"<div style='font-size:11px; color:rgba(255,255,255,0.3); "
                f"text-align:right;'>{resp_dict['duration_ms']} ms</div>",
                unsafe_allow_html=True,
            )


# ── Render history ────────────────────────────────────────────────────────────
for i, msg in enumerate(st.session_state["chat_messages"]):
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    else:
        _render_assistant_message(msg, msg_key=f"h{i}")


# ── Handle input ──────────────────────────────────────────────────────────────
user_input = st.chat_input("Ask anything about your marketing data…")
question = pending or user_input
from lib.cot_chat import process_question
if question:
    # Show the user message immediately
    with st.chat_message("user"):
        st.markdown(question)
    st.session_state["chat_messages"].append({"role": "user", "content": question})

    # Process through COT pipeline
    with st.spinner("Thinking…"):
        try:
            # Build conversation history from prior session messages
            history = []
            for m in st.session_state["chat_messages"][:-1]:  # exclude the just-added user msg
                if m.get("role") in ("user", "assistant") and m.get("content"):
                    history.append({"role": m["role"], "content": m["content"]})
            
            response: ChatResponse = process_question(
                question=question,
                llm=llm,
                history=history,
                previous_sql=st.session_state.get("last_sql"),
                previous_data=st.session_state.get("last_data"),
            )
        except Exception as exc:
            log.exception("COT processing failed")
            response = ChatResponse(
                content=f"I hit an unexpected error: *{exc}*. Please rephrase or try a simpler question.",
                error=str(exc),
            )

    # Serialize chart_spec (dataclass → dict) for session-state storage
    chart_spec_dict = None
    if response.chart_spec is not None:
        chart_spec_dict = {
            "chart_type": response.chart_spec.chart_type,
            "x":          response.chart_spec.x,
            "y":          response.chart_spec.y,
            "color":      response.chart_spec.color,
            "title":      response.chart_spec.title,
        }

    # Store in session state (as dict, since dataclass + DataFrame don't serialize cleanly)
    response_dict = {
        "role":         "assistant",
        "content":      response.content,
        "intent":       response.intent,
        "sql":          response.sql,
        "data":         response.data.to_dict("records") if isinstance(response.data, pd.DataFrame) and not response.data.empty else None,
        "chart_spec":   chart_spec_dict,
        "steps":        response.steps,
        "duration_ms":  response.duration_ms,
        "error":        response.error,
    }
    st.session_state["chat_messages"].append(response_dict)
    st.session_state["last_sql"] = response.sql

    # Keep the live DataFrame for the NEXT turn's "chart that" request.
    # Only update when this turn actually produced data — a "visualize"
    # turn re-uses the same data, so don't overwrite it with None.
    if isinstance(response.data, pd.DataFrame) and not response.data.empty:
        st.session_state["last_data"] = response.data

    # Re-render this assistant message (unique key = total message count at this moment)
    _render_assistant_message(response_dict, msg_key=f"live_{len(st.session_state['chat_messages'])}")