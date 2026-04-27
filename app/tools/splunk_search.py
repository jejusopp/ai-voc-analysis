from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


def _build_spl(
    env: str,
    sourcetype: str,
    earliest: str,
    latest: str,
) -> str:
    et = {"PRD": "kube:prd", "STP": "kube:stp", "DEV": "kube:dev"}[env]
    return f"""search eventtype="{et}" sourcetype="{sourcetype}"
earliest="{earliest}" latest="{latest}"
| search (" ERROR " OR "Exception" OR "TooManyRequests" OR "API rate limit exceeded")
| rex "(?<timestamp>\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}:\\d{{2}}\\.\\d+) (?<level>\\w+) \\[(?<app>[^,]+),(?<traceId>[^,]*),(?<spanId>[^\\]]*)\\]"
| rex "(?<exception_class>[A-Za-z0-9_.$]+Exception(?:\\$[A-Za-z0-9_.$]+)?): (?<exception_message>[^\\n]+)"
| stats count by exception_class, exception_message
| sort -count
| head 50
"""


def _run_blocking_spl(settings: Settings, spl: str) -> str:
    import splunklib.client as client
    import splunklib.results as results

    if not settings.splunk_token or not settings.splunk_host:
        return "[Splunk 미설정] SPLUNK_HOST / SPLUNK_TOKEN 을 .env 에 설정하세요.\n"

    svc = client.connect(
        host=settings.splunk_host,
        port=settings.splunk_port,
        token=settings.splunk_token,
        autologin=True,
    )
    if settings.splunk_debug_response:
        logger.info("Splunk SPL query:\n%s", spl)

    stream = svc.jobs.oneshot(spl, output_mode="json")
    reader = results.JSONResultsReader(stream)
    rows: list[dict] = []
    for item in reader:
        # splunk-sdk JSONResultsReader 는 dict(RESULT) 또는 Message 객체를 반환한다.
        if isinstance(item, dict):
            rows.append(item)
            if settings.splunk_debug_response:
                logger.info(
                    "Splunk RESULT row: %s",
                    json.dumps(item, ensure_ascii=False, default=str),
                )
            continue
        msg_type = getattr(item, "type", "")
        msg_text = getattr(item, "message", item)
        if msg_type == "ERROR":
            logger.error("Splunk message error: %s", msg_text)
        elif msg_type == "WARN":
            logger.warning("Splunk message warn: %s", msg_text)
        elif settings.splunk_debug_response:
            logger.info("Splunk message %s: %s", msg_type or "UNKNOWN", msg_text)
    if not rows:
        return "조회 결과가 0건입니다. 시간 범위를 넓혀 보시겠어요? (예: 최근 2시간)\n"
    return "예외별 건수(상위):\n" + "\n".join(
        json.dumps(row, ensure_ascii=False, default=str) for row in rows[:30]
    )


async def run_spl_async(settings: Settings, spl: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_blocking_spl, settings, spl)


async def search_errors(
    settings: Settings,
    *,
    env: str,
    sourcetype: str,
    earliest: str,
    latest: str,
) -> str:
    spl = _build_spl(env, sourcetype, earliest, latest)
    try:
        return await run_spl_async(settings, spl)
    except Exception as e:
        logger.exception("Splunk 조회 실패")
        return f"[Splunk 오류] {type(e).__name__}: {e}\n"
