from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class TimeRange(TypedDict, total=False):
    """Splunk earliest / latest 문자열 (상대 또는 절대)."""

    earliest: str
    latest: str


class VocState(TypedDict, total=False):
    """아키텍처 §10 상태."""

    messages: Annotated[list[AnyMessage], add_messages]
    env: Literal["PRD", "STP", "DEV"]
    env_confirmed: bool
    domain: str
    domain_confirmed: bool
    domain_suggestions: list[str]
    time_range: TimeRange
    time_confirmed: bool
    user_id: str
    log_summary: str
