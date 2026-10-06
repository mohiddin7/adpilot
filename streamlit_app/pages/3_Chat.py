"""Chat: the full conversation with the analyst. Its own conversation, separate from the page chats; the viewer's
past conversations are kept in this browser and listed in the sidebar."""

import streamlit as st
from lib import chat, view
from lib.controls import md

view.start("Chat", "💬")
chat.thinking_toggle()
colors = view.meta_colors()
st.caption("The AdPilot analyst writes SQL, checks it, and shows its working. Answers come from free models.")

with st.sidebar:
    st.subheader("Your chats")
    if st.button("New chat", key="new_chat", type="primary", width="stretch"):
        chat.clear("chat")
        st.rerun()
    if st.session_state.get("chat_saved_unavailable"):
        st.caption("Chats stay in this tab only: this browser doesn't allow saved data.")
    for conv in chat.saved() or []:
        left, right = st.columns([5, 1])
        if left.button(f"{md(conv['title'])} · {conv['updated']}", key=f"open_{conv['id']}", width="stretch"):
            chat.restore(conv)
            st.rerun()
        if right.button("✕", key=f"del_{conv['id']}", help="Delete this chat"):
            chat.forget(conv["id"])
            st.rerun()

for i, message in enumerate(chat.history("chat")):
    with st.chat_message(message["role"]):
        if "answer" in message:
            chat.render_answer(message["answer"], colors=colors, key=f"t{i}")
        elif message.get("error"):
            st.warning(message["content"])
        else:
            st.markdown(md(message["content"]))

question = st.chat_input("Ask about your marketing data…", key="page_chat")
if question:
    with st.chat_message("user"):
        st.markdown(md(question))
    with st.chat_message("assistant"):
        answer = chat.ask_and_record(question, "", "chat")
        if answer is not None:
            chat.render_answer(answer, colors=colors, key="new")
    chat.remember_current()

chat.sync_browser()  # last: carries every change above to this browser
