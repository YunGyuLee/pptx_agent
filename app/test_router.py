"""
테스트 UI 라우터 — 제거 시 이 파일과 test_ui.html 삭제 후
agent_api.py에서 아래 2줄 제거:
  from test_router import router as test_router
  app.include_router(test_router)
"""

from __future__ import annotations

import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

_here = Path(__file__).parent
for _candidate in (_here.parent / "test_package", _here / "test_package"):
    if _candidate.exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

import agent_core
from events import EventBus, AgentEvent, EVENT_DONE
from test_scenarios import SCENARIOS

router = APIRouter(prefix="/test", tags=["test"])

# ── 참고 문서 분리 ────────────────────────────────────────────

_REF_PATTERNS = re.compile(
    r"^(taxonomy|doc_metadata|context|분류체계|메타데이터|이전대화)",
    re.IGNORECASE,
)

def _split_docs(docs: list[dict]) -> tuple[list[dict], list[dict]]:
    target, refs = [], []
    for d in docs:
        (refs if _REF_PATTERNS.match(d["name"]) else target).append(d)
    return target, refs


# ── 결과 저장소 ───────────────────────────────────────────────

_results: dict[str, dict] = {}
_lock = threading.Lock()


# ── 로그 수집 버스 ────────────────────────────────────────────

def _make_log_bus(key: str) -> EventBus:
    def _handler(event: AgentEvent) -> None:
        if event.type == EVENT_DONE:
            return
        icon = {"phase": "▶", "tool_call": "🔧", "reflect": "💭", "error": "❌"}.get(event.type, "•")
        with _lock:
            if key in _results:
                _results[key].setdefault("log", []).append(f"{icon} {event.message}")
    return EventBus(handler=_handler)


# ── 결과 직렬화 ───────────────────────────────────────────────

def _serialize(key: str) -> dict:
    with _lock:
        r = _results.get(key)
    if not r:
        return {"status": "idle"}
    out: dict = {"status": r["status"], "elapsed": r.get("elapsed"), "log": r.get("log") or []}
    if r["status"] == "done":
        out["result"] = r["result"]
    elif r["status"] == "error":
        out["error"] = r.get("error", "")
    return out


# ── 실행 함수 ─────────────────────────────────────────────────

def _default_cmd(task: str) -> str:
    return {
        "classify":  "문서를 분류하라.",
        "qna":       "문서 내용을 설명하라.",
        "summarize": "문서를 요약하라.",
        "translate": "문서를 번역하라.",
    }.get(task, "문서를 분석하라.")


def _exec_scenario(scenario: dict, key: str) -> None:
    with _lock:
        _results[key] = {"status": "running", "started_at": time.time(), "log": []}
    try:
        _cmd_val = scenario.get("command")
        command = (_cmd_val() if callable(_cmd_val) else _cmd_val) or ""
        use_critic = scenario.get("use_critic", False)
        task = scenario.get("task", "qna")
        docs = scenario["documents"]()

        if scenario.get("batch"):
            outputs = []
            for i, doc_group in enumerate(docs):
                doc_names = [d["name"] for d in doc_group]
                ref_names = [d["name"] for d in doc_group if _REF_PATTERNS.match(d["name"])]
                with _lock:
                    _results[key].setdefault("log", []).append(f"▶ [{i+1}/{len(docs)}] {doc_names[0]}")
                result = agent_core.run(
                    command=command or _default_cmd(task),
                    documents=doc_group,
                    use_critic=use_critic,
                    task=task,
                    bus=_make_log_bus(key),
                    reference_names=ref_names or None,
                )
                outputs.append({"doc": doc_names[0] if doc_names else f"batch_{i}", "result": result})
            final = outputs
        else:
            final = agent_core.run(
                command=command or _default_cmd(task),
                documents=docs,
                use_critic=use_critic,
                task=task,
                bus=_make_log_bus(key),
            )

        with _lock:
            if key in _results:
                started = _results[key].get("started_at", time.time())
                _results[key].update({"status": "done", "result": final,
                                       "elapsed": round(time.time() - started, 1)})
    except Exception:
        with _lock:
            if key in _results:
                started = _results[key].get("started_at", time.time())
                _results[key].update({"status": "error", "error": traceback.format_exc(),
                                       "elapsed": round(time.time() - started, 1)})


