"""
Qwen Agent API 서버
실행: python3 agent_api.py
포트: 8100 (agent studio mini와 분리)

엔드포인트:
  POST /api/v1/agent/run         — 완료 후 결과 반환 (agent_studio_mini 호환)
  POST /api/v1/agent/run/stream  — SSE: 진행 이벤트 실시간 스트리밍 후 결과 반환
  GET  /api/v1/health
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import re

import agent_core
import prompt_manager
from config import API_HOST, API_PORT, _env_int, get_allowed_tools
from events import EventBus, AgentEvent, EVENT_DONE, EVENT_ERROR

_REF_PATTERN = re.compile(
    r"^(taxonomy|doc_metadata|context|분류체계|메타데이터|이전대화)",
    re.IGNORECASE,
)

def _ref_names(docs: list[dict]) -> list[str] | None:
    names = [d["name"] for d in docs if _REF_PATTERN.match(d["name"])]
    return names or None
from test_router import router as test_router  # 제거 시 이 줄 + 아래 include_router 줄 삭제
from pptx_api import router as pptx_router

AGENT_TIMEOUT: int = _env_int("AGENT_TIMEOUT", 300)  # 초

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Qwen Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(test_router)  # 제거 시 이 줄 + 위 import 줄 삭제
app.include_router(pptx_router)


# ── 요청/응답 스키마 ────────────────────────────────────────────

class Document(BaseModel):
    name: str
    content: str


class AgentRunRequest(BaseModel):
    task: str                           # classify | qna | summarize | translate
    documents: list[Document] = []
    command: str | None = None          # None이면 task별 기본 템플릿 사용
    context_summary: str | None = None  # 연속 세션용 이전 대화 요약
    target_lang: str = "한국어"          # translate 전용
    question: str | None = None         # qna 전용
    allowed_tools: list[str] | None = None  # None=태스크 기본값, 명시 시 override


class AgentRunResponse(BaseModel):
    task: str
    result: dict | str  # classify는 dict, 나머지는 str


SUPPORTED_TASKS = {"classify", "qna", "summarize", "translate"}


# ── 공통 유틸 ────────────────────────────────────────────────────

def _build_command(req: AgentRunRequest) -> str:
    if req.task == "classify":
        return prompt_manager.get("classify")
    if req.task == "qna":
        return prompt_manager.get("qna", question=req.question or "전달된 문서의 주요 내용을 설명해줘.")
    if req.task == "summarize":
        return prompt_manager.get("summarize", length="3~5문단", format="항목별 정리")
    if req.task == "translate":
        return prompt_manager.get("translate", target_lang=req.target_lang)
    return ""


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _normalize_result(task: str, raw: dict | str) -> dict | str:
    """classify 결과를 agent_studio_mini 스키마로 정규화."""
    if task != "classify" or not isinstance(raw, dict):
        return raw
    return {
        "node_path":             raw.get("node_path", "미분류"),
        "summary":               raw.get("summary", ""),
        "keywords":              raw.get("keywords", []),
        "sample_questions":      raw.get("sample_questions", []),
        "classification_reason": raw.get("classification_reason", ""),
        "confidence_score":      _safe_float(raw.get("confidence_score", 0.0)),
    }


def _validate(req: AgentRunRequest):
    if req.task not in SUPPORTED_TASKS:
        raise HTTPException(400, f"지원하지 않는 task: {req.task}. 가능: {sorted(SUPPORTED_TASKS)}")
    if not req.documents:
        raise HTTPException(400, "documents가 비어있습니다.")


# ── 헬스체크 ─────────────────────────────────────────────────────

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "service": "qwen-agent-api"}


# ── 동기 엔드포인트 (agent_studio_mini 호환) ──────────────────────

@app.post("/api/v1/agent/run", response_model=AgentRunResponse)
def agent_run(req: AgentRunRequest):
    _validate(req)
    command = req.command or _build_command(req)
    docs = [{"name": d.name, "content": d.content} for d in req.documents]
    if req.context_summary:
        docs.append({"name": "context_이전대화", "content": f"# 이전 대화 요약\n\n{req.context_summary}"})
    logger.info(f"[{req.task}] 문서 {len(docs)}개")

    tools = get_allowed_tools(req.task, req.allowed_tools)
    logger.info(f"[{req.task}] 허용 툴: {tools}")
    try:
        raw = agent_core.run(
            command=command,
            documents=docs,
            use_critic=(req.task == "classify"),
            task=req.task,
            allowed_tools=tools,
            reference_names=_ref_names(docs),
        )
    except Exception as e:
        logger.error(f"[{req.task}] 오류: {e}")
        raise HTTPException(500, str(e))

    result = _normalize_result(req.task, raw)
    logger.info(f"[{req.task}] 완료")
    return AgentRunResponse(task=req.task, result=result)


# ── SSE 스트리밍 엔드포인트 ──────────────────────────────────────

@app.post("/api/v1/agent/run/stream")
async def agent_run_stream(req: AgentRunRequest):
    """
    진행 이벤트를 SSE로 실시간 전송.
    각 이벤트: data: {"type": "...", "message": "...", "meta": {...}}\n\n
    마지막:    data: {"type": "result", "result": <최종 결과>}\n\n

    클라이언트 예시 (JavaScript):
        const resp = await fetch('/api/v1/agent/run/stream', {method:'POST', body: JSON.stringify(...)})
        const reader = resp.body.getReader()
    """
    _validate(req)
    command = req.command or _build_command(req)
    docs = [{"name": d.name, "content": d.content} for d in req.documents]
    if req.context_summary:
        docs.append({"name": "context_이전대화", "content": f"# 이전 대화 요약\n\n{req.context_summary}"})

    tools = get_allowed_tools(req.task, req.allowed_tools)
    logger.info(f"[SSE/{req.task}] 허용 툴: {tools}")

    # asyncio.Queue를 사용 — agent 스레드 → 이벤트루프 단방향 채널.
    # threading.Queue + run_in_executor(q.get) 방식은 요청마다 스레드풀 슬롯을
    # 블로킹 점유해 고동시성에서 스레드풀이 소진될 수 있음.
    loop = asyncio.get_running_loop()
    aio_q: asyncio.Queue = asyncio.Queue()
    result_holder: dict = {}

    def _make_sse_bus() -> EventBus:
        def _handler(event: AgentEvent) -> None:
            loop.call_soon_threadsafe(aio_q.put_nowait, event)
        return EventBus(handler=_handler)

    def run_agent():
        try:
            raw = agent_core.run(
                command=command,
                documents=docs,
                use_critic=(req.task == "classify"),
                bus=_make_sse_bus(),
                task=req.task,
                allowed_tools=tools,
                reference_names=_ref_names(docs),
            )
            result_holder["result"] = _normalize_result(req.task, raw)
        except Exception as e:
            logger.error(f"[SSE/{req.task}] 에이전트 오류: {e}")
            result_holder["error"] = str(e)
        finally:
            loop.call_soon_threadsafe(aio_q.put_nowait, None)  # sentinel

    # run_agent는 스레드풀 슬롯 1개만 사용. generate()는 await로 비동기 대기.
    loop.run_in_executor(None, run_agent)

    async def generate():
        while True:
            try:
                event = await asyncio.wait_for(aio_q.get(), timeout=AGENT_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if event is None:
                break
            payload = {"type": event.type, "message": event.message}
            if event.meta:
                payload["meta"] = event.meta
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if "result" in result_holder:
            yield f"data: {json.dumps({'type': 'result', 'result': result_holder['result']}, ensure_ascii=False)}\n\n"
        elif "error" in result_holder:
            yield f"data: {json.dumps({'type': 'error', 'message': result_holder['error']}, ensure_ascii=False)}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': f'타임아웃 ({AGENT_TIMEOUT}초)'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
    logger.info(f"테스트 UI: http://localhost:{API_PORT}/test")
    uvicorn.run("agent_api:app", host=API_HOST, port=API_PORT, reload=False)
