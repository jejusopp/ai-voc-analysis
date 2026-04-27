from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    analyze_node,
    ask_domain_node,
    ask_env_node,
    ask_issue_node,
    ask_time_node,
    llm_node,
    route_after_llm,
    splunk_node,
)
from app.state import VocState


def build_graph(checkpointer: BaseCheckpointSaver | None) -> Any:
    g = StateGraph(VocState)
    g.add_node("llm_node", llm_node)
    g.add_node("ask_issue_node", ask_issue_node)
    g.add_node("ask_env_node", ask_env_node)
    g.add_node("ask_time_node", ask_time_node)
    g.add_node("ask_domain_node", ask_domain_node)
    g.add_node("splunk_node", splunk_node)
    g.add_node("analyze_node", analyze_node)

    g.add_edge(START, "llm_node")
    g.add_conditional_edges(
        "llm_node",
        route_after_llm,
        {
            "ask_issue": "ask_issue_node",
            "ask_env": "ask_env_node",
            "ask_time": "ask_time_node",
            "ask_domain": "ask_domain_node",
            "splunk": "splunk_node",
        },
    )
    g.add_edge("ask_issue_node", END)
    g.add_edge("ask_env_node", END)
    g.add_edge("ask_time_node", END)
    g.add_edge("ask_domain_node", END)
    g.add_edge("splunk_node", "analyze_node")
    g.add_edge("analyze_node", END)

    return g.compile(checkpointer=checkpointer)
