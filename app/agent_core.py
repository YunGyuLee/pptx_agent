"""
Qwen Agent Core — Planner → Worker/Direct → (Critic)

흐름:
  Planner: 문서 메타데이터(이름+토큰수) + 태스크 → mode 결정
    ├─ "direct": 모든 문서 한번에 주입 → 단발 LLM 호출 → (Critic)
    └─ "tool":   list_docs → read_doc → Reflector → 결과 초안 → (Critic)

  Planner 실패 시 폴백: DIRECT_INJECT_TOKEN_LIMIT 임계값으로 자동 분기
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Union

from qwen_agent.agents import FnCallAgent, Assistant
from qwen_agent.llm.schema import FUNCTION, ASSISTANT as ASSISTANT_ROLE
from qwen_agent.tools.base import BaseTool, register_tool
from qwen_agent.utils.tokenization_qwen import count_tokens

from config import (
    LLM_CONFIG,
    DIRECT_INJECT_TOKEN_LIMIT,
    WORKER_MAX_STEPS,
    REFLECT_INTERVAL,
    REFLECT_CONTEXT_CHARS,
    SEARCH_MAX_LINES,
    CRITIC_REF_CHARS,
    AGENT_MAX_CONCURRENCY,
    CLASSIFY_MODE,
    CLASSIFY_MAX_STEPS,
    DIRECT_CHUNK_TOKENS,
    get_allowed_tools,
)

# AGENT_MAX_CONCURRENCY 값으로 초기화 — 설정 변경은 재시작 필요
_run_semaphore = threading.Semaphore(AGENT_MAX_CONCURRENCY)
from events import EventBus, cli_handler, EVENT_PHASE, EVENT_TOOL, EVENT_REFLECT, EVENT_THINK


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════

def _extract_text(response: list) -> str:
    """
    agent.run() yield 결과에서 최종 텍스트 추출.
    - content가 str이면 그대로
    - content가 list[ContentItem]이면 text 필드 합산
    - Qwen3 thinking mode: content 비어있으면 reasoning_content fallback
    """
    if not response:
        return ""
    msg = response[-1]
    content = msg.content if hasattr(msg, "content") else msg.get("content", "")

    # list[ContentItem] 처리
    if isinstance(content, list):
        parts = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(item.text or "")
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        content = "".join(parts)

    # Qwen3 thinking mode: content가 비면 reasoning_content 시도
    if not content or not content.strip():
        rc = getattr(msg, "reasoning_content", None) or (msg.get("reasoning_content") if isinstance(msg, dict) else None)
        if rc:
            if isinstance(rc, list):
                content = "".join(getattr(i, "text", "") or (i.get("text", "") if isinstance(i, dict) else "") for i in rc)
            else:
                content = str(rc)

    return content.strip() if isinstance(content, str) else ""


def _strip_fence(text: str) -> str:
    """
    ```json ... ``` 또는 ``` ... ``` 코드 펜스 제거.
    lstrip/rstrip은 문자 집합 제거라 버그 — 명시적 slice 사용.
    """
    text = text.strip()
    # 시작 펜스 제거
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    # 끝 펜스 제거
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _parse_json(raw: str) -> dict | str:
    text = _strip_fence(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # prose에 묻힌 JSON 객체 추출 (LLM이 설명 텍스트와 함께 반환할 때)
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return raw


# ══════════════════════════════════════════════════════════════
# DocStore
# ══════════════════════════════════════════════════════════════

class DocStore:
    def __init__(self, documents: list[dict]):
        if not documents:
            raise ValueError("documents가 비어있습니다.")
        missing = [i for i, d in enumerate(documents) if "name" not in d or "content" not in d]
        if missing:
            raise ValueError(f"documents[{missing}]에 'name' 또는 'content' 키 누락")
        self._docs = {d["name"]: d["content"] for d in documents}

    def list(self) -> list[str]:
        return list(self._docs.keys())

    def read(self, name: str) -> str:
        if name not in self._docs:
            return f"[오류] '{name}' 없음. 사용 가능: {', '.join(self._docs)}"
        return self._docs[name]

    def search(self, name: str, query: str) -> str:
        content = self.read(name)
        if content.startswith("[오류]"):
            return content
        lines = content.split("\n")
        matched = [l for l in lines if any(k.lower() in l.lower() for k in query.split())]
        return f"[{name}] 검색 결과:\n" + ("\n".join(matched[:SEARCH_MAX_LINES]) if matched else "관련 내용 없음")

    def total_tokens(self) -> int:
        return sum(count_tokens(c) for c in self._docs.values())

    def all_content(self) -> str:
        return "\n\n---\n\n".join(f"### {n}\n{c}" for n, c in self._docs.items())

    def metadata(self) -> list[dict]:
        """이름 + 토큰 수만 담은 경량 메타데이터. Planner에 전달용."""
        return [
            {"name": n, "tokens": count_tokens(c)}
            for n, c in self._docs.items()
        ]


# ══════════════════════════════════════════════════════════════
# Tools
# ══════════════════════════════════════════════════════════════

# 요청별 DocStore를 thread-local로 격리 (동시 API 호출 안전)
_thread_local = threading.local()

def _get_store() -> "DocStore | None":
    return getattr(_thread_local, "store", None)

def _set_store(store: "DocStore") -> None:
    _thread_local.store = store

_TOOL_MESSAGES = {
    "list_docs":        "문서 목록 파악 중...",
    "read_doc":         "문서 읽는 중: {name}",
    "search_doc":       "[{name}] '{query}' 검색 중...",
    "find_docs":        "키워드로 문서 필터링 중: {query}",
    "get_doc_metadata": "문서 메타데이터 조회 중...",
    "extract_section":  "[{name}] '{section_hint}' 섹션 추출 중...",
    "chunk_doc":        "[{name}] 청크 {chunk_index} 읽는 중...",
}


@register_tool("list_docs")
class ListDocsTool(BaseTool):
    description = "세션에 등록된 문서 목록 반환. 탐색 시작 시 가장 먼저 호출."
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: Union[str, dict], **kwargs) -> str:
        if _get_store() is None:
            return "[오류] DocStore 미초기화"
        docs = _get_store().list()
        return json.dumps({"documents": docs, "count": len(docs)}, ensure_ascii=False)


@register_tool("read_doc")
class ReadDocTool(BaseTool):
    description = "지정 문서 전체 내용 반환. list_docs로 이름 확인 후 호출."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "읽을 문서 이름 (list_docs 결과와 정확히 일치)"}
        },
        "required": ["name"],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return "[오류] params JSON 파싱 실패"
        if not isinstance(params, dict):
            return "[오류] params가 dict가 아님"
        if _get_store() is None:
            return "[오류] DocStore 미초기화"
        name = params.get("name", "")
        if not name:
            return "[오류] 'name' 파라미터 누락"
        return f"[문서: {name}]\n{_get_store().read(name)}"


@register_tool("search_doc")
class SearchDocTool(BaseTool):
    description = "특정 문서에서 키워드 검색. 문서가 길 때 필요한 부분만 빠르게 탐색."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "검색할 문서 이름"},
            "query": {"type": "string", "description": "검색 키워드 (공백 구분 OR 검색)"},
        },
        "required": ["name", "query"],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return "[오류] params JSON 파싱 실패"
        if not isinstance(params, dict):
            return "[오류] params가 dict가 아님"
        if _get_store() is None:
            return "[오류] DocStore 미초기화"
        return _get_store().search(params.get("name", ""), params.get("query", ""))


@register_tool("find_docs")
class FindDocsTool(BaseTool):
    description = "키워드로 관련 문서를 필터링. 문서가 많을 때 read_doc 대상을 좁히는 용도."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색 키워드 (공백 구분 OR 검색)"},
        },
        "required": ["query"],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return "[오류] params JSON 파싱 실패"
        if not isinstance(params, dict):
            return "[오류] params가 dict가 아님"
        store = _get_store()
        if store is None:
            return "[오류] DocStore 미초기화"
        query = params.get("query", "")
        if not query:
            return "[오류] 'query' 파라미터 누락"
        keywords = [k.lower() for k in query.split()]
        results = []
        for name in store.list():
            content = store.read(name).lower()
            hits = [k for k in keywords if k in content]
            if hits:
                # 첫 번째 히트 라인 발췌
                first_line = next(
                    (l.strip() for l in store.read(name).split("\n") if any(k in l.lower() for k in hits)),
                    ""
                )
                results.append({"name": name, "matched_keywords": hits, "preview": first_line[:100]})
        if not results:
            return json.dumps({"matched": [], "count": 0}, ensure_ascii=False)
        return json.dumps({"matched": results, "count": len(results)}, ensure_ascii=False)


@register_tool("get_doc_metadata")
class GetDocMetadataTool(BaseTool):
    description = "문서 등록 시 입력된 메타데이터(키워드, 분류 등) 조회. 'doc_metadata' 문서가 세션에 있을 때 사용."
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: Union[str, dict], **kwargs) -> str:
        store = _get_store()
        if store is None:
            return "[오류] DocStore 미초기화"
        # doc_metadata 문서가 넘어온 경우 반환
        for candidate in ("doc_metadata", "문서메타데이터", "metadata"):
            if candidate in store.list():
                return store.read(candidate)
        return "[정보] 메타데이터 문서 없음 (doc_metadata 미전달)"


@register_tool("extract_section")
class ExtractSectionTool(BaseTool):
    description = "문서에서 특정 조항·섹션을 추출. 헤더(#, 제N조) 기준으로 해당 블록 전체 반환."
    parameters = {
        "type": "object",
        "properties": {
            "name":         {"type": "string", "description": "문서 이름"},
            "section_hint": {"type": "string", "description": "찾을 섹션 힌트 (예: '제12조', '담보인정비율', '## 평가기준')"},
        },
        "required": ["name", "section_hint"],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        import re
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return "[오류] params JSON 파싱 실패"
        if not isinstance(params, dict):
            return "[오류] params가 dict가 아님"
        store = _get_store()
        if store is None:
            return "[오류] DocStore 미초기화"
        name = params.get("name", "")
        hint = params.get("section_hint", "")
        content = store.read(name)
        if content.startswith("[오류]"):
            return content
        lines = content.split("\n")
        # 섹션 시작 인덱스 탐색
        start_idx = None
        for i, line in enumerate(lines):
            if hint.lower() in line.lower():
                start_idx = i
                break
        if start_idx is None:
            return f"[{name}] '{hint}' 섹션을 찾지 못했습니다."
        # 다음 동급 이상 헤더까지 추출
        header_pattern = re.compile(r"^#{1,3} |^제\d+조")
        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if header_pattern.match(lines[i]) and i > start_idx:
                end_idx = i
                break
        section = "\n".join(lines[start_idx:end_idx]).strip()
        return f"[{name}] '{hint}' 섹션:\n\n{section}"


@register_tool("chunk_doc")
class ChunkDocTool(BaseTool):
    description = "긴 문서를 청크 단위로 나눠 반환. read_doc으로 토큰 초과 시 사용."
    parameters = {
        "type": "object",
        "properties": {
            "name":        {"type": "string", "description": "문서 이름"},
            "chunk_index": {"type": "integer", "description": "청크 번호 (0부터 시작)"},
            "chunk_size":  {"type": "integer", "description": "청크당 글자 수 (기본 3000)"},
        },
        "required": ["name", "chunk_index"],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return "[오류] params JSON 파싱 실패"
        if not isinstance(params, dict):
            return "[오류] params가 dict가 아님"
        store = _get_store()
        if store is None:
            return "[오류] DocStore 미초기화"
        name = params.get("name", "")
        try:
            chunk_index = int(float(params.get("chunk_index", 0)))
            chunk_size  = int(float(params.get("chunk_size", 3000)))
        except (TypeError, ValueError):
            return "[오류] chunk_index / chunk_size 값이 올바르지 않습니다."
        content = store.read(name)
        if content.startswith("[오류]"):
            return content
        total_chunks = max(1, -(-len(content) // chunk_size))  # ceiling division
        if chunk_index < 0 or chunk_index >= total_chunks:
            return f"[오류] chunk_index {chunk_index} 범위 초과. 총 청크 수: {total_chunks}"
        start = chunk_index * chunk_size
        chunk = content[start:start + chunk_size]
        return (
            f"[{name}] 청크 {chunk_index + 1}/{total_chunks} "
            f"(글자 {start}~{start + len(chunk)}):\n\n{chunk}"
        )


# ══════════════════════════════════════════════════════════════
# Planner
# ══════════════════════════════════════════════════════════════

_PLANNER_SYSTEM = """당신은 문서 처리 전략 플래너입니다.
태스크 설명과 문서 목록(이름, 토큰 수)을 보고 최적 처리 전략을 결정하세요.

