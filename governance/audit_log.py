"""
governance/audit_log.py -- per the updated architecture: "Rule approvals
should not be stored in ClickHouse. Instead, use Git version control,
Pull requests, Code review, Admin approval before merging."

Three jobs remain the same as before, but the third one changed shape:
1. Log every rule-check outcome so a success rate is computable.
2. Cross-reference that log against feedback to flag repeated patterns
   -- never acting on any of it automatically.
3. Draft a SUGGESTED fix for a flagged issue and return it directly in
   the API response. This is now purely advisory: nothing is submitted,
   queued, or "approved" inside the app. A human reads the suggestion,
   decides whether it's right, and -- if so -- edits rules/rules_book.py
   by hand and opens a normal pull request. Git's own history, review,
   and rollback ARE the audit trail here, not an application table.
"""

import re
from datetime import datetime
from typing import Dict, List

from openai import OpenAI

from governance.feedback_store import get_recent_feedback

client = OpenAI()

# Most-recent-first, capped -- in-memory by design, same reasoning as
# memory/session_store.py and governance/feedback_store.py: this is
# operational visibility for the current process's traffic, not a
# permanent record. Render's own logs (via observability/logger.py)
# already capture every individual event if a longer history is needed.
_RULE_AUDIT_LOG: List[dict] = []
_AUDIT_LOG_CAP = 200


def log_rule_audit(sql: str, violations: List[str], stage: str, user_message: str) -> None:
    """
    Records one rule-check outcome -- with or without violations. Tags
    every entry with the classified query pattern, via the SQL
    validator's own detect_intent (imported lazily to avoid a circular
    import -- sql_validator.py calls this function, and this function
    needs sql_validator's detect_intent, so one side has to import late).
    """
    try:
        from tools.sql_validator import detect_intent
        intent = detect_intent(user_message, sql=sql if sql != "summary_check" else "")
        pattern_tag = (
            intent.get("pattern_hint")
            or intent.get("metric")
            or (f"stage_{intent['stage']}" if intent.get("stage") else None)
            or (f"cohort_{intent['cohort_stage']}" if intent.get("cohort_stage") else None)
            or "unclassified"
        )
    except Exception:
        pattern_tag = "unclassified"

    entry = {
        "ts": datetime.utcnow().isoformat(),
        "stage": stage,  # "pre_execute" | "post_execute" | "post_summary"
        "pattern": pattern_tag,
        "violations": violations,
        "sql_preview": sql[:300],
        "user_message": user_message[:200],
    }
    _RULE_AUDIT_LOG.insert(0, entry)
    del _RULE_AUDIT_LOG[_AUDIT_LOG_CAP:]


def get_audit_metrics() -> dict:
    """Success/failure aggregation over the current in-memory window."""
    total = len(_RULE_AUDIT_LOG)
    if total == 0:
        return {
            "total_events": 0, "clean": 0, "with_violations": 0,
            "success_rate_pct": None, "by_rule_id": {}, "by_pattern": {},
            "by_stage": {}, "pattern_rates": {},
        }

    clean = sum(1 for e in _RULE_AUDIT_LOG if not e["violations"])
    with_violations = total - clean

    by_rule_id: Dict[str, int] = {}
    by_pattern: Dict[str, int] = {}
    by_stage: Dict[str, int] = {}
    pattern_totals: Dict[str, Dict[str, int]] = {}

    for e in _RULE_AUDIT_LOG:
        by_stage[e["stage"]] = by_stage.get(e["stage"], 0) + 1
        pt = pattern_totals.setdefault(e["pattern"], {"total": 0, "violations": 0})
        pt["total"] += 1
        if e["violations"]:
            pt["violations"] += 1
            by_pattern[e["pattern"]] = by_pattern.get(e["pattern"], 0) + 1
            for v in e["violations"]:
                rule_id = v.split("]")[0].lstrip("[") if v.startswith("[") else "unknown"
                by_rule_id[rule_id] = by_rule_id.get(rule_id, 0) + 1

    return {
        "total_events": total,
        "clean": clean,
        "with_violations": with_violations,
        "success_rate_pct": round(clean / total * 100, 1),
        "pattern_rates": pattern_totals,
        "by_rule_id": dict(sorted(by_rule_id.items(), key=lambda x: -x[1])),
        "by_pattern": dict(sorted(by_pattern.items(), key=lambda x: -x[1])),
        "by_stage": by_stage,
    }


