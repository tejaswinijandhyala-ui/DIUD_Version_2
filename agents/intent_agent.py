"""
agents/intent_agent.py — the very first thing that happens to every question.

This agent has exactly one job: read the user's question and figure out
what they're actually asking for. It does NOT write SQL, does NOT look at
business rules, does NOT touch the database. It just translates a human
sentence into a structured shape the rest of the pipeline can act on —
like a receptionist who listens to what you need and fills out a form,
rather than trying to solve your problem themselves.
"""

import json
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from openai import OpenAI

from graph.state import GraphState

client = OpenAI()

# The fixed list of question types this system knows how to handle.
# Keeping this as an explicit list (rather than "whatever Claude feels
# like") means every downstream agent can rely on a small, known set of
# possible values instead of handling infinite free-text categories.
INTENT_CATEGORIES = [
    "metric_lookup", "trend_analysis", "root_cause_analysis", "comparison",
    "ranking", "dashboard_summary", "forecast", "pipeline_analysis",
    "deal_analysis", "risk_identification", "recommendation",
    "data_export", "general_conversation",
]


class IntentOutput(BaseModel):
    """
    The exact shape the Intent Agent must return.

    Using a real schema instead of "please respond in JSON" as a polite
    request matters here: if Claude's response doesn't match this shape,
    we find out immediately, in this one file, instead of it silently
    breaking three steps later when the SQL agent gets confused by a
    missing field.
    """
    intent: str = Field(description="One of the fixed intent categories")
    metrics: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    filters: Dict = Field(default_factory=dict)
    time_period: Optional[str] = None
    needs_clarification: bool = False
    clarification_question: Optional[str] = None


SYSTEM_PROMPT = f"""You are the Intent Agent for a revenue intelligence copilot.

Your only job: read the user's question and classify it. You do not write
SQL, you do not explain business rules, you do not answer the question.

Valid intent categories: {", ".join(INTENT_CATEGORIES)}

If the question is genuinely ambiguous (for example, it's missing a metric,
time period, or is too vague to act on), set needs_clarification to true and
write ONE short, specific clarifying question. Only do this when truly
necessary — most questions should NOT need clarification.

Respond with ONLY valid JSON matching this shape, nothing else, no markdown
fences, no preamble:
{{
  "intent": "...",
  "metrics": ["..."],
  "dimensions": ["..."],
  "filters": {{}},
  "time_period": "..." or null,
  "needs_clarification": true or false,
  "clarification_question": "..." or null
}}
"""


def run_intent_agent(state: GraphState) -> GraphState:
    """
    Called once per question, always first in the pipeline.

    Reads the question and conversation history off the shared state,
    asks Claude to classify it, and writes the structured result back
    onto the state for every later step to use.
    """
    conversation_context = "\n".join(
        f"{turn['role']}: {turn['content']}"
        for turn in state.get("conversation_history", [])
    )

    user_message = (
        f"Conversation so far:\n{conversation_context}\n\n"
        f"Current question: {state['question']}"
    )

    response = client.messages.create(
        model="gpt-5",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = response.content[0].text.strip()

    try:
        parsed = IntentOutput.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValueError) as e:
        # If Claude didn't return valid JSON, we do NOT guess what it meant.
        # We fall back to asking the user directly — this is the same
        # "safe exit, never fabricate" principle from retry_policy.py,
        # applied to a parsing failure instead of a retry-limit failure.
        state["needs_clarification"] = True
        state["clarification_question"] = (
            "I had trouble understanding that question — could you rephrase it?"
        )
        state.setdefault("errors", []).append(f"intent_agent_parse_error: {e}")
        return state

    state["intent"] = parsed.intent
    state["metrics"] = parsed.metrics
    state["dimensions"] = parsed.dimensions
    state["filters"] = parsed.filters
    state["time_period"] = parsed.time_period
    state["needs_clarification"] = parsed.needs_clarification
    state["clarification_question"] = parsed.clarification_question

    return state
