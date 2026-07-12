"""
graph.py — where the whole pipeline gets assembled into one runnable graph.

Everything up to this point (state.py, retry_policy.py, router.py) was
about *rules and definitions*. This file is where those rules actually
become a working pipeline: a list of stops (nodes) and a list of roads
connecting them (edges), some of which are conditional forks.

The individual node functions (do_intent, do_sql_generation, and so on)
live in agents/ and tools/ — we'll write those next. This file doesn't
contain any business logic itself; it only wires pieces together, the
same way a light switch doesn't generate electricity, it just connects
a circuit.
"""

from langgraph.graph import StateGraph, END

from graph.state import GraphState
from graph.router import (
    route_after_intent,
    route_after_sql_validation,
    route_after_data_validation,
    route_after_response_validation,
)

# These will be implemented one by one as we move through agents/ and tools/.
# Importing them here now means graph.py already describes the *complete*
# shape of the pipeline, even before every node's internals are written.
from agents.intent_agent import run_intent_agent
from agents.clarification_agent import run_clarification_agent
from agents.sql_agent import run_sql_agent
from agents.insight_agent import run_insight_agent

from tools.rule_loader import load_rules
from tools.schema_loader import load_schema
from tools.sql_validator import validate_sql
from tools.clickhouse import execute_query
from tools.data_validator import validate_data
from tools.response_validator import validate_response
from tools.visualization_selector import select_visualization
from tools.visualization_validator import validate_visualization
from graph.retry_policy import exit_sql_failed, exit_data_failed, exit_response_failed
from observability.logger import logged_node


def build_graph():
    """
    Builds the graph exactly once, at startup. Think of this function as
    drawing the flowchart in code: every box becomes add_node, every
    arrow becomes add_edge, and every fork becomes add_conditional_edges.
    """
    graph = StateGraph(GraphState)

    # ---- Register every stop on the line ----
    # Every node is wrapped with logged_node(), which records how long it
    # took and whether it errored, in one consistent place — none of the
    # agent or tool files themselves contain any logging code.
    graph.add_node("classify_intent", logged_node("classify_intent")(run_intent_agent))
    graph.add_node("ask_clarification", logged_node("ask_clarification")(run_clarification_agent))
    graph.add_node("retrieve_rules", logged_node("retrieve_rules")(load_rules))
    graph.add_node("retrieve_schema", logged_node("retrieve_schema")(load_schema))
    graph.add_node("generate_sql", logged_node("generate_sql")(run_sql_agent))
    graph.add_node("validate_sql", logged_node("validate_sql")(validate_sql))
    graph.add_node("execute_query", logged_node("execute_query")(execute_query))
    graph.add_node("validate_data", logged_node("validate_data")(validate_data))
    graph.add_node("generate_insight", logged_node("generate_insight")(run_insight_agent))
    graph.add_node("select_visualization", logged_node("select_visualization")(select_visualization))
    graph.add_node("validate_visualization", logged_node("validate_visualization")(validate_visualization))
    graph.add_node("validate_response", logged_node("validate_response")(validate_response))
    graph.add_node("sql_failed_exit", logged_node("sql_failed_exit")(exit_sql_failed))
    graph.add_node("data_failed_exit", logged_node("data_failed_exit")(exit_data_failed))
    graph.add_node("response_failed_exit", logged_node("response_failed_exit")(exit_response_failed))

    # ---- The straight-line road (no decision needed) ----
    graph.set_entry_point("classify_intent")
    graph.add_edge("retrieve_rules", "retrieve_schema")
    graph.add_edge("retrieve_schema", "generate_sql")
    graph.add_edge("generate_sql", "validate_sql")
    graph.add_edge("execute_query", "validate_data")
    graph.add_edge("generate_insight", "select_visualization")
    graph.add_edge("select_visualization", "validate_visualization")
    graph.add_edge("validate_visualization", "validate_response")
    graph.add_edge("ask_clarification", END)  # clarification always ends the turn
    graph.add_edge("sql_failed_exit", END)
    graph.add_edge("data_failed_exit", END)
    graph.add_edge("response_failed_exit", END)

    # ---- The forks (these use router.py to decide the next stop) ----
    graph.add_conditional_edges(
        "classify_intent",
        route_after_intent,
        {"ask_clarification": "ask_clarification", "retrieve_rules": "retrieve_rules"},
    )
    graph.add_conditional_edges(
        "validate_sql",
        route_after_sql_validation,
        {
            "execute_query": "execute_query",
            "generate_sql": "generate_sql",
            "sql_failed_exit": "sql_failed_exit",
        },
    )
    graph.add_conditional_edges(
        "validate_data",
        route_after_data_validation,
        {
            "generate_insight": "generate_insight",
            "generate_sql": "generate_sql",
            "data_failed_exit": "data_failed_exit",
        },
    )
    graph.add_conditional_edges(
        "validate_response",
        route_after_response_validation,
        {
            "return_response": END,
            "generate_insight": "generate_insight",
            "response_failed_exit": "response_failed_exit",
        },
    )

    return graph.compile()


# Built once when the app starts, then reused for every question —
# rebuilding the graph on every request would be pure wasted work.
compiled_graph = build_graph()
