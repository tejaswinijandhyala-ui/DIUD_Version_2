"""
agents/insight_agent.py — turns validated data into the actual explanation
a person reads. This is the ONLY agent that writes narrative text.

Critically: this agent only ever runs after tools/data_validator.py has
already approved the results. It never sees raw, unchecked ClickHouse
output — by the time this file runs, the numbers on the state are already
trustworthy. That ordering is what makes "never hallucinate numbers" a
structural fact rather than a hopeful instruction: this agent simply
never gets the chance to talk about unverified data, because the graph
doesn't call it until validation has already passed.

Design note: this agent also produces the final `response` text directly
(the PRD's "Executive Response Agent" role). Keeping insight generation
and response writing as one call, rather than two, is a deliberate
choice — both happen after data is already validated, so combining them
adds no hallucination risk, only saves a redundant API call. Compare
this to the Intent/Clarification split earlier, which was merged for the
same reason.
"""

import json
from openai import OpenAI
from graph.state import GraphState

client = OpenAI()

# Maps each intent to the response shape the PRD calls for. This tells
# Claude which sections are appropriate to include — it does NOT force
# every section into every answer. A metric lookup should stay short; a
# root cause question should be thorough. The intent decides the shape.
INTENT_GUIDANCE = {
    "metric_lookup": "Give a direct, short answer with the number and applied filters. No lengthy explanation.",
    "trend_analysis": "Explain the trend: executive summary, major drivers, and a recommendation if appropriate.",
    "root_cause_analysis": "Full investigation: executive summary, root cause, supporting metrics, contributing dimensions, risks, opportunities, recommended actions.",
    "comparison": "Side-by-side comparison with growth/decline percentages and an executive summary.",
    "ranking": "A ranked list with key observations.",
    "dashboard_summary": "Executive summary, KPI highlights, major trends, risks, opportunities, recommendations.",
    "forecast": "Explain the forecast, its assumptions, and the confidence level plainly.",
    "recommendation": "Data-backed recommendations tied directly to specific numbers.",
    "data_export": "A short note confirming what's being exported and any filters applied.",
    "general_conversation": "Respond naturally and briefly — this may not need any data at all.",
}


def _facts_from_result(rows: list) -> str:
    """
    Turns the validated query result into a plain-text 'fact envelope' —
    the ONLY numbers Claude is allowed to reference in its answer. This
    is handed in as literal data the model reads, not something it has
    to recall or re-derive, which is what makes it a hard boundary
    rather than a soft instruction in the prompt.
    """
    if not rows:
        return "No rows returned."
    # Capped to keep the prompt small — the full result set is still
    # available separately for export; this is only for narration.
    preview = rows[:50]
    return json.dumps(preview, indent=2, default=str)


SYSTEM_PROMPT = """You are the Insight Agent for a revenue intelligence copilot.
You think like a senior Revenue Operations analyst, not a database.

Your only job: explain validated data to the user in clear business
language. You do not write SQL. You do not decide what the data contains
— you only explain what's already there.

Hard rule: every number you mention must come directly from the "Facts"
block you are given. Never estimate, never round in a way that changes
meaning, never state a number that isn't present in the facts. If the
facts don't support something the user is asking about, say so plainly
instead of filling the gap with a plausible-sounding guess.

Write like an experienced analyst talking to a colleague — plain,
confident, specific. Avoid generic phrases like "the data shows." Instead
of "Closed Won is down 12%," write something like "Closed Won revenue
declined 12% compared to last month, driven mainly by fewer Enterprise
deals closing in North America."

Only include the sections that genuinely help this specific answer. Do
not force every response into the same template — a simple lookup should
be short; a root cause question should be thorough.
"""


def run_insight_agent(state: GraphState) -> GraphState:
    """
    Reads the validated query result and writes the actual answer.
    Only ever called after data_validator.py has approved the results.
    """
    facts = _facts_from_result(state.get("query_result", []))
    intent = state.get("intent", "general_conversation")
    guidance = INTENT_GUIDANCE.get(intent, "Answer clearly and concisely.")

    retry_note = ""
    if state.get("response_validation_errors"):
        retry_note = (
            "\n\nYour previous answer had these problems — fix them:\n"
            + "\n".join(f"- {e}" for e in state["response_validation_errors"])
        )

    user_message = f"""Question: {state['question']}
Detected intent: {intent}
Guidance for this intent: {guidance}

Filters applied: {state.get('filters', {})}
Time period: {state.get('time_period', 'not specified')}

Facts (the ONLY numbers you may reference):
{facts}
{retry_note}
"""

    response = client.messages.create(
        model="gpt-5",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text.strip()
    state["analysis"] = text
    state["response"] = text

    return state
