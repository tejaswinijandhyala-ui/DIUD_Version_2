"""
agents/clarification_agent.py — runs only when the Intent Agent flagged
the question as ambiguous.

One deliberate design choice worth calling out: the Intent Agent already
asks Claude to write a clarifying question as part of its single JSON
response (see intent_agent.py's `clarification_question` field). So this
node does NOT make a second Claude call to ask "what should I clarify?" —
that would be redundant, slower, and cost more, for no real benefit.

Instead, this node's job is small and deterministic: take the question
Claude already wrote, turn it into the actual response for this turn, and
mark the state so the graph knows to stop here and wait for the user's
answer. If you ever want a more elaborate clarification flow later (for
example, offering multiple-choice options), this is the file to expand —
but the LLM reasoning for "is this ambiguous, and if so what should I
ask" stays owned by the Intent Agent, so there's only one place that
decision is made.
"""

from graph.state import GraphState


def run_clarification_agent(state: GraphState) -> GraphState:
    """
    Takes the clarifying question already produced by the Intent Agent
    and finalizes it as this turn's response.
    """
    question = state.get("clarification_question")

    if not question:
        # Defensive fallback: we should never get here without a question
        # already set, but if we somehow do, don't send an empty response.
        question = "Could you clarify what you're looking for?"
        state.setdefault("errors", []).append(
            "clarification_agent_called_without_question"
        )

    state["response"] = question
    state["final_status"] = "needs_clarification"

    return state
