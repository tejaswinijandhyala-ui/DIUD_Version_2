"""
agents/governance_agent.py -- the Improvement Analysis Agent.

Runs in the background, on a schedule (daily or weekly). Its job is to
look at accumulated feedback and find real, repeated patterns.

Per the updated architecture, this agent's output is now purely
advisory: it logs each genuine pattern it finds (visible in Render's
logs) and returns the list from run_governance_agent() so it can be
surfaced in an admin view if useful. It has no function available to it
that submits anywhere, approves anything, or changes any file. A human
reviewing the logs decides whether a pattern is real and, if so, edits
rules/rules_book.py themselves and opens a pull request -- Git is the
only place a rule actually changes.
"""

import json
import logging

from openai import OpenAI

from governance.feedback_store import get_recent_feedback

client = OpenAI()
logger = logging.getLogger("ri_copilot.governance")

MIN_FEEDBACK_TO_ANALYZE = 3  # a single complaint isn't a pattern; a few similar ones might be

SYSTEM_PROMPT = """You are the Improvement Analysis Agent for a revenue
intelligence copilot. You review a batch of user feedback and look for
genuine, repeated patterns -- not one-off complaints.

For each pattern you find, decide what KIND of fix it points to:
- "new_rule": a business rule is missing or wrong in the Rules Book
- "prompt_improvement": an agent's prompt is causing a repeated mistake
- "validator_rule": the SQL or data validator should catch this but doesn't

Only report patterns that appear multiple times across different
feedback entries. Ignore anything that looks like a single isolated
incident -- that's noise, not a pattern worth a human's time.

Respond with ONLY a JSON array, nothing else. Each item:
{
  "suggestion_type": "new_rule" | "prompt_improvement" | "validator_rule",
  "description": "plain-English summary of the pattern you found",
  "proposed_change": "the specific fix you'd recommend"
}

If you find no genuine patterns, respond with an empty array: []
"""


def run_governance_agent() -> list:
    """
    The entry point for the scheduled background job. Pulls recent
    feedback, asks Claude for real patterns, and logs each one -- never
    writes anywhere. Returns the list of patterns found, for an admin
    view to display if useful.
    """
    feedback = get_recent_feedback(limit=200)

    if len(feedback) < MIN_FEEDBACK_TO_ANALYZE:
        return []  # not enough signal yet -- save the API call

    feedback_text = json.dumps(feedback, indent=2, default=str)

    response = client.messages.create(
        model="gpt-5",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Recent feedback:\n{feedback_text}"}],
    )

    try:
        patterns = json.loads(response.content[0].text.strip())
    except (json.JSONDecodeError, ValueError):
        return []

    for pattern in patterns:
        logger.info(
            "governance suggestion type=%s description=%r proposed_change=%r -- "
            "review and apply manually to rules/rules_book.py if valid",
            pattern.get("suggestion_type", "new_rule"),
            pattern.get("description", ""),
            pattern.get("proposed_change", ""),
        )

    return patterns
