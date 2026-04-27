from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LlmStatePatch(BaseModel):
    """llm_node에서 대화를 보고 state에 반영할 추출 결과."""

    env: Optional[Literal["PRD", "STP", "DEV"]] = None
    env_confirmed: bool = False
    domain: Optional[str] = None
    domain_confirmed: bool = False
    splunk_earliest: Optional[str] = None
    splunk_latest: Optional[str] = None
    time_confirmed: bool = False
    domain_suggestions: list[str] = Field(default_factory=list)