def get_feedback_metrics() -> dict:
    """Same aggregation shape as before, reading from the in-memory
    feedback log rather than a persisted one."""
    feedback = get_recent_feedback(limit=500)
    total = len(feedback)
    if total == 0:
        return {"total_feedback": 0, "thumbs_up": 0, "thumbs_down": 0,
                "positive_rate_pct": None, "by_pattern": {}, "by_issue_type": {},
                "recent_comments": []}

    def _is_positive(f):
        return f.get("feedback_type") == "correct"

    up = sum(1 for e in feedback if _is_positive(e))
    down = total - up

    by_pattern: Dict[str, Dict[str, int]] = {}
    by_issue_type: Dict[str, int] = {}
    for e in feedback:
        try:
            from tools.sql_validator import detect_intent
            intent = detect_intent(e.get("question", ""), sql=e.get("sql", ""))
            pattern = intent.get("pattern_hint") or intent.get("metric") or "unclassified"
        except Exception:
            pattern = "unclassified"
        rating = "up" if _is_positive(e) else "down"
        p = by_pattern.setdefault(pattern, {"up": 0, "down": 0})
        p[rating] = p.get(rating, 0) + 1
        issue_type = e.get("feedback_type") if not _is_positive(e) else None
        if issue_type:
            by_issue_type[issue_type] = by_issue_type.get(issue_type, 0) + 1

    return {
        "total_feedback": total,
        "thumbs_up": up,
        "thumbs_down": down,
        "positive_rate_pct": round(up / total * 100, 1),
        "by_pattern": dict(sorted(by_pattern.items(), key=lambda kv: kv[1].get("down", 0), reverse=True)),
        "by_issue_type": dict(sorted(by_issue_type.items(), key=lambda x: -x[1])),
        "recent_comments": [
            {"rating": "up" if _is_positive(e) else "down",
             "issue_type": e.get("feedback_type"), "comment": e.get("feedback_note", "")}
            for e in feedback if e.get("feedback_note")
        ][:20],
    }


def get_flagged_issues(
    rule_violation_threshold: int = 3,
    pattern_failure_rate_threshold: float = 30.0,
    pattern_min_sample: int = 5,
    feedback_negative_rate_threshold: float = 40.0,
    feedback_min_sample: int = 3,
    issue_type_threshold: int = 3,
) -> dict:
    """
    Cross-references the rule-violation log and the feedback log, and
    surfaces anything crossing a repeat threshold. Never changes
    behavior itself, and now never writes anything anywhere either --
    it's a pure read-only report a human looks at.
    """
    audit = get_audit_metrics()
    feedback_metrics = get_feedback_metrics()
    flags: List[dict] = []

    for rule_id, count in audit["by_rule_id"].items():
        if count >= rule_violation_threshold:
            flags.append({
                "severity": "high" if count >= rule_violation_threshold * 2 else "medium",
                "source": "rule_engine",
                "subject": rule_id,
                "detail": f"Rule '{rule_id}' has fired {count} times in the current audit window.",
                "suggested_action": "Check whether this rule is too strict, or the model keeps making the same mistake.",
            })

    rule_flagged_patterns = set()
    for pattern, stats in audit.get("pattern_rates", {}).items():
        total = stats["total"]
        violations = stats["violations"]
        if total >= pattern_min_sample:
            rate = violations / total * 100
            if rate >= pattern_failure_rate_threshold:
                rule_flagged_patterns.add(pattern)
                flags.append({
                    "severity": "high" if rate >= 50 else "medium",
                    "source": "rule_engine",
                    "subject": pattern,
                    "detail": f"Question pattern '{pattern}' fails our own rules {rate:.0f}% of the time ({violations}/{total} attempts).",
                    "suggested_action": "This question type may need clearer prompt guidance or a rule review.",
                })

    feedback_flagged_patterns = set()
    for pattern, counts in feedback_metrics.get("by_pattern", {}).items():
        total = counts.get("up", 0) + counts.get("down", 0)
        if total >= feedback_min_sample:
            neg_rate = counts.get("down", 0) / total * 100
            if neg_rate >= feedback_negative_rate_threshold:
                feedback_flagged_patterns.add(pattern)
                flags.append({
                    "severity": "high" if neg_rate >= 60 else "medium",
                    "source": "user_feedback",
                    "subject": pattern,
                    "detail": f"Users rated '{pattern}' answers negatively {neg_rate:.0f}% of the time ({counts.get('down', 0)}/{total} ratings).",
                    "suggested_action": "Check recent comments for this pattern.",
                })

    for issue_type, count in feedback_metrics.get("by_issue_type", {}).items():
        if count >= issue_type_threshold:
            flags.append({
                "severity": "medium",
                "source": "user_feedback",
                "subject": issue_type,
                "detail": f"Users have reported '{issue_type}' {count} times.",
                "suggested_action": "Review recent comments tagged with this issue type.",
            })

    for pattern in (rule_flagged_patterns & feedback_flagged_patterns):
        flags.append({
            "severity": "critical",
            "source": "cross_signal",
            "subject": pattern,
            "detail": f"'{pattern}' is failing BOTH our own rules AND getting negative user feedback.",
            "suggested_action": "Prioritize this one -- it's the strongest signal the system can produce.",
        })

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    flags.sort(key=lambda f: severity_order.get(f["severity"], 4))

    return {
        "flag_count": len(flags),
        "flags": flags,
        "note": "Review surface only -- nothing here changes behavior automatically. A person decides what to act on.",
    }


