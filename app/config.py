from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).parent / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
except ImportError:
    pass  # python-dotenv 없으면 환경변수로 직접 설정

# ──────────────────────────────────────────────
# LLM 연결
# ──────────────────────────────────────────────
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        print(f"[config] {key} 값이 올바르지 않음 — 기본값 {default} 사용")
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        print(f"[config] {key} 값이 올바르지 않음 — 기본값 {default} 사용")
        return default

LLM_CONFIG = {
    "model":        os.getenv("MODEL_NAME",   "Qwen3-8B"),
    "model_server": os.getenv("MODEL_SERVER", "http://localhost:8000/v1"),
    "api_key":      os.getenv("MODEL_API_KEY","EMPTY"),
    "generate_cfg": {
        "temperature":      _env_float("LLM_TEMPERATURE", 0.1),
        "max_input_tokens": _env_int("LLM_MAX_INPUT_TOKENS", 30000),
    },
}

# ──────────────────────────────────────────────
# 파일 크기 분기
# ──────────────────────────────────────────────
# 문서 전체 토큰이 이 값 이하면 직접 주입, 초과하면 tool calling
DIRECT_INJECT_TOKEN_LIMIT: int = _env_int("DIRECT_INJECT_TOKEN_LIMIT", 12000)

# ──────────────────────────────────────────────
# Worker (tool calling 경로)
# ──────────────────────────────────────────────
WORKER_MAX_STEPS: int      = _env_int("WORKER_MAX_STEPS", 10)

# ──────────────────────────────────────────────
# Classify 경로 설정
# ──────────────────────────────────────────────
# "direct_critic" : Planner/Worker/Reflector 제거, Critic 유지 (빠름 + 품질 검토)
# "direct"        : LLM 1회만 (가장 빠름, 스키마 정규화로 품질 보완)
# "tool"          : 기존 tool calling 경로 (CLASSIFY_MAX_STEPS 적용)
CLASSIFY_MODE: str     = os.getenv("CLASSIFY_MODE", "direct_critic")
CLASSIFY_MAX_STEPS: int = _env_int("CLASSIFY_MAX_STEPS", 3)

# ──────────────────────────────────────────────
# Direct 청크 처리 (summarize / translate)
# ──────────────────────────────────────────────
# direct 경로에서 총 토큰이 DIRECT_INJECT_TOKEN_LIMIT 초과 시 청크 분할 처리.
# DIRECT_CHUNK_TOKENS: 청크 1개당 최대 토큰 수
DIRECT_CHUNK_TOKENS: int = _env_int("DIRECT_CHUNK_TOKENS", 10000)

# ──────────────────────────────────────────────
# 동시 실행 제어
# ──────────────────────────────────────────────
# agent_core.run() 동시 실행 최대 수. 1=완전 직렬, 2+=제한적 병렬.
# 폐쇄망 단일 LLM 서버 환경에서는 1 권장.
AGENT_MAX_CONCURRENCY: int = _env_int("AGENT_MAX_CONCURRENCY", 1)
REFLECT_INTERVAL: int      = _env_int("REFLECT_INTERVAL", 3)
REFLECT_CONTEXT_CHARS: int = _env_int("REFLECT_CONTEXT_CHARS", 3000)
SEARCH_MAX_LINES: int      = _env_int("SEARCH_MAX_LINES", 30)

# 태스크별 기본 허용 툴 — 명시하지 않은 태스크는 전체 허용
# 새 툴 추가 시 여기서 태스크별 허용 여부를 명시적으로 결정
_ALL_TOOLS = [
    "list_docs", "read_doc", "search_doc",
    "find_docs", "get_doc_metadata",
    "extract_section", "chunk_doc",
]
TASK_TOOL_DEFAULTS: dict[str, list[str]] = {
    "classify":  ["list_docs", "read_doc", "find_docs", "get_doc_metadata", "extract_section"],
    "qna":       ["list_docs", "read_doc", "search_doc", "find_docs", "get_doc_metadata", "extract_section", "chunk_doc"],
    "summarize": ["list_docs", "read_doc", "find_docs", "extract_section", "chunk_doc"],
    "translate": ["list_docs", "read_doc", "chunk_doc"],
}


def get_allowed_tools(task: str | None, override: list[str] | None = None) -> list[str]:
    """
    task와 override를 받아 실제 허용할 툴 목록 반환.
    override가 있으면 그것을 사용 (단, 등록된 툴 이름만 허용).
    없으면 TASK_TOOL_DEFAULTS, 그것도 없으면 전체.
    """
    if override is not None:
        unknown = [t for t in override if t not in _ALL_TOOLS]
        if unknown:
            print(f"[config] 알 수 없는 툴 무시: {unknown}")
        return [t for t in override if t in _ALL_TOOLS] or _ALL_TOOLS
    return TASK_TOOL_DEFAULTS.get(task or "", _ALL_TOOLS)

# ──────────────────────────────────────────────
# Critic
# ──────────────────────────────────────────────
CRITIC_REF_CHARS: int = _env_int("CRITIC_REF_CHARS", 2000)

# ──────────────────────────────────────────────
# API 서버
# ──────────────────────────────────────────────
API_HOST: str  = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int  = _env_int("API_PORT", 8100)

# ──────────────────────────────────────────────
# CLI 대화 히스토리
# ──────────────────────────────────────────────
MAX_HISTORY_TURNS: int = _env_int("MAX_HISTORY_TURNS", 10)
# ──────────────────────────────────────────────
# PPTX 생성 업무
# ──────────────────────────────────────────────
# 업로드 템플릿, 생성 결과, QA 메타데이터 저장 위치.
# 폐쇄망 운영 시 별도 볼륨으로 지정 권장.
PPTX_WORKSPACE_DIR: Path = Path(
    os.getenv("PPTX_WORKSPACE_DIR", str(Path(__file__).parent / "workspace" / "pptx_jobs"))
)
PPTX_MAX_UPLOAD_MB: int = _env_int("PPTX_MAX_UPLOAD_MB", 50)
PPTX_JOB_TIMEOUT: int = _env_int("PPTX_JOB_TIMEOUT", 600)

