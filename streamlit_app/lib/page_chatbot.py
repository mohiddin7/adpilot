"""
lib/page_chatbot.py — Page-aware chatbot embedded in the sidebar of every page.

Major changes from v1:
  - Now delegates to the FULL chain-of-thought pipeline (lib/cot_chat) so it
    can actually query BigQuery — not just echo page_summary.
  - Page context is injected as a synthetic conversation turn, so the LLM
    has context about what the user is viewing.
  - iMessage-style chat bubbles: user on the right (blue), bot on the left
    (transparent). Looks like a real chat client.
  - Fixed text input — proper height, no overlap with placeholder.
  - All safety layers still apply (sanitize input, scope guard, SQL validator).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd
import streamlit as st

from . import config
from .cot_chat import process_question, ChatResponse
from .llm_client import LLMClient
from .sql_validator import html_escape_for_display

log = logging.getLogger(__name__)


# ── CSS: chat bubbles + sidebar polish ────────────────────────────────────────
_CHATBOT_CSS = """
<style>
.page-chatbot-container {
    margin: 12px 0 12px 0;
    padding: 14px 14px 10px 14px;
    background: linear-gradient(180deg, rgba(37,99,235,0.08), rgba(124,58,237,0.06));
    border: 1px solid rgba(37,99,235,0.25);
    border-radius: 12px;
    position: relative;
}
.page-chatbot-container::before {
    content: '';
    position: absolute;
    top: -1px; left: 12px; right: 12px;
    height: 2px;
    background: linear-gradient(90deg, #2563EB, #7C3AED);
    border-radius: 2px;
}
.page-chatbot-title {
    font-size: 12px;
    font-weight: 700;
    color: rgba(255,255,255,0.95);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 6px;
}
.page-chatbot-subtitle {
    font-size: 11px;
    color: rgba(255,255,255,0.55);
    margin-bottom: 12px;
    line-height: 1.45;
}

/* Bubble container */
.pc-history { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }

/* Base bubble */
.pc-bubble {
    max-width: 88%;
    padding: 8px 11px;
    border-radius: 12px;
    font-size: 13px;
    line-height: 1.45;
    word-wrap: break-word;
    box-shadow: 0 1px 2px rgba(0,0,0,0.18);
    animation: fadein 0.2s ease-out;
}
@keyframes fadein { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }

/* User bubble — RIGHT, blue */
.pc-bubble.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #2563EB, #1D4ED8);
    color: #fff;
    border-bottom-right-radius: 3px;
}

