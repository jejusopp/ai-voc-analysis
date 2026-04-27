from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import gradio as gr
from fastapi import Body, FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field

from app.config import ensure_data_dir, get_settings
from app.graph.workflow import build_graph

logger = logging.getLogger(__name__)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1)


class ChatOut(BaseModel):
    thread_id: str
    reply: str


def _last_ai_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            c = m.content
            if isinstance(c, str):
                return c
            return str(c)
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_data_dir(settings.checkpoint_db_path)
    path = settings.checkpoint_db_path.as_posix()
    async with AsyncSqliteSaver.from_conn_string(path) as checkpointer:
        app.state.graph = build_graph(checkpointer)
        yield


app = FastAPI(title="AI VOC 선제 분석", lifespan=lifespan)


def build_gradio_demo() -> gr.Blocks:
    with gr.Blocks(title="AI VOC 선제 분석 챗봇") as demo:
        gr.Markdown("## AI VOC 선제 분석 챗봇")
        chatbot = gr.Chatbot(height=520, label="대화")
        message = gr.Textbox(
            label="메시지",
            placeholder="VOC 내용을 입력하세요. (예: 편성 정보가 안 나와요)",
            lines=1,
        )
        send = gr.Button("전송", variant="primary")
        thread_id = gr.State("")
        gr.ClearButton([chatbot, message], value="대화 지우기")

        async def on_submit(
            user_text: str,
            history: list[dict[str, str]] | None,
            current_thread_id: str,
        ) -> tuple[str, list[dict[str, str]], str]:
            text = user_text.strip()
            if not text:
                return "", history or [], current_thread_id

            tid = current_thread_id or uuid.uuid4().hex
            out = await app.state.graph.ainvoke(
                {"messages": [HumanMessage(content=text)]},
                config={"configurable": {"thread_id": tid}},
            )
            reply = _last_ai_text(out.get("messages", [])) or "(응답 없음)"
            next_history = list(history or [])
            next_history.append({"role": "user", "content": text})
            next_history.append({"role": "assistant", "content": reply})
            return "", next_history, tid

        message.submit(on_submit, [message, chatbot, thread_id], [message, chatbot, thread_id])
        send.click(on_submit, [message, chatbot, thread_id], [message, chatbot, thread_id])

    return demo


@app.post("/chat/start", response_model=ChatOut)
async def chat_start(body: ChatIn) -> ChatOut:
    thread_id = uuid.uuid4().hex
    graph = app.state.graph
    cfg = {"configurable": {"thread_id": thread_id}}
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=body.message)]},
        config=cfg,
    )
    reply = _last_ai_text(out.get("messages", []))
    return ChatOut(thread_id=thread_id, reply=reply or "(응답 없음)")


@app.post("/chat/{thread_id}", response_model=ChatOut)
async def chat_turn(thread_id: str, body: ChatIn) -> ChatOut:
    graph = app.state.graph
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        out = await graph.ainvoke(
            {"messages": [HumanMessage(content=body.message)]},
            config=cfg,
        )
    except Exception as e:
        logger.exception("chat invoke 실패 thread_id=%s", thread_id)
        raise HTTPException(status_code=500, detail=str(e)) from e
    reply = _last_ai_text(out.get("messages", []))
    return ChatOut(thread_id=thread_id, reply=reply or "(응답 없음)")


@app.post("/webhook/teams")
async def teams_webhook(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, bool]:
    """Microsoft Teams 봇 연동 확장용 스텁."""
    _ = payload
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app = gr.mount_gradio_app(app, build_gradio_demo(), path="/gradio")