전략 기준:
- "direct": 문서를 모두 한번에 컨텍스트에 주입해 처리
  적합한 경우: 총 토큰이 적음 / 단순 요약·번역 / 모든 문서를 동시에 비교해야 할 때
- "tool": 필요한 문서를 선택적으로 읽으며 처리
  적합한 경우: 총 토큰이 많음 / 분류처럼 특정 문서(taxonomy 등)를 먼저 읽어야 할 때 / 탐색적 분석

반드시 아래 JSON만 반환하세요:
{
  "mode": "direct" | "tool",
  "reason": "판단 이유 (한 문장)",
  "priority_docs": ["먼저 읽어야 할 문서명", ...]
}
priority_docs는 tool 모드일 때만 의미 있음. direct 모드면 빈 배열."""


def _run_planner(store: DocStore, command: str, bus: EventBus) -> dict:
    """
    문서 메타데이터(내용 없음)만 보고 처리 전략 결정.
    실패 시 None 반환 → 호출자가 토큰 임계값 폴백 처리.
    """
    bus.phase("Planner: 처리 전략 수립 중...")

    meta = store.metadata()
    total_tokens = sum(m["tokens"] for m in meta)

    prompt = (
        f"## 태스크\n{command[:1500]}\n\n"  # command 앞부분 — JSON 스키마 포함 여부 감안해 1500자
        f"## 문서 목록\n"
        + "\n".join(f"- {m['name']} ({m['tokens']:,} 토큰)" for m in meta)
        + f"\n\n총 토큰: {total_tokens:,}"
    )

    agent = Assistant(llm=LLM_CONFIG, system_message=_PLANNER_SYSTEM)
    final = ""
    for response in agent.run([{"role": "user", "content": prompt}]):
        t = _extract_text(response)
        if t:
            final = t

    try:
        result = json.loads(_strip_fence(final))
        mode = result.get("mode", "")
        if mode not in ("direct", "tool"):
            raise ValueError(f"mode 값 이상: {mode}")
        bus.reflect(f"전략: {mode.upper()} — {result.get('reason', '')}")
        return result
    except Exception as e:
        bus.reflect(f"Planner 파싱 실패({e}) → 토큰 임계값 폴백")
        return {}


# ══════════════════════════════════════════════════════════════
# 경로 A — 직접 주입 (소형 문서)
# ══════════════════════════════════════════════════════════════

_DIRECT_SYSTEM = """당신은 문서 분석 전문가입니다.
전달된 모든 문서를 읽고 지시를 수행하세요.
결과는 지시된 형식(JSON 또는 텍스트)으로만 반환하세요."""

def _run_direct(store: DocStore, command: str, bus: EventBus) -> str:
    _set_store(store)  # tool 경로와 동일하게 thread-local 갱신
    bus.phase("직접 주입 경로 (소형 문서)")
    bus.tool("모든 문서를 컨텍스트에 주입 중...")

    full_context = f"## 수행할 태스크\n{command}\n\n## 문서 목록\n{store.all_content()}"
    agent = Assistant(llm=LLM_CONFIG, system_message=_DIRECT_SYSTEM)

    final = ""
    for response in agent.run([{"role": "user", "content": full_context}]):
        t = _extract_text(response)
        if t:
            final = t
    return final


# ══════════════════════════════════════════════════════════════
# 경로 A-2 — Direct 청크 처리 (summarize / translate 대형 문서)
# ══════════════════════════════════════════════════════════════

def _split_content_into_chunks(content: str, chunk_tokens: int) -> list[str]:
    """토큰 기준으로 텍스트를 청크로 분할. 단락 경계 우선."""
    if count_tokens(content) <= chunk_tokens:
        return [content]

    # 단락 단위로 쪼개서 청크 합산
    paragraphs = content.split("\n\n")
    chunks, current, current_tokens = [], [], 0
    for para in paragraphs:
        para_tokens = count_tokens(para)
        if current_tokens + para_tokens > chunk_tokens and current:
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        # 단락 자체가 chunk_tokens 초과면 강제 분할
        if para_tokens > chunk_tokens:
            words = para.split()
            sub, sub_tokens = [], 0
            for w in words:
                wt = count_tokens(w)
                if sub_tokens + wt > chunk_tokens and sub:
                    chunks.append(" ".join(sub))
                    sub, sub_tokens = [], 0
                sub.append(w)
                sub_tokens += wt
            if sub:
                current = [" ".join(sub)]
                current_tokens = sub_tokens
        else:
            current.append(para)
            current_tokens += para_tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _run_direct_chunked(store: DocStore, command: str, task: str, bus: EventBus) -> str:
    """
    문서 전체 토큰이 DIRECT_INJECT_TOKEN_LIMIT 초과 시 청크 단위로 순차 처리.
    - translate: 청크별 번역 후 이어 붙이기
    - summarize: 청크별 요약 후 재요약 (계층 요약)
    """
    all_content = store.all_content()
    total_tokens = count_tokens(all_content)

    # 청크 분할
    chunks = _split_content_into_chunks(all_content, DIRECT_CHUNK_TOKENS)
    total = len(chunks)
    bus.phase(f"청크 분할 처리: 총 {total}개 청크 ({total_tokens:,} 토큰)")

    agent = Assistant(llm=LLM_CONFIG, system_message=_DIRECT_SYSTEM)
    chunk_results = []

    for i, chunk in enumerate(chunks, 1):
        bus.phase(f"청크 {i}/{total} 처리 중...")
        if task == "translate":
            chunk_command = f"{command}\n\n[참고: 전체 문서의 {i}/{total} 부분입니다. 이 부분만 번역하세요.]"
        else:  # summarize
            chunk_command = f"{command}\n\n[참고: 전체 문서의 {i}/{total} 부분입니다. 이 부분의 핵심 내용을 요약하세요.]"

        ctx = f"## 수행할 태스크\n{chunk_command}\n\n## 문서 내용\n{chunk}"
        chunk_result = ""
        for response in agent.run([{"role": "user", "content": ctx}]):
            t = _extract_text(response)
            if t:
                chunk_result = t
        chunk_results.append(chunk_result)

    if total == 1:
        return chunk_results[0]

    # 청크 결과 합산
    if task == "translate":
        # 번역은 순서대로 이어 붙이기
        return "\n\n".join(chunk_results)
    else:
        # 요약은 청크 요약들을 재요약
        bus.phase("청크 요약 통합 중...")
        combined = "\n\n---\n\n".join(
            f"[파트 {i+1}]\n{r}" for i, r in enumerate(chunk_results)
        )
        merge_command = f"{command}\n\n아래는 문서를 {total}개 부분으로 나눠 요약한 결과입니다. 전체를 하나의 일관된 요약으로 통합하세요."
        ctx = f"## 수행할 태스크\n{merge_command}\n\n## 파트별 요약\n{combined}"
        final = ""
        for response in agent.run([{"role": "user", "content": ctx}]):
            t = _extract_text(response)
            if t:
                final = t
        return final


# ══════════════════════════════════════════════════════════════
# 경로 B — Tool Calling (대형 문서)
# ══════════════════════════════════════════════════════════════

_WORKER_SYSTEM = """당신은 문서 분석 전문가입니다. Plan-Act-Reflect 루프로 작업하세요.