/* Bot bubble — LEFT, transparent */
.pc-bubble.bot {
    align-self: flex-start;
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.09);
    color: rgba(255,255,255,0.92);
    border-bottom-left-radius: 3px;
}
.pc-bubble.bot.error { border-left: 3px solid #FE2C55; }

/* "Thinking…" bubble with animated dots */
.pc-bubble.bot.thinking {
    display: inline-flex;
    align-items: center;
    padding: 10px 14px;
}
.pc-thinking-dots { display: inline-flex; gap: 4px; align-items: center; }
.pc-thinking-dots span {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: rgba(255,255,255,0.55);
    animation: pc-pulse 1.2s infinite ease-in-out;
}
.pc-thinking-dots span:nth-child(1) { animation-delay: -0.32s; }
.pc-thinking-dots span:nth-child(2) { animation-delay: -0.16s; }
@keyframes pc-pulse {
    0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
    40%           { transform: scale(1.0); opacity: 1.0; }
}

/* Text input: pad properly so placeholder doesn't overlap */
.page-chatbot-container .stTextArea textarea,
.page-chatbot-container .stTextInput input {
    min-height: 44px !important;
    font-size: 13px !important;
    padding: 10px 12px !important;
    background: rgba(0,0,0,0.25) !important;
    border: 1px solid rgba(255,255,255,0.15) !important;
}
.page-chatbot-container .stTextInput input:focus {
    border-color: #2563EB !important;
    box-shadow: 0 0 0 1px #2563EB !important;
}

/* Intent badge inside bot bubble */
.pc-intent {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 1px 6px;
    border-radius: 8px;
    margin-bottom: 4px;
}
.pc-intent.factual    { background: rgba(37,99,235,0.18);  color: #93C5FD; }
.pc-intent.suggestion { background: rgba(5,150,105,0.18);  color: #6EE7B7; }
.pc-intent.analytical { background: rgba(124,58,237,0.18); color: #C4B5FD; }
.pc-intent.off_topic  { background: rgba(245,158,11,0.18); color: #FCD34D; }
.pc-intent.refused    { background: rgba(254,44,85,0.18);  color: #FCA5A5; }

/* Status hint */
.pc-meta {
    font-size: 10px;
    color: rgba(255,255,255,0.4);
    margin-top: 4px;
    text-align: right;
}
</style>
"""


def _format_page_context_message(page_name: str, page_summary: dict) -> str:
    """Build a synthetic assistant message describing the current page."""
    if not page_summary:
        return f"I'm currently viewing the {page_name} page."
    lines = [f"I'm viewing the {page_name} page, which shows:"]
    for key, val in page_summary.items():
        # Escape both — page_summary values are usually safe but defense in depth
        safe_k = html_escape_for_display(str(key))
        safe_v = html_escape_for_display(str(val))
        lines.append(f"- {safe_k}: {safe_v}")
    return "\n".join(lines)


def _render_bubble(role: str, content: str, intent: Optional[str] = None,
                   is_error: bool = False) -> str:
    """Return HTML for a single chat bubble."""
    safe = html_escape_for_display(content).replace("\n", "<br>")
    if role == "user":
        return f"<div class='pc-bubble user'>{safe}</div>"
    badge = ""
    if intent:
        badge = f"<div class='pc-intent {intent}'>{intent.upper()}</div>"
    err_cls = " error" if is_error else ""
    return f"<div class='pc-bubble bot{err_cls}'>{badge}{safe}</div>"


# ── Page-specific suggestion chips shown in the animated beacon ───────────────
_PAGE_SUGGESTIONS: dict[str, list[str]] = {
    "home": [
        "What action should I take this week?",
        "Why is TikTok flagged for review?",
        "Explain the budget reallocation",
    ],
    "performance overview": [
        "Which campaign has the best Cost per Acquisition?",
        "What's driving our CPA trend?",
        "Compare platforms across all metrics",
    ],
    "ai insights": [
        "Why was this campaign flagged as an anomaly?",
        "What does the forecast mean for our budget?",
        "Explain the budget reallocation in plain English",
    ],
    "channel deep dives": [
        "How does Facebook compare to Google?",
        "What's TikTok's biggest weakness?",
        "Show all platform metrics side by side",
    ],
    "chat with your data": [
        "What was spend by platform?",
        "Which campaign has the worst Cost per Acquisition?",
        "Show all anomalies sorted by severity",
    ],
}


def _get_suggestions(page_name: str) -> list[str]:
    key = page_name.lower()
    for k, v in _PAGE_SUGGESTIONS.items():
        if k in key:
            return v
    return ["What's the top opportunity?", "Which platform should I focus on?", "Show me the data"]


def _render_ai_beacon(page_name: str) -> None:
    """
    Floating AI beacon with SVG perimeter trace animation.

    The "traveling light" is an SVG <rect> with stroke-dashoffset animation:
    a single short bright dash moves along the rectangle's border path.
    No CSS pseudo-elements, no conic-gradient, no mask — just an SVG
    attribute animation. Works in every browser and inside Streamlit's iframe.
    """
    suggestions = _get_suggestions(page_name)
    chips_js = ",".join(f'"{s}"' for s in suggestions)
    pid = re.sub(r"[^a-zA-Z0-9]", "", page_name)[:16]

    beacon_html = f"""
<style>
.aib{pid} {{
  position: fixed;
  bottom: 22px;
  right: 22px;
  z-index: 99999;
  font-family: 'Inter', -apple-system, sans-serif;
  animation: aibIn{pid} 0.5s cubic-bezier(0.34,1.56,0.64,1) 0.3s both;
}}
@keyframes aibIn{pid} {{
  from {{ opacity:0; transform: scale(0.5) translateY(16px); }}
  to   {{ opacity:1; transform: scale(1) translateY(0); }}
}}
.aibBox{pid} {{
  position: relative;
  width: 252px;
  background: rgba(10,12,19,0.95);
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 12px;
  overflow: visible;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  cursor: pointer;
}}
.aibSvg{pid} {{
  position: absolute;
  inset: -1px;
  width: calc(100% + 2px);
  height: calc(100% + 2px);
  pointer-events: none;
  overflow: visible;
}}
.aibHd{pid} {{
  display: flex; align-items: center; gap: 7px;
  padding: 8px 10px 5px 10px;
}}
.aibDot{pid} {{
  width: 18px; height: 18px; border-radius: 50%;
  background: linear-gradient(135deg, #6366F1, #06B6D4);
  display: flex; align-items: center; justify-content: center;
  font-size: 9px; color: white; flex-shrink: 0;
}}
.aibTtl{pid} {{
  font-size: 12px; font-weight: 600; color: rgba(255,255,255,0.88);
  flex: 1; white-space: nowrap;
}}
.aibX{pid} {{
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.5);
  cursor: pointer; font-size: 13px; line-height: 1;
  padding: 1px 6px; border-radius: 4px;
}}
.aibX{pid}:hover {{ background: rgba(255,255,255,0.14); color: rgba(255,255,255,0.85); }}
.aibBd{pid} {{ padding: 0 10px 9px 10px; }}
.aibHint{pid} {{
  font-size: 11px; color: rgba(255,255,255,0.36);
  display: flex; align-items: center; gap: 4px; margin-bottom: 4px;
}}
.aibArr{pid} {{
  color: #7C3AED;
  animation: aibNdg{pid} 1.6s ease-in-out infinite;
}}
@keyframes aibNdg{pid} {{
  0%,100% {{ transform: translateX(0); }}
  50%     {{ transform: translateX(-3px); }}
}}
.aibChip{pid} {{
  font-size: 11.5px; color: #A5B4FC;
  background: rgba(99,102,241,0.10);
  border: 1px solid rgba(99,102,241,0.16);
  border-radius: 7px; padding: 3px 8px;
  line-height: 1.4; transition: opacity 0.3s ease;
}}
.aibBox{pid}.col .aibBd{pid},
.aibBox{pid}.col .aibX{pid},
.aibBox{pid}.col .aibTtl{pid} {{ display: none; }}
.aibBox{pid}.col {{ width: 106px; border-radius: 20px; }}
.aibBox{pid}.col .aibHd{pid} {{ padding: 8px 10px; }}
.aibBox{pid}.col::after {{
  content: 'AI Chat'; font-size: 11px; font-weight: 600;
  color: rgba(255,255,255,0.68);
}}
.aibBox{pid}.col .aibSvg{pid} {{ display: none; }}
</style>

<div class="aib{pid}">
  <div class="aibBox{pid}" id="aibB{pid}">
    <svg class="aibSvg{pid}" xmlns="http://www.w3.org/2000/svg">
      <rect x="0.5" y="0.5" rx="12" ry="12"
            width="calc(100% - 1px)" height="calc(100% - 1px)"
            fill="none" stroke="url(#aibG{pid})" stroke-width="1.5"
            stroke-dasharray="40 640" stroke-linecap="round">
        <animate attributeName="stroke-dashoffset"
                 from="0" to="-680" dur="3.5s" repeatCount="indefinite"/>
      </rect>
      <defs>
        <linearGradient id="aibG{pid}">
          <stop offset="0%" stop-color="#6366F1"/>
          <stop offset="50%" stop-color="#A78BFA"/>
          <stop offset="100%" stop-color="#06B6D4"/>
        </linearGradient>
      </defs>
    </svg>
    <div class="aibHd{pid}">
      <div class="aibDot{pid}">&#10022;</div>
      <span class="aibTtl{pid}">Ask about this page</span>
      <button class="aibX{pid}" id="aibXBtn{pid}">&#8212;</button>
    </div>
    <div class="aibBd{pid}">
      <p class="aibHint{pid}">
        <span class="aibArr{pid}">&larr;</span> Open sidebar to chat
      </p>
      <div class="aibChip{pid}" id="aibCh{pid}"></div>
    </div>
  </div>
</div>

<script>
(function() {{
  var box  = document.getElementById('aibB{pid}');
  var xBtn = document.getElementById('aibXBtn{pid}');
  var chip = document.getElementById('aibCh{pid}');
  var sk   = 'aibC_{pid}';
  var ci   = 0;
  var sugs = [{chips_js}];
  
  if (!box) return;
  if (sessionStorage.getItem(sk) === '1') box.classList.add('col');
  if (chip && sugs.length) chip.textContent = sugs[0];
  
  setInterval(function() {{
    if (box.classList.contains('col') || !chip) return;
    chip.style.opacity = '0';
    setTimeout(function() {{
      ci = (ci + 1) % sugs.length;
      chip.textContent = sugs[ci];
      chip.style.opacity = '1';
    }}, 300);
  }}, 3200);
  
  var at = setTimeout(function() {{
    if (!box.classList.contains('col')) {{
      box.classList.add('col');
      sessionStorage.setItem(sk, '1');
    }}
  }}, 9000);
  
  box.addEventListener('mouseenter', function() {{ clearTimeout(at); }});
  
  // MINUS BUTTON CLICK: Minimize the widget
  xBtn.addEventListener('click', function(e) {{
    e.stopPropagation();
    box.classList.add('col');
    sessionStorage.setItem(sk, '1');
  }});
  
  // WIDGET BODY CLICK: Open Sidebar and Focus Chat
  box.addEventListener('click', function(e) {{
    if (e.target === xBtn) return;
    
    // 1. If the widget is minimized, expand it
    if (box.classList.contains('col')) {{
      box.classList.remove('col');
      sessionStorage.removeItem(sk);
      clearTimeout(at);
    }}
    
    // 2. Click Streamlit's native sidebar open button if it exists
    var openBtn = document.querySelector('[data-testid="stSidebarCollapsedControl"] button');
    if (openBtn) openBtn.click();
    
    // 3. Focus the input field (Wrapped in a short timeout to let the sidebar mount)
    setTimeout(function() {{
      var sels = [
        '[data-testid="stSidebarContent"] input[type="text"]',
        '[data-testid="stSidebar"] input[type="text"]',
        'section[data-testid="stSidebar"] input',
      ];
      for (var i = 0; i < sels.length; i++) {{
        var inps = document.querySelectorAll(sels[i]);
        if (inps.length) {{
          inps[inps.length - 1].focus();
          inps[inps.length - 1].scrollIntoView({{ behavior: 'smooth', block: 'center' }});
          return;
        }}
      }}
    }}, 100);
  }});
}})();
</script>
"""
    st.markdown(beacon_html, unsafe_allow_html=True)

def render_page_chatbot(
    page_name:    str,
    page_summary: Optional[dict] = None,
    height_hint:  int = 240,   # kept for backwards compat, unused now
) -> None:
    """
    Render a page-aware chatbot at the bottom of the sidebar.

    Also injects an animated floating AI beacon into the main content area
    that draws attention to the sidebar chat with rotating page-specific
    suggestion chips.

    Behavior:
      - Page context (page_name + page_summary) is injected as a synthetic
        prior assistant message, so the LLM knows what the user is looking at.
      - User's question goes through the FULL COT pipeline — sanitize,
        scope guard, intent classify, SQL agent or planner, validate, execute,
        narrate.
      - Result rendered as iMessage-style bubbles (user right, bot left).
    """
    # Inject the floating beacon into the main content area
    _render_ai_beacon(page_name)
    # Inject CSS once per page
    st.markdown(_CHATBOT_CSS, unsafe_allow_html=True)

    # Per-page session state — two keys: confirmed messages, and a pending Q
    state_key   = f"_pagebot_messages__{page_name}"
    pending_key = f"_pagebot_pending__{page_name}"
    if state_key not in st.session_state:
        st.session_state[state_key] = []
    if pending_key not in st.session_state:
        st.session_state[pending_key] = None

    llm = LLMClient()

    with st.sidebar:
        st.markdown("---")

        # Page-context preamble (visible inside the bordered box)
        st.markdown(
            f"""
            <div class='page-chatbot-container'>
              <div class='page-chatbot-title'>💬 Ask about this page</div>
              <div class='page-chatbot-subtitle'>
                Grounded in what you're viewing. For deeper analysis use the full Chat page.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── Render history bubbles ────────────────────────────────────────────
        # Include the pending question (if any) as a temporary user bubble.
        # When pending is set, we render: history + pending_user + thinking_bubble.
        history = st.session_state[state_key][-6:]
        bubbles_html = ["<div class='pc-history'>"]
        for msg in history:
            if msg["role"] == "user":
                bubbles_html.append(_render_bubble("user", msg["content"]))
            else:
                bubbles_html.append(_render_bubble(
                    "bot", msg["content"],
                    intent=msg.get("intent"),
                    is_error=bool(msg.get("error")),
                ))

        if st.session_state[pending_key]:
            # Show the pending user message immediately
            bubbles_html.append(_render_bubble("user", st.session_state[pending_key]))
            # Show a "thinking" bubble with animated dots
            bubbles_html.append(
                "<div class='pc-bubble bot thinking'>"
                "<span class='pc-thinking-dots'><span></span><span></span><span></span></span>"
                "<span style='margin-left:6px; color:rgba(255,255,255,0.55); "
                "font-size:12px;'>Thinking…</span>"
                "</div>"
            )

        bubbles_html.append("</div>")
        st.markdown("\n".join(bubbles_html), unsafe_allow_html=True)

        # ── If a question is pending, process it now ──────────────────────────
        # This block runs on the rerun triggered when the user clicked "Ask".
        # The pending user bubble + thinking spinner above are already visible.
        if st.session_state[pending_key]:
            pending_q = st.session_state[pending_key]
            # Clear pending FIRST so a refresh during processing doesn't loop
            st.session_state[pending_key] = None
            with st.spinner(""):
                _process(state_key, page_name, page_summary or {}, pending_q, llm)
            st.rerun()

        # ── Input row ─────────────────────────────────────────────────────────
        user_q = st.text_input(
            "Question",
            key=f"_pagebot_input__{page_name}",
            placeholder="Ask anything about your data…",
            label_visibility="collapsed",
        )

        col_a, col_b = st.columns([2, 1])
        with col_a:
            send = st.button("Ask", key=f"_pagebot_send__{page_name}",
                              width='stretch', type="primary")
        with col_b:
            if st.button("Clear", key=f"_pagebot_clear__{page_name}",
                          width='stretch'):
                st.session_state[state_key] = []
                st.session_state[pending_key] = None
                st.rerun()

        if send and user_q and user_q.strip():
            # Stash the question as pending and rerun. The rerun renders the
            # pending bubble + thinking spinner, then processes, then reruns
            # again to show the final answer.
            st.session_state[pending_key] = user_q.strip()
            st.rerun()

        if not llm.is_available:
            st.markdown(
                "<div style='font-size:10px; color:#F59E0B; margin-top:6px;'>"
                "⚠ LLM not configured — chatbot offline"
                "</div>",
                unsafe_allow_html=True,
            )


def _process(
    state_key:    str,
    page_name:    str,
    page_summary: dict,
    question:     str,
    llm:          LLMClient,
) -> None:
    """
    Run a single chat turn through the full COT pipeline with page context.
    Updates session state with the user and bot messages.
    """
    # 1. Append user message first (so UI can show it even on error)
    st.session_state[state_key].append({"role": "user", "content": question})

    if not llm.is_available:
        st.session_state[state_key].append({
            "role":    "bot",
            "content": "I can't answer right now — the AI assistant is offline.",
            "error":   True,
        })
        return

    # 2. Build conversation history WITH a synthetic context-setting message
    context_msg = _format_page_context_message(page_name, page_summary)
    history: list[dict] = [
        {"role": "assistant", "content": context_msg},
    ]

    # Add prior user/assistant exchanges (excluding the one we just appended)
    for m in st.session_state[state_key][:-1]:
        if m.get("role") == "user":
            history.append({"role": "user", "content": m["content"]})
        elif m.get("role") == "bot" and not m.get("error"):
            history.append({"role": "assistant", "content": m["content"]})

    # 3. Delegate to full COT pipeline
    try:
        response: ChatResponse = process_question(
            question=question,
            llm=llm,
            history=history,
        )
    except Exception as exc:
        log.exception("Page chatbot processing failed")
        st.session_state[state_key].append({
            "role":    "bot",
            "content": "Something went wrong. Try rephrasing your question.",
            "error":   True,
        })
        return

    # 4. Append bot message
    st.session_state[state_key].append({
        "role":     "bot",
        "content":  response.content or "I couldn't generate an answer.",
        "intent":   response.intent,
        "error":    bool(response.error),
    })