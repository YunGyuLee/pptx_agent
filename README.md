# 은행 폐쇄망 문서 에이전트 — 반입 패키지

## 디렉토리 구조

```
qwen_agent_export/
├── README.md                  ← 이 파일
├── app/                       ← 실행 코드 (폐쇄망 반입 대상)
│   ├── agent_core.py          ← 에이전트 핵심 로직 (Planner/Worker/Reflector/Critic)
│   ├── agent_api.py           ← FastAPI REST 서버 (포트 8100)
│   ├── config.py              ← 환경변수 기반 설정 + 툴 제한 정책
│   ├── events.py              ← SSE 이벤트 버스
│   ├── prompt_manager.py      ← 태스크별 프롬프트 템플릿
│   ├── cli_chat.py            ← CLI 대화 인터페이스
│   ├── .env.example           ← 환경변수 예시 (복사 후 .env로 사용)
│   └── qwen_agent/            ← Qwen-Agent 라이브러리 (vendored, dashscope/GUI 제외)
├── test_package/              ← 반입 후 동작 검증용
│   ├── run_test.py            ← classify/qna/summarize 전체 테스트
│   ├── docs/                  ← 샘플 문서 (여신규정 5개 + taxonomy + 이전대화)
│   └── commands/              ← 태스크별 커맨드 MD
├── mock_llm_server.py         ← LLM 없이 테스트할 때 쓰는 목 서버 (포트 18000)
└── requirements_check.txt     ← 버전 호환성 메모

## 개발/운영 분리

| 폴더 | 용도 |
|------|------|
| `~/Desktop/Backup/Qwen-agent/` | 개발·테스트 원본 (git 관리) |
| `~/Desktop/Backup/qwen_agent_export/` | 반입 직전 파일 보관 (이 폴더) |
| `qwen_agent_export/dist/` | 폐쇄망 반입용 최종 패키징 |
```

---

## 실행 방법

### 1. 환경 설정

```bash
cd app/
cp .env.example .env
# .env 편집 — MODEL_NAME, MODEL_SERVER, MODEL_API_KEY 설정
```

### 2. API 서버 실행

```bash
cd app/
python3 agent_api.py
# → http://localhost:8100
```

### 3. 동작 검증 (목 서버 사용 시)

터미널 1:
```bash
python3 mock_llm_server.py   # 포트 18000
```

터미널 2:
```bash
cd test_package/
MODEL_NAME=mock-llm MODEL_SERVER=http://localhost:18000/v1 MODEL_API_KEY=EMPTY \
python3 run_test.py all
```

### 4. CLI 대화

```bash
cd app/
python3 cli_chat.py
```

---

## API 스펙

### POST /api/v1/agent/run

```json
{
  "task": "classify",           // classify | qna | summarize | translate
  "documents": [
    {"name": "파일명", "content": "문서 내용"}
  ],
  "question": "질문 (qna 전용)",
  "context_summary": "이전 대화 요약 (선택)",
  "allowed_tools": ["list_docs", "read_doc"]   // 툴 제한 (선택, 기본값: 태스크별 정책)
}
```

응답:
```json
{
  "task": "classify",
  "result": {
    "node_path": "여신/담보관리",
    "summary": "...",
    "keywords": ["담보", "LTV"],
    "sample_questions": ["..."],
    "classification_reason": "...",
    "confidence_score": 0.92
  }
}
```

### POST /api/v1/agent/run/stream

동일 요청, SSE 응답:
```
data: {"type": "phase", "message": "Planner: 전략 수립 중..."}
data: {"type": "reflect", "message": "정보 충분: True"}
data: {"type": "result", "result": {...}}
```

---

## 에이전트 아키텍처

```
요청
 └─ Planner         — DIRECT(소문서) / TOOL(대문서) 경로 결정
     ├─ [DIRECT]    — 문서 직접 주입 → LLM 1회 호출
     └─ [TOOL]      — Worker + Reflector 루프
         ├─ Worker  — list_docs → read_doc/search_doc 반복 탐색
         ├─ Reflector — 정보 충분성 판단 (REFLECT_INTERVAL 스텝마다)
         └─ Critic  — 결과 품질 검토 (classify 전용, use_critic=True)
```

### 처리 흐름

1. **Planner**: 전체 문서 토큰 합산 → `DIRECT_INJECT_TOKEN_LIMIT(12000)` 이하면 직접 주입
2. **Worker**: `FnCallAgent`가 툴 호출 루프 실행, 최대 `WORKER_MAX_STEPS(10)` 스텝
3. **Reflector**: 매 3스텝마다 "정보 충분?" 판단 → 충분하면 조기 종료
4. **Synthesis**: 루프 종료 후 final_content 없으면 수집 내용으로 별도 합성
5. **Critic**: classify 태스크에서 결과 JSON 품질 검토 후 재생성 여부 결정

---

## 툴 구성

| 툴 | 역할 | 파라미터 |
|----|------|----------|
| `list_docs` | 세션 문서 목록 반환 | 없음 |
| `read_doc` | 문서 전체 내용 반환 | `name` |
| `search_doc` | 문서 내 키워드 검색 | `name`, `query` |

### 태스크별 기본 허용 툴

| 태스크 | 허용 툴 |
|--------|---------|
| classify | list_docs, read_doc |
| qna | list_docs, read_doc, search_doc |
| summarize | list_docs, read_doc |
| translate | list_docs, read_doc |

API 요청 시 `allowed_tools` 필드로 override 가능. 미등록 툴 이름은 자동 필터링.

---

## 향후 추가 검토 중인 툴

| 툴 | 설명 | 우선순위 |
|----|------|----------|
| `extract_section` | 조항/섹션 단위 추출 (search_doc보다 구조 인식) | 높음 |
| `list_docs_by_keyword` | 키워드로 관련 문서 필터링 (대규모 문서셋 대응) | 높음 |
| `chunk_doc` | 초장문 문서를 청크 단위로 분할 반환 | 중간 |
| `compare_docs` | 두 문서 차이 비교 (규정 개정 대응) | 중간 |
| `get_doc_metadata` | 이전 분류 결과 조회 (중복 분류 방지) | 낮음 |

---

## 주요 설정값 (config.py / .env)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `MODEL_NAME` | Qwen3-8B | LLM 모델명 |
| `MODEL_SERVER` | http://localhost:8000/v1 | vLLM 서버 주소 |
| `MODEL_API_KEY` | EMPTY | API 키 |
| `DIRECT_INJECT_TOKEN_LIMIT` | 12000 | 직접 주입 임계 토큰 수 |
| `WORKER_MAX_STEPS` | 10 | Worker 최대 툴 호출 횟수 |
| `REFLECT_INTERVAL` | 3 | Reflector 실행 간격 (스텝) |
| `AGENT_TIMEOUT` | 300 | API 응답 타임아웃 (초) |
| `API_PORT` | 8100 | API 서버 포트 |

---

## 알려진 제약

- **NousFnCallPrompt 형식**: LLM은 `<tool_call>\n{...}\n</tool_call>` 텍스트로 툴 호출 (OpenAI native tool_calls 형식 아님)
- **Python 3.9 호환**: `from __future__ import annotations` 필수 (`str | None` 타입힌트)
- **폐쇄망**: dashscope, GUI, 웹 서버 컴포넌트 제외됨
- **목 서버**: 실제 LLM 없이 테스트 가능하나 고정 응답 — 실제 모델과 품질 차이 있음