[Plan] 먼저 list_docs()로 전체 문서 파악
[Act]  taxonomy/분류체계 문서를 먼저 read_doc(), 이후 대상 문서 read_doc()
[Reflect] 읽은 내용이 태스크를 완수하기에 충분한가?
  - 충분 → 결과 JSON 반환
  - 부족 → 추가 read_doc() 또는 search_doc() 후 재반성

결과 JSON 스키마 (분류 태스크 기준):
{
  "node_path": "대분류/소분류",
  "summary": "한 문단 요약",
  "keywords": ["키워드1", ...],
  "sample_questions": ["질문1", ...],
  "classification_reason": "taxonomy 어느 항목에 해당하는지 명시",
  "confidence_score": 0.0~1.0,
  "referenced_docs": ["참조 문서명", ...]
}"""

_SYNTHESIS_SYSTEM = (
    "당신은 문서 분석 전문가입니다. 제공된 문서 내용을 바탕으로 태스크를 수행하고 "
    "결과를 JSON으로 반환하세요. (synthesis 단계)"
)

_SYNTHESIS_CLASSIFY_SYSTEM = """\
당신은 문서 분류 전문가입니다. 제공된 문서 내용과 taxonomy를 바탕으로 문서를 분류하고
반드시 아래 JSON 스키마로만 반환하세요. 다른 텍스트는 절대 포함하지 마세요.

