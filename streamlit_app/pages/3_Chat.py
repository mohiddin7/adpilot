"""Chat: the full conversation with the analyst. Its own conversation, separate from the page chats."""

import streamlit as st
from lib import chat, view
from lib.controls import md

view.start("Chat", "💬")
chat.thinking_toggle()
st.title("Chat with your data")
st.caption("The AdPilot analyst writes SQL, checks it, and shows its working. Answers come from free models.")

for message in chat.history("chat"):
    with st.chat_message(message["role"]):
        if "answer" in message:
            chat.render_answer(message["answer"])
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
            chat.render_answer(answer)

if chat.history("chat") and st.button("Start a new conversation", key="new_chat"):
    chat.clear("chat")
    st.rerun()
