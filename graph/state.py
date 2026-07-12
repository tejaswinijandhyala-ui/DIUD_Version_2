"""
GraphState — the shared notebook that every agent and tool shares.

Think of this as one shared piece of paper that gets passed from person to
person along an assembly line. Each agent reads what earlier agents wrote,
adds its own notes, and passes it along. Nothing is hidden between steps —
if you want to know what happened at any point in the pipeline, you just
look at what's written on this notebook at that moment.
"""

from typing import TypedDict, List, Dict, Any, Optional


class GraphState(TypedDict, total=False):
    # ---- What the user asked ----
    # The raw question, plus the conversation so far so follow-ups like
    # "now show only Healthcare" make sense.
    question: str
    conversation_history: List[Dict[str, str]]
    conversation_id: str

    # ---- What we understood about the question ----
    # Filled in by the Intent Agent. This is the "translation" of a human
    # question into something the rest of the system can act on.
    intent: str
    metrics: List[str]
    dimensions: List[str]
    filters: Dict[str, Any]
    time_period: Optional[str]
    needs_clarification: bool
    clarification_question: Optional[str]

    # ---- What we retrieved to answer it ----
    # Filled in by the Rule Loader and Schema Loader. Claude never invents
    # this — it's always looked up from the Rules Book and the schema.
    rules: Dict[str, Any]
    schema: Dict[str, Any]

    # ---- The SQL stage ----
    sql: str
    sql_valid: bool
    sql_validation_errors: List[str]
    sql_retry_count: int

    # ---- The data stage ----
    query_result: List[Dict[str, Any]]
    data_valid: bool
    data_validation_errors: List[str]
    data_retry_count: int

    # ---- The insight stage ----
    analysis: str
    chart_type: Optional[str]
    visualization: Dict[str, Any]

    # ---- The final response ----
    response: str
    response_valid: bool
    response_validation_errors: List[str]
    response_retry_count: int

    # ---- Bookkeeping ----
    # A running list of anything that went wrong anywhere in the pipeline,
    # plus the final outcome so logging/observability has one clear field
    # to check instead of inspecting every other field.
    errors: List[str]
    final_status: str  # "success" | "failed" | "needs_clarification"


def new_state(question: str, conversation_id: str, conversation_history: Optional[List[Dict[str, str]]] = None) -> GraphState:
    """
    Creates a fresh, empty notebook for a new question.
    Every counter starts at zero, every list starts empty — this is the
    one place that defines what a "clean slate" looks like, so no agent
    has to guess at default values.
    """
    return GraphState(
        question=question,
        conversation_id=conversation_id,
        conversation_history=conversation_history or [],
        intent="",
        metrics=[],
        dimensions=[],
        filters={},
        time_period=None,
        needs_clarification=False,
        clarification_question=None,
        rules={},
        schema={},
        sql="",
        sql_valid=False,
        sql_validation_errors=[],
        sql_retry_count=0,
        query_result=[],
        data_valid=False,
        data_validation_errors=[],
        data_retry_count=0,
        analysis="",
        chart_type=None,
        visualization={},
        response="",
        response_valid=False,
        response_validation_errors=[],
        response_retry_count=0,
        errors=[],
        final_status="",
    )