{
  "node_path": "대분류/소분류/세부분류",
  "summary": "문서 전체 내용 한 문단 요약",
  "keywords": ["핵심키워드1", "핵심키워드2"],
  "sample_questions": ["이 문서로 답할 수 있는 질문1", "질문2"],
  "classification_reason": "이 분류를 선택한 이유",
  "confidence_score": 0.0
}

규칙: keywords 최대 15개, sample_questions 최대 10개, confidence_score 0.0~1.0"""

_REFLECTOR_SYSTEM = """당신은 정보 충분성 판단 전문가입니다.

Worker가 지금까지 수집한 정보가 태스크를 완수하기에 충분한지 판단하세요.

반드시 아래 JSON만 반환하세요:
{
  "sufficient": true/false,
  "reason": "판단 이유",
  "need_more": ["추가로 읽어야 할 문서명 또는 검색 키워드"]
}"""


def _run_reflector(store: DocStore, command: str, collected_so_far: str, bus: EventBus) -> dict:
    bus.phase("Reflector: 정보 충분성 판단 중...")

    agent = Assistant(llm=LLM_CONFIG, system_message=_REFLECTOR_SYSTEM)
    prompt = (
        f"## 태스크\n{command}\n\n"
        f"## 사용 가능한 문서 목록\n{json.dumps(store.list(), ensure_ascii=False)}\n\n"
        f"## 지금까지 수집한 내용\n{collected_so_far[:REFLECT_CONTEXT_CHARS]}"
    )

    final = ""
    for response in agent.run([{"role": "user", "content": prompt}]):
        t = _extract_text(response)
        if t:
            final = t

    try:
        return json.loads(_strip_fence(final))
    except Exception:
        return {"sufficient": True, "reason": "파싱 실패 — 진행", "need_more": []}


def _extract_tool_args(response: list) -> dict:
    """마지막 ASSISTANT 메시지의 function_call.arguments 파싱.
    qwen_agent Message 스키마: tool args는 message.function_call.arguments (str)에 있음."""
    for msg in reversed(response):
        role = msg.role if hasattr(msg, "role") else msg.get("role", "")
        if role != ASSISTANT_ROLE:
            continue
        # Message 객체 경로
        fc = getattr(msg, "function_call", None)
        if fc is not None:
            args_str = getattr(fc, "arguments", None) or ""
            try:
                return json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                return {}
        # dict 경로 (드물지만 호환)
        fc_dict = msg.get("function_call") if isinstance(msg, dict) else None
        if fc_dict:
            try:
                return json.loads(fc_dict.get("arguments", "{}"))
            except json.JSONDecodeError:
                return {}
    return {}


def _run_worker(
    store: DocStore,
    command: str,
    bus: EventBus,
    max_steps: int = WORKER_MAX_STEPS,
    priority_docs: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    task: str | None = None,
    reference_names: list[str] | None = None,
) -> tuple[str, list[str]]:
    """WorkerAgent: Plan-Act-Reflect 루프. 반복 출력 방지를 위해 seen_count 추적."""
    _set_store(store)  # thread-local에 저장 → 동시 요청 간 격리

    tools = allowed_tools or ["list_docs", "read_doc", "search_doc"]
    bus.phase(f"Worker: 문서 탐색 시작 (허용 툴: {', '.join(tools)})")

    agent = FnCallAgent(
        llm=LLM_CONFIG,
        system_message=_WORKER_SYSTEM,
        function_list=tools,
    )

    # 참고 문서 힌트 (결과 출력 대상 아님을 명시)
    user_content = command
    if reference_names:
        valid_refs = [n for n in reference_names if n in store.list()]
        if valid_refs:
            ref_hint = (
                "\n\n[참고 문서 안내] 아래 문서는 작업 참고용입니다. "
                "결과물에 이 문서들의 이름이나 내용을 직접 출력하지 마세요:\n"
                + "\n".join(f"- {n}" for n in valid_refs)
            )
            user_content = command + ref_hint

    # Planner가 우선순위 문서를 지정했으면 Worker 프롬프트에 힌트 추가
    if priority_docs:
        # store에 실제 존재하는 이름만 허용 (주입 방지)
        valid = [d for d in priority_docs if d in store.list()]
        dropped = [d for d in priority_docs if d not in store.list()]
        if dropped:
            bus.reflect(f"Planner 힌트 문서 없음(무시): {dropped}")
        if valid:
            hint = "\n\n[Planner 힌트] 아래 문서를 우선적으로 읽으세요:\n" + "\n".join(f"- {d}" for d in valid)
            user_content = user_content + hint

    messages = [{"role": "user", "content": user_content}]
    referenced: list[str] = []
    collected_contents: list[str] = []
    final_content = ""
    tool_steps = 0

    # 반복 출력 방지: 이미 처리한 메시지 수 추적
    seen_count = 0
    # ASSISTANT 스트리밍 중복 방지: 마지막으로 emit한 think 내용
    last_think = ""

    gen = agent.run(messages)
    try:
        for response in gen:
            if not response:
                continue

            new_msgs = response[seen_count:]
            seen_count = len(response)

            for msg in new_msgs:
                role = msg.role if hasattr(msg, "role") else msg.get("role", "")
                content = msg.content if hasattr(msg, "content") else msg.get("content", "")
                name = msg.name if hasattr(msg, "name") else msg.get("name", "")

                if role == FUNCTION:
                    tool_args = _extract_tool_args(response)
                    tpl = _TOOL_MESSAGES.get(name, f"{name} 실행 중...")
                    try:
                        msg_text = tpl.format(**tool_args)
                    except KeyError:
                        msg_text = tpl
                    bus.tool(msg_text, tool_name=name, **tool_args)

                    if name == "read_doc":
                        doc_name = tool_args.get("name", "")
                        if doc_name:
                            referenced.append(doc_name)
                            # content가 ContentItem 리스트일 경우 text 추출
                            if isinstance(content, str):
                                text_content = content
                            elif isinstance(content, list):
                                text_content = "\n".join(
                                    item.text if hasattr(item, "text") else str(item)
                                    for item in content
                                )
                            else:
                                text_content = str(content)
                            collected_contents.append(f"[{doc_name}]\n{text_content[:2000]}")

                    tool_steps += 1

                    if tool_steps % REFLECT_INTERVAL == 0 and tool_steps < max_steps:
                        verdict = _run_reflector(store, command, "\n\n".join(collected_contents), bus)
                        bus.reflect(f"충분: {verdict['sufficient']} — {verdict['reason']}")

                        if verdict["sufficient"]:
                            bus.reflect("정보 충분 → 결과 생성 단계로")
                            tool_steps = max_steps  # 아래 break 조건 유도
                        else:
                            hints = verdict.get("need_more", [])
                            if hints:
                                bus.reflect(f"추가 탐색 필요: {hints}")

                    if tool_steps >= max_steps:
                        bus.reflect(f"최대 탐색 횟수({max_steps}) 도달 → 결과 생성")
                        break

                elif role == ASSISTANT_ROLE:
                    content_str = content if isinstance(content, str) else ""
                    preview = content_str.strip()[:80].replace("\n", " ")
                    if preview and preview != last_think:
                        bus.think(preview + ("..." if len(content_str.strip()) > 80 else ""))
                        last_think = preview
                    if content_str.strip():
                        final_content = content_str

            if tool_steps >= max_steps:
                break
    finally:
        gen.close()

    # FnCallAgent가 tool 루프 중단 후 최종 텍스트를 생성하지 못한 경우,
    # 수집된 문서 내용(없으면 전체 문서 목록)을 바탕으로 합성 호출로 결과 생성.
    if not final_content.strip():
        if not collected_contents:
            # read_doc 없이 max_steps 도달 — 문서 목록이라도 context로 제공
            collected_contents = [
                f"[{name}]\n{store.read(name)[:1000]}"
                for name in store.list()
            ]
        bus.phase("Worker: 수집 내용 기반 최종 결과 합성 중...")
        synthesis_ctx = (
            f"## 수행할 태스크\n{command}\n\n"
            f"## 지금까지 읽은 문서 내용\n"
            + "\n\n".join(collected_contents)
        )
        synth_sys = _SYNTHESIS_CLASSIFY_SYSTEM if task == "classify" else _SYNTHESIS_SYSTEM
        synth_agent = Assistant(llm=LLM_CONFIG, system_message=synth_sys)
        for resp in synth_agent.run([{"role": "user", "content": synthesis_ctx}]):
            t = _extract_text(resp)
            if t:
                final_content = t

    return final_content, list(set(referenced))


# ══════════════════════════════════════════════════════════════
# CriticAgent
# ══════════════════════════════════════════════════════════════

_CRITIC_SYSTEM = """당신은 문서 분류 검수 전문가입니다.

