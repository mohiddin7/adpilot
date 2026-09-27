"""Chat: the full conversation with the analyst. Same session and history as the sidebar box on the other pages."""

import streamlit as st
from lib import chat, view

view.start("Chat", "💬")
chat.thinking_toggle()
st.title("Chat with your data")
st.caption("The AdPilot analyst writes SQL, checks it, and shows its working. Answers come from free models.")

for message in chat.history():
    with st.chat_message(message["role"]):
        if "answer" in message:
            chat.render_answer(message["answer"])
        elif message.get("error"):
            st.warning(message["content"])
        else:
            st.markdown(message["content"])

question = st.chat_input("Ask about your marketing data…", key="page_chat")
if question:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        answer = chat.ask_and_record(question)
        if answer is not None:
            chat.render_answer(answer)

if chat.history() and st.button("Start a new conversation"):
    st.session_state.pop("messages", None)
    st.session_state.pop("session_id", None)
    st.rerun()