def _exec_free(key: str, task: str, command: str, docs: list[dict]) -> None:
    with _lock:
        _results[key] = {"status": "running", "started_at": time.time(), "log": []}
    try:
        target_docs, ref_docs = _split_docs(docs)
        ref_names = [d["name"] for d in ref_docs]

        if task == "classify" and len(target_docs) > 1:
            total = len(target_docs)
            outputs = []
            for i, doc in enumerate(target_docs):
                with _lock:
                    _results[key].setdefault("log", []).append(f"▶ [{i+1}/{total}] {doc['name']}")
                result = agent_core.run(
                    command=command,
                    documents=[doc] + ref_docs,
                    use_critic=True,
                    task=task,
                    bus=_make_log_bus(key),
                    reference_names=ref_names or None,
                )
                outputs.append({"doc": doc["name"], "result": result})
            final = outputs
        else:
            final = agent_core.run(
                command=command,
                documents=docs,
                use_critic=(task == "classify"),
                task=task,
                bus=_make_log_bus(key),
                reference_names=ref_names or None,
            )

        with _lock:
            if key in _results:
                started = _results[key].get("started_at", time.time())
                _results[key].update({"status": "done", "result": final,
                                       "elapsed": round(time.time() - started, 1)})
    except Exception:
        with _lock:
            if key in _results:
                started = _results[key].get("started_at", time.time())
                _results[key].update({"status": "error", "error": traceback.format_exc(),
                                       "elapsed": round(time.time() - started, 1)})


# ── API 엔드포인트 ────────────────────────────────────────────

@router.get("")
def test_ui():
    return FileResponse(str(Path(__file__).parent / "test_ui.html"), media_type="text/html")


@router.get("/scenarios")
def list_scenarios():
    return [{"id": s["id"], "label": s["label"], "group": s["group"], "desc": s["desc"]}
            for s in SCENARIOS]


@router.post("/run/{sid}")
def run_scenario(sid: str):
    scenario = next((s for s in SCENARIOS if s["id"] == sid), None)
    if not scenario:
        return JSONResponse({"error": "not found"}, status_code=404)
    threading.Thread(target=_exec_scenario, args=(scenario, sid), daemon=True).start()
    return {"status": "started"}


@router.post("/run_group/{gid}")
def run_group(gid: str):
    group = [s for s in SCENARIOS if s["group"] == gid]
    for s in group:
        threading.Thread(target=_exec_scenario, args=(s, s["id"]), daemon=True).start()
    return [s["id"] for s in group]


@router.post("/run_all")
def run_all():
    for s in SCENARIOS:
        threading.Thread(target=_exec_scenario, args=(s, s["id"]), daemon=True).start()
    return [s["id"] for s in SCENARIOS]


@router.post("/clear")
def clear_results():
    with _lock:
        _results.clear()
    return {"status": "cleared"}


@router.get("/result/{sid}")
def get_result(sid: str):
    return _serialize(sid)


# ── 자유 테스트 ───────────────────────────────────────────────

@router.post("/free_run")
async def free_run(
    task: str = Form(...),
    query: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    taxonomy: str = Form(""),
    metadata: str = Form(""),
):
    docs = []
    for f in files:
        content = (await f.read()).decode("utf-8", errors="replace")
        docs.append({"name": Path(f.filename).stem, "content": content})
    if taxonomy.strip():
        docs.append({"name": "taxonomy_분류체계", "content": taxonomy})
    if metadata.strip():
        docs.append({"name": "doc_metadata", "content": metadata})
    if not docs:
        return JSONResponse({"error": "문서를 1개 이상 업로드하세요."}, status_code=400)

    cmd_map = {"classify": "문서를 분류하라.", "summarize": "문서를 요약하라.", "translate": "문서를 번역하라."}
    command = query if task == "qna" else (query or cmd_map.get(task, "문서를 분석하라."))

    key = str(uuid.uuid4())  # 요청별 고유 키 — 동시 요청 간 결과 격리
    threading.Thread(target=_exec_free, args=(key, task, command, docs), daemon=True).start()
    return {"job_id": key}


@router.get("/free_result/{job_id}")
def free_result(job_id: str):
    return _serialize(job_id)


# ── 샘플 문서 ────────────────────────────────────────────────

def _docs_dir() -> Path | None:
    h = Path(__file__).parent
    for candidate in (h.parent / "test_package" / "docs", h / "test_package" / "docs"):
        if candidate.exists():
            return candidate
    return None


@router.get("/sample_docs")
def sample_docs():
    d = _docs_dir()
    if not d:
        return []
    return [{"name": f.stem, "filename": f.name}
            for f in sorted(d.glob("*.md"))
            if f.name not in ("taxonomy_분류체계.md", "context_이전대화.md")]


@router.get("/sample_doc/{filename}")
def get_sample_doc(filename: str):
    d = _docs_dir()
    if not d:
        return JSONResponse({"error": "docs 폴더 없음"}, status_code=404)
    path = (d / filename).resolve()
    if not str(path).startswith(str(d.resolve())) or path.suffix != ".md" or not path.exists():
        return JSONResponse({"error": "파일 없음"}, status_code=404)
    return {"name": path.stem, "content": path.read_text(encoding="utf-8")}


@router.get("/taxonomy")
def get_taxonomy():
    d = _docs_dir()
    if d:
        f = d / "taxonomy_분류체계.md"
        if f.exists():
            return {"content": f.read_text(encoding="utf-8")}
    return {"content": ""}
