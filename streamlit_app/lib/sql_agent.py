"""
lib/sql_agent.py — Natural-language to BigQuery SQL via LLM.

The agent generates the SQL string. It does NOT execute it.
sql_validator.py validates it. bq_client.py executes it.
This separation keeps each module testable in isolation.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import config, prompts
from .llm_client import LLMClient

log = logging.getLogger(__name__)


class SQLAgent:
    """
    Translates a natural-language marketing question into a BigQuery
    SELECT statement using the configured LLM.
    """

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self._llm = llm or LLMClient()

    def generate(
        self,
        user_question:   str,
        previous_sql:    Optional[str] = None,
        previous_result: Optional[str] = None,
        history:         Optional[list[dict]] = None,
    ) -> Optional[str]:
        """
        Generate SQL for the user question, with optional conversation memory.

        Args:
            user_question:   The user's natural-language question.
            previous_sql:    SQL from the prior turn (for direct follow-ups).
            previous_result: Short summary of the prior result.
            history:         Last N turns of conversation [{role, content}, ...].
                             Lets the LLM resolve references like "now show TikTok".

        Returns:
            SQL string, or None if LLM is unavailable or failed.
        """
        if not self._llm.is_available:
            log.warning("SQLAgent.generate: LLM not available")
            return None

        # If history is provided, send it as the messages array so the LLM
        # treats it as real conversation context. Otherwise fall back to
        # a single user turn with inline previous_sql for backward compat.
        if history:
            msgs: list[dict] = list(history)
            current = user_question
            if previous_sql:
                current = (
                    f"For reference, the previous SQL I generated was:\n"
                    f"```sql\n{previous_sql}\n```\n\n"
                    f"New question (may reference the previous): {user_question}"
                )
            msgs.append({"role": "user", "content": current})
            sql = self._llm.complete(
                system_prompt=prompts.SQL_AGENT_SYSTEM,
                messages=msgs,
                max_tokens=400,
                temperature=0.1,
            )
        else:
            user_content = user_question
            if previous_sql:
                user_content = (
                    f"Previous query:\n```sql\n{previous_sql}\n```\n"
                    + (f"Previous result summary: {previous_result}\n\n"
                       if previous_result else "\n")
                    + f"Follow-up question: {user_question}"
                )
            sql = self._llm.complete(
                system_prompt=prompts.SQL_AGENT_SYSTEM,
                user_prompt=user_content,
                max_tokens=400,
                temperature=0.1,
            )

        if sql:
            log.debug("SQLAgent generated: %s", sql[:300])
        else:
            log.warning("SQLAgent: empty response for question: %s",
                        user_question[:100])

        return sql