Worker 초안을 검토하고 반드시 아래 JSON 스키마로만 반환하세요. 다른 텍스트 절대 포함 금지.

{
  "node_path": "대분류/소분류/세부분류",
  "summary": "문서 전체 내용 한 문단 요약",
  "keywords": ["핵심키워드1", "핵심키워드2"],
  "sample_questions": ["이 문서로 답할 수 있는 질문1", "질문2"],
  "classification_reason": "이 분류를 선택한 이유",
  "confidence_score": 0.0
}

검토 기준:
1. node_path가 taxonomy에 실제 존재하는 경로인지
2. confidence_score가 근거 대비 적절한지 (0.0~1.0)
3. summary가 문서 내용을 정확히 반영하는지
4. keywords/sample_questions가 실질적인지 (최대 각 15개, 10개)

수정 불필요 시 초안 그대로 스키마에 맞춰 반환."""


def _run_critic(store: DocStore, worker_result: str, referenced_docs: list[str], bus: EventBus) -> str:
    bus.phase("Critic: 결과 검토 중...")

    parts = ["## Worker 초안\n" + worker_result]

    for name in store.list():
        if "taxonomy" in name.lower() or "분류체계" in name:
            parts.append(f"## 분류 체계\n{store.read(name)}")

    for name in referenced_docs:
        content = store.read(name)
        if not content.startswith("[오류]"):
            parts.append(f"## 참조: {name}\n{content[:CRITIC_REF_CHARS]}")

    agent = Assistant(llm=LLM_CONFIG, system_message=_CRITIC_SYSTEM)
    final = ""
    for response in agent.run([{"role": "user", "content": "\n\n---\n\n".join(parts)}]):
        t = _extract_text(response)
        if t:
            final = t
    return final


# ── classify 결과 정규화 ──────────────────────────────────────

_CLASSIFY_DEFAULTS: dict = {
    "node_path": "미분류",
    "summary": "",
    "keywords": [],
    "sample_questions": [],
    "classification_reason": "",
    "confidence_score": 0.0,
}


def _normalize_classify(raw) -> dict:
    """LLM 출력이 어떤 형태든 studio mini 스키마를 보장.
    구 스키마 필드(reason, category_name, alternative_path)도 흡수.
    """
    if not isinstance(raw, dict):
        return {**_CLASSIFY_DEFAULTS, "classification_reason": str(raw) if raw else ""}
    result = {**_CLASSIFY_DEFAULTS}
    field_aliases = {
        "reason": "classification_reason",
        "category_name": "node_path",  # 최후 fallback — 덮어쓰지 않음
    }
    for k, v in raw.items():
        alias = field_aliases.get(k)
        if alias and not raw.get(alias):  # alias로 변환 (원래 필드가 없을 때만)
            result[alias] = str(v)
        elif k in result:
            result[k] = v
    # 타입 강제
    try:
        result["confidence_score"] = float(result["confidence_score"])
    except (TypeError, ValueError):
        result["confidence_score"] = 0.0
    result["confidence_score"] = max(0.0, min(1.0, result["confidence_score"]))
    if not isinstance(result["keywords"], list):
        result["keywords"] = []
    if not isinstance(result["sample_questions"], list):
        result["sample_questions"] = []
    return result


# ══════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════

def run(
    command: str,
    documents: list[dict],
    use_critic: bool = True,
    max_steps: int = WORKER_MAX_STEPS,
    force_mode: str | None = None,        # "direct" | "tool" | None(자동)
    bus: EventBus | None = None,          # None이면 CLI 출력 기본값
    task: str | None = None,              # 태스크 이름 (툴 기본값 결정에 사용)
    allowed_tools: list[str] | None = None,  # None=task 기본값, 명시 시 override
    reference_names: list[str] | None = None,  # 참고용 문서명 — 결과 출력 대상 아님
) -> dict | str:
    """
    Args:
        command:         수행할 태스크 명령
        documents:       [{"name": str, "content": str}, ...] (taxonomy 포함)
        use_critic:      Critic 검토 활성화 (분류 태스크 권장)
        max_steps:       tool calling 최대 횟수
        force_mode:      None=자동 분기, "direct"=강제 직접주입, "tool"=강제 tool calling
        bus:             EventBus 인스턴스. None이면 CLI 출력용 기본 버스 생성.
        task:            태스크 이름 — TASK_TOOL_DEFAULTS에서 기본 툴 목록 결정
        allowed_tools:   허용할 툴 명시 리스트. None이면 task 기반 기본값 사용.
        reference_names: 참고용 문서 이름 목록. 결과에 언급하지 말라는 힌트를 Worker에 전달.

    Returns:
        classify → dict, 그 외 → str
    """
    if bus is None:
        bus = EventBus(handler=cli_handler)

    _run_semaphore.acquire()
    try:
        return _run_inner(
            command=command, documents=documents, use_critic=use_critic,
            max_steps=max_steps, force_mode=force_mode, bus=bus,
            task=task, allowed_tools=allowed_tools, reference_names=reference_names,
        )
    finally:
        _run_semaphore.release()


def _run_inner(
    command: str,
    documents: list[dict],
    use_critic: bool,
    max_steps: int,
    force_mode: str | None,
    bus: EventBus,
    task: str | None,
    allowed_tools: list[str] | None,
    reference_names: list[str] | None,
) -> dict | str:
    tools = get_allowed_tools(task, allowed_tools)
    store = DocStore(documents)
    tokens = store.total_tokens()
    priority_docs: list[str] = []

    # ── classify 경로: CLASSIFY_MODE 설정으로 제어 ──────────────
    if task == "classify" and force_mode is None:
        cmode = CLASSIFY_MODE  # "direct_critic" | "direct" | "tool"
        if cmode in ("direct_critic", "direct"):
            force_mode = "direct"
            use_critic = (cmode == "direct_critic")
        else:
            force_mode = "tool"
            max_steps = CLASSIFY_MAX_STEPS

    # ── summarize / translate: 토큰 기준 direct or 청크 direct ──
    # Planner 없이 토큰 수로만 분기. 초과 시 청크 처리.
    if task in ("summarize", "translate") and force_mode is None:
        force_mode = "direct"  # tool 경로 스킵 — 어차피 전부 읽어야 함

    # 경로 결정
    if force_mode in ("direct", "tool"):
        mode = force_mode
    else:
        # qna 등: Planner에게 판단 위임 → 실패 시 토큰 임계값 폴백
        plan = _run_planner(store, command, bus)
        if plan.get("mode") in ("direct", "tool"):
            mode = plan["mode"]
            priority_docs = plan.get("priority_docs") or []
        else:
            mode = "direct" if tokens <= DIRECT_INJECT_TOKEN_LIMIT else "tool"
            bus.reflect(f"폴백 임계값 적용: {DIRECT_INJECT_TOKEN_LIMIT:,} 토큰 기준 → {mode.upper()}")

    bus.phase(
        f"문서 {len(documents)}개 | 토큰 {tokens:,} | 경로: {mode.upper()} | "
        f"Critic: {'ON' if use_critic else 'OFF'}"
    )

    if mode == "direct":
        # 토큰 초과 시 청크 처리 (summarize/translate만 해당)
        if task in ("summarize", "translate") and tokens > DIRECT_INJECT_TOKEN_LIMIT:
            worker_result = _run_direct_chunked(store, command, task, bus)
        else:
            worker_result = _run_direct(store, command, bus)
        referenced = []
    else:
        worker_result, referenced = _run_worker(
            store, command, bus,
            max_steps=max_steps,
            priority_docs=priority_docs,
            allowed_tools=tools,
            task=task,
            reference_names=reference_names,
        )

    if not worker_result.strip():
        bus.error("응답 없음")
        return {"error": "응답 없음"}

    if use_critic:
        final_raw = _run_critic(store, worker_result, referenced, bus)
    else:
        final_raw = worker_result

    bus.done()
    parsed = _parse_json(final_raw)
    if task == "classify":
        return _normalize_classify(parsed)
    return parsed


# ══════════════════════════════════════════════════════════════
# DocSession — 하위 호환
# ══════════════════════════════════════════════════════════════

class DocSession:
    def __init__(
        self,
        command: str,
        documents: list[dict] | None = None,
        context_summary: str | None = None,
        extra_files: list[str] | None = None,
    ):
        self.command = command
        self.documents = list(documents or [])

        if context_summary:
            self.documents.append({
                "name": "context_이전대화",
                "content": f"# 이전 대화 요약\n\n{context_summary}"
            })

        for path in extra_files or []:
            try:
                content = Path(path).read_text(encoding="utf-8")
                self.documents.append({"name": Path(path).stem, "content": content})
            except Exception as e:
                print(f"[DocSession] 파일 로드 실패: {path} — {e}")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