_RULE_REVIEWER_SYSTEM_PROMPT = """
You are the Rule Reviewer -- an internal tool, never user-facing.

You are given ONE flagged recurring problem. Your job: diagnose the
likely root cause in plain English (2-3 sentences), and draft ONE
short, clear, ADDITIVE clarification (2-4 sentences) that a human could
add to rules/rules_book.py to prevent this specific mistake.

HARD RULES:
- You may only suggest ADDING a clarifying instruction. NEVER propose
  removing, weakening, or contradicting an existing rule.
- If you cannot draft a safe, additive fix with real confidence, say so
  honestly and leave PROPOSED_ADDITION empty.
- Write a plain-English instruction, not SQL or code -- this text is
  meant to be reviewed by a person and, if they agree, pasted directly
  into a rules_book.py entry as part of a normal pull request.

Respond in EXACTLY this format:

DIAGNOSIS: <your diagnosis>
PROPOSED_ADDITION:
<the clarification text, or leave blank if you have no safe fix to propose>
"""


def draft_fix_proposal(flag: dict) -> dict:
    """
    Drafts a SUGGESTED fix for one flagged issue and returns it directly
    -- nothing is stored, queued, or submitted anywhere. This is a
    read-only recommendation: a human decides whether to act on it by
    editing rules/rules_book.py themselves and opening a pull request.
    That PR, its review, and Git's own history are the entire audit
    trail for this suggestion -- there is deliberately no in-app
    "approve" button that pretends to make a change live, since the
    only thing that actually changes behavior is the file on disk.
    """
    user_msg = (
        f"FLAGGED ISSUE:\nSource: {flag['source']}\nSubject: {flag['subject']}\n"
        f"Detail: {flag['detail']}"
    )
    response = client.messages.create(
        model="gpt-5",
        system=_RULE_REVIEWER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=800,
    )
    text = "\n".join(b.text for b in response.content if hasattr(b, "text") and b.text)

    diagnosis_m = re.search(r"DIAGNOSIS:\s*(.*?)(?=\nPROPOSED_ADDITION:)", text, re.S)
    addition_m = re.search(r"PROPOSED_ADDITION:\s*(.*)", text, re.S)
    diagnosis = diagnosis_m.group(1).strip() if diagnosis_m else text[:400]
    addition = addition_m.group(1).strip() if addition_m else ""

    return {
        "flag": flag,
        "diagnosis": diagnosis,
        "proposed_addition": addition,
        "has_safe_fix": bool(addition),
    }
