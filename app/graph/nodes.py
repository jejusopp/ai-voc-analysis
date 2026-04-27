from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from app.config import get_settings
from app.domain_registry import DomainRegistry
from app.graph.schemas import LlmStatePatch
from app.llm_factory import get_chat_model
from app.state import VocState
from app.tools.splunk_search import search_errors

logger = logging.getLogger(__name__)


@lru_cache
def _registry_for_path(path_str: str) -> DomainRegistry:
    return DomainRegistry.load(Path(path_str))


def _domains_prompt() -> str:
    p = get_settings().domains_yaml_path.resolve().as_posix()
    return _registry_for_path(p).prompt_block()


_ASK_ENV_MARKER = "PRD(운영) 환경 기준으로 조회할게요"


def _message_text(m: AnyMessage) -> str:
    c = getattr(m, "content", "")
    return c if isinstance(c, str) else str(c)


def _last_human_text(messages: list[AnyMessage]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
            return _message_text(m).strip()
    return ""


def _had_env_question_before_last_user(messages: list[AnyMessage]) -> bool:
    if not messages:
        return False
    rest = list(messages)
    while rest and (isinstance(rest[-1], HumanMessage) or getattr(rest[-1], "type", "") == "human"):
        rest.pop()
    for m in rest:
        if isinstance(m, AIMessage) or getattr(m, "type", "") == "ai":
            if _ASK_ENV_MARKER in _message_text(m):
                return True
    return False


def _parse_explicit_env(text: str) -> Literal["PRD", "STP", "DEV"] | None:
    raw = text.strip()
    if not raw:
        return None
    u = raw.upper()
    if re.search(r"\bSTP\b", u) or "스테이징" in raw:
        return "STP"
    if re.search(r"\bDEV\b", u) or "개발 환경" in raw or "개발환경" in raw.replace(" ", ""):
        return "DEV"
    if re.search(r"\bPRD\b", u) or "운영 환경" in raw or bool(re.search(r"(^|\s)운영(\s|$|,|이|로|요)", raw)):
        return "PRD"
    return None


def _is_affirmative_env_reply(text: str) -> bool:
    """환경 확인 질문 직후 짧은 긍정(기본 PRD 동의)으로 볼 만한 답."""
    s = text.strip().lower()
    if not s or len(s) > 48:
        return False
    if "아니" in s or "아닌" in s or "말고" in s:
        return False
    if re.search(r"\bstp\b", s) or "스테이징" in text or re.search(r"\bdev\b", s) or "개발" in text:
        return False
    affirm = ("네", "예", "응", "ㅇㅇ", "맞", "그래", "ok", "yes", "좋아", "그렇게", "해주", "부탁", "확인", "진행")
    if any(t in s for t in affirm):
        return True
    if "prd" in s or "운영" in text:
        return True
    return False


def _merge_env_strict(state: VocState, patch: LlmStatePatch, msgs: list[AnyMessage]) -> dict:
    """
    아키텍처 §5-1: PRD 기본 제안 후 사용자 확인 전에는 env 확정 금지.
    LLM의 env_confirmed 는 신뢰하지 않는다(모델이 true 로 잘못 내는 경우 방지).
    """
    last_human = _last_human_text(msgs)
    explicit = _parse_explicit_env(last_human)
    asked = _had_env_question_before_last_user(msgs)

    env = state.get("env")
    if patch.env is not None:
        env = patch.env
    if explicit is not None:
        env = explicit

    confirmed = False
    if explicit is not None:
        confirmed = True
    elif asked and _is_affirmative_env_reply(last_human):
        confirmed = True
        if env is None:
            env = "PRD"

    out: dict = {"env_confirmed": confirmed}
    if env is not None:
        out["env"] = cast(Literal["PRD", "STP", "DEV"], env)
    return out


async def llm_node(state: VocState) -> dict:
    """대화에서 환경·시간·도메인 슬롯을 구조화 추출해 state에 반영."""
    model = get_chat_model().with_structured_output(LlmStatePatch)
    sys = SystemMessage(
        content=(
            "당신은 VOC(고객 문의)를 해석해 Splunk 조회에 필요한 정보를 채우는 분석가입니다.\n"
            "규칙:\n"
            "1) 환경 PRD/STP/DEV: 사용자가 운영/스테이징/개발 또는 PRD/STP/DEV 를 말하면 env에 반영. "
            "구조화 필드 env_confirmed 는 항상 false 로 두세요(앱에서만 환경 확정을 처리함).\n"
            "2) 시간: 구체적 시각이 있으면 Splunk용 earliest/latest 문자열을 제안하고 time_confirmed=true. "
            "모르면 false이며 splunk_earliest/splunk_latest 는 비워도 됩니다.\n"
            "3) 도메인: 아래 목록의 name 과 일치하는 서비스가 VOC에 분명하면 domain 에 정확한 name, "
            "domain_confirmed=true.\n"
            "불명확하면 domain_suggestions 에 목록에 있는 name 최대 3개만.\n\n"
            "도메인 목록:\n"
            f"{_domains_prompt()}"
        )
    )
    msgs = [sys, *state.get("messages", [])]
    try:
        patch: LlmStatePatch = await model.ainvoke(msgs)
    except Exception:
        logger.exception("llm_node structured invoke 실패")
        return {}

    out: dict = {}
    out.update(_merge_env_strict(state, patch, msgs))
    if patch.domain is not None:
        out["domain"] = patch.domain
    if patch.domain_confirmed:
        out["domain_confirmed"] = True
    if patch.domain_suggestions:
        out["domain_suggestions"] = patch.domain_suggestions[:3]
    if patch.splunk_earliest and patch.splunk_latest:
        out["time_range"] = {"earliest": patch.splunk_earliest, "latest": patch.splunk_latest}
    if patch.time_confirmed:
        out["time_confirmed"] = True
    return out


def ask_env_node(_state: VocState) -> dict:
    text = (
        "PRD(운영) 환경 기준으로 조회할게요, 맞나요?\n"
        "아니라면 STP(스테이징) 또는 DEV(개발) 중 선택해 주세요."
    )
    return {"messages": [AIMessage(content=text)]}


def ask_issue_node(_state: VocState) -> dict:
    text = (
        "안녕하세요. 분석할 VOC 내용을 알려 주세요.\n"
        "예: \"Subscriber 로그인 시 500 에러\", \"편성 정보가 비어 있어요\""
    )
    return {"messages": [AIMessage(content=text)]}


def ask_time_node(_state: VocState) -> dict:
    text = "언제쯤 발생했나요? (예: 오늘 오후 2시경, 30분 전 등)"
    return {"messages": [AIMessage(content=text)]}


def ask_domain_node(state: VocState) -> dict:
    p = get_settings().domains_yaml_path.resolve().as_posix()
    reg = _registry_for_path(p)
    names = state.get("domain_suggestions") or []
    bullets: list[str] = []
    for n in names[:3]:
        d = reg.resolve(n)
        if d:
            bullets.append(f"- **{d.name}** : {d.description}")
    if not bullets:
        for d in reg.domains[:3]:
            bullets.append(f"- **{d.name}** : {d.description}")
    body = "\n".join(bullets)
    text = (
        "어떤 서비스 쪽 문제인지 확인이 필요합니다. 아래 중 해당하는 항목이 있나요?\n"
        f"{body}\n"
        "- 직접 입력 (예: Subscriber, Payment ...)"
    )
    return {"messages": [AIMessage(content=text)]}


async def splunk_node(state: VocState) -> dict:
    settings = get_settings()
    p = settings.domains_yaml_path.resolve().as_posix()
    reg = _registry_for_path(p)
    env = state.get("env") or "PRD"
    domain_name = state.get("domain") or ""
    d = reg.resolve(domain_name)
    if not d:
        return {
            "log_summary": (
                f"[입력 오류] 도메인 '{domain_name}' 을(를) domains.yaml 목록에서 찾지 못했습니다. "
                "정확한 서비스명을 다시 알려 주세요."
            )
        }
    tr = state.get("time_range") or {}
    earliest = tr.get("earliest") or "-30m@m"
    latest = tr.get("latest") or "now"
    summary = await search_errors(
        settings,
        env=env,
        sourcetype=d.sourcetype,
        earliest=earliest,
        latest=latest,
    )
    return {"log_summary": summary}


async def analyze_node(state: VocState) -> dict:
    """요약 로그를 바탕으로 추정 원인 설명 (근거 함께)."""
    log_summary = state.get("log_summary") or "(로그 없음)"
    model = get_chat_model()
    sys = SystemMessage(
        content=(
            "당신은 사내 로그 분석가입니다. 아래 Splunk 요약을 근거로 "
            "[추정 원인]과 [근거 로그 요약]을 한국어로 간결히 작성하세요. "
            "반드시 추정임을 명시하고, 데이터가 없으면 추가 조회가 필요함을 안내하세요."
        )
    )
    human_content = f"Splunk 요약:\n{log_summary}"
    msgs = [sys, HumanMessage(content=human_content)]
    try:
        res = await model.ainvoke(msgs)
        text = getattr(res, "content", str(res))
        if not isinstance(text, str):
            text = str(text)
    except Exception as e:
        logger.exception("analyze_node 실패")
        text = f"분석 단계에서 오류가 났습니다: {type(e).__name__}: {e}\n\n원본 요약:\n{log_summary}"
    reply = f"분석 결과를 안내드립니다.\n\n{text}"
    return {"messages": [AIMessage(content=reply)]}


def route_after_llm(state: VocState) -> str:
    last = _last_human_text(state.get("messages", []))
    if _is_non_voc_smalltalk(last):
        return "ask_issue"
    if not state.get("env_confirmed"):
        return "ask_env"
    if not state.get("time_confirmed"):
        return "ask_time"
    if not state.get("domain_confirmed"):
        return "ask_domain"
    return "splunk"


def _is_non_voc_smalltalk(text: str) -> bool:
    s = text.strip().lower()
    if not s or len(s) > 24:
        return False

    voc_keywords = (
        "오류",
        "에러",
        "장애",
        "실패",
        "안됨",
        "안 돼",
        "불가",
        "exception",
        "error",
        "로그",
        "splunk",
        "500",
        "503",
        "timeout",
    )
    if any(k in s for k in voc_keywords):
        return False

    greetings = (
        "안녕",
        "안녕하세요",
        "ㅎㅇ",
        "하이",
        "hello",
        "hi",
        "hey",
        "반가",
    )
    return any(g in s for g in greetings)
