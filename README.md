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

## PPTX 생성 기능

사내 양식이 적용된 `.pptx` 템플릿과 대략적인 작성 내용을 입력받아, 템플릿의 대표 슬라이드를 복제/치환하는 방식으로 새 PPTX를 생성하는 기능입니다.

기존 문서 에이전트(`/api/v1/agent/run`)와 분리하여 별도 라우터로 구현되어 있습니다.

### API 흐름

#### 1. 구성안 생성

```http
POST /api/v1/pptx/plan
Content-Type: multipart/form-data
```

입력 필드:

| 필드 | 설명 |
|------|------|
| `template_file` | 사내 양식이 들어 있는 `.pptx` 파일 |
| `content` | 사용자가 만들고 싶은 발표 내용 |
| `instruction` | 추가 지시사항 |
| `output_filename` | 결과 파일명 |
| `slide_count` | 원하는 슬라이드 수 |
| `purpose` | 발표 목적 (`general`, `executive_report` 등) |
| `strictness` | 양식 엄격도 (`strict`, `balanced`, `flexible`) |

응답 예시:

```json
{
  "task": "pptx_plan_from_template",
  "result": {
    "job_id": "...",
    "status": "planned",
    "template_profile": {},
    "plan": {
      "title": "7월 월간 보고서",
      "purpose": "executive_report",
      "strictness": "strict",
      "slides": [
        {
          "slide_no": 1,
          "layout_role": "cover",
          "title": "7월 월간 보고서",
          "bullets": []
        }
      ]
    },
    "warnings": []
  }
}
```

#### 2. 승인된 구성안으로 PPTX 생성

```http
POST /api/v1/pptx/jobs/{job_id}/generate
Content-Type: application/json
```

요청 body:

```json
{
  "plan": {
    "title": "7월 월간 보고서",
    "purpose": "executive_report",
    "strictness": "strict",
    "slides": []
  },
  "output_filename": "monthly_report.pptx"
}
```

`plan`을 생략하면 `/plan` 단계에서 저장된 기본 구성안을 사용합니다.

응답 예시:

```json
{
  "task": "pptx_generate_from_template",
  "result": {
    "job_id": "...",
    "status": "done",
    "file_name": "monthly_report.pptx",
    "download_url": "/api/v1/pptx/files/{job_id}/monthly_report.pptx",
    "slide_count": 4,
    "warnings": []
  }
}
```

#### 3. 단축 생성 API

```http
POST /api/v1/pptx/generate
```

`/plan`과 `/generate`를 한 번에 실행하는 호환용 단축 API입니다. 실제 업무 화면에서는 사용자가 구성안을 확인할 수 있도록 `/plan` → `/jobs/{job_id}/generate` 흐름을 권장합니다.

#### 4. Job 및 파일 조회

```http
GET /api/v1/pptx/jobs/{job_id}
GET /api/v1/pptx/files/{job_id}/{filename}
```

### 템플릿 작성 규칙

양식 보존 정확도를 높이려면 사내 템플릿 PPTX에 대표 슬라이드를 넣어두는 방식을 권장합니다.

예시:

| 대표 슬라이드 | 역할 |
|---------------|------|
| 표지 | `cover` |
| 목차 | `agenda` |
| 일반 본문 | `content` |
| 표/현황 | `table` |
| 요약/결론 | `summary` |

가능하면 슬라이드의 작은 텍스트 또는 placeholder에 아래 마커를 넣어두면 역할 인식이 안정적입니다.

```text
[role:cover]
[role:agenda]
[role:content]
[role:table]
[role:summary]
```

마커가 없을 때는 슬라이드 순서, 텍스트, 표 포함 여부를 기준으로 역할을 추정합니다.

### 코드 구조

PPTX 기능은 `agent_core.py`에 섞지 않고 독립 모듈로 분리되어 있습니다.

| 파일 | 역할 |
|------|------|
| `app/pptx_api.py` | FastAPI 라우터. 업로드, plan 생성, PPTX 생성, 다운로드 API 제공 |
| `app/pptx_service.py` | PPTX 생성 도메인 로직. 템플릿 분석, 구성안 생성, 매핑, 렌더링, QA, job 저장소 포함 |
| `app/config.py` | PPTX 작업 디렉토리, 업로드 크기, timeout 설정 |
| `app/agent_api.py` | `pptx_api.router`를 기존 FastAPI 앱에 연결 |

### 코드 실행 경로

#### `/api/v1/pptx/plan`

```text
pptx_api.plan_pptx()
  └─ PptxGenerationService.create_plan()
      ├─ PptxJobStore.create_job_id()
      ├─ PptxJobStore.store_template()
      ├─ PptxTemplateInspector.inspect()
      ├─ PptxContentPlanner.create_plan()
      ├─ PptxQualityChecker.check_plan()
      └─ PptxJobStore.write_plan()
```

#### `/api/v1/pptx/jobs/{job_id}/generate`

```text
pptx_api.generate_pptx_from_plan()
  └─ PptxGenerationService.generate()
      ├─ PptxJobStore.read_plan_record()
      ├─ PptxJobStore.template_path()
      ├─ PptxLayoutMapper.map()
      ├─ PptxPlaceholderRenderer.render()
      ├─ PptxQualityChecker.check_generation()
      └─ PptxJobStore.write_generation_notes()
```

### 주요 클래스 책임

#### `PptxPlanRequest`

`/plan` 요청을 서비스 계층으로 전달하기 위한 입력 DTO입니다.

주요 필드:

```python
template_path: Path
content: str
instruction: str
output_filename: str
slide_count: int | None
purpose: str
strictness: "strict" | "balanced" | "flexible"
```

#### `PptxGenerateRequest`

승인된 plan을 실제 PPTX로 생성할 때 사용하는 입력 DTO입니다.

```python
job_id: str
plan: dict | None
output_filename: str | None
```

#### `PptxTemplateInspector`

업로드된 템플릿 PPTX를 zip 패키지로 열어 구조를 분석합니다.

분석 항목:

- 슬라이드 수
- 슬라이드 파일 목록
- 슬라이드 크기
- theme 파일 목록
- layout 목록
- 각 슬라이드의 텍스트 노드 수
- 각 슬라이드의 preview text
- table 포함 여부
- 대표 역할(`cover`, `agenda`, `content`, `table`, `summary`)

#### `PptxContentPlanner`

사용자의 rough content를 검토 가능한 `PptxDeckPlan`으로 변환합니다.

현재는 deterministic MVP입니다. 향후 LLM 기반 planner로 교체할 수 있도록 `PptxDeckPlan` 반환 계약을 분리해두었습니다.

#### `PptxLayoutMapper`

구성안의 각 슬라이드가 요구하는 `layout_role`에 맞춰 템플릿의 대표 슬라이드를 선택합니다.

매핑 규칙:

1. 같은 role의 대표 슬라이드가 있으면 사용
2. 없으면 `content` 대표 슬라이드로 fallback
3. 그래도 없으면 템플릿 슬라이드 순서 기준 fallback

#### `PptxPlaceholderRenderer`

실제 PPTX 파일을 생성합니다.

동작 방식:

1. 원본 PPTX를 zip으로 읽음
2. master/layout/theme/media 등 비슬라이드 리소스는 보존
3. plan의 슬라이드 수만큼 `ppt/slides/slide1.xml`, `slide2.xml` ... 생성
4. 대표 슬라이드 XML의 `<a:t>` 텍스트 노드를 title/bullets로 치환
5. `ppt/presentation.xml`의 slide list 갱신
6. `ppt/_rels/presentation.xml.rels` 갱신
7. `[Content_Types].xml` 갱신

즉 현재 방식은 새 디자인을 코드로 그리는 방식이 아니라, **사내 템플릿 대표 슬라이드를 복제하고 텍스트만 치환하는 방식**입니다.

#### `PptxJobStore`

job 단위 파일 저장을 담당합니다.

기본 저장 위치:

```text
app/workspace/pptx_jobs/{job_id}/
├── uploads/
│   └── template.pptx
├── outputs/
│   └── generated_deck.pptx
├── plan.json
└── generation_result.json
```

#### `PptxQualityChecker`

현재는 구조적 QA만 수행합니다.

확인 항목:

- 템플릿에 슬라이드가 있는지
- 템플릿 레이아웃 정보를 찾았는지
- 작성 내용이 비어 있는지
- 대표 슬라이드를 재사용해야 하는지
- 텍스트 노드 부족으로 일부 항목이 미반영되었는지

### 설정값

`app/config.py`에 PPTX 관련 설정이 추가되어 있습니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PPTX_WORKSPACE_DIR` | `app/workspace/pptx_jobs` | job, 업로드, 결과 파일 저장 위치 |
| `PPTX_MAX_UPLOAD_MB` | `50` | 업로드 가능한 PPTX 최대 크기 |
| `PPTX_JOB_TIMEOUT` | `600` | 향후 비동기 job timeout 용도 |

### 현재 한계 및 다음 개선 포인트

현재 구현은 MVP이며 아래 한계가 있습니다.

- 텍스트 치환은 `<a:t>` 노드 순서 기반입니다.
- PowerPoint placeholder 이름/위치 기반 치환은 아직 아닙니다.
- 표, 차트, 이미지 생성은 아직 지원하지 않습니다.
- 렌더링 기반 overflow/겹침 QA는 아직 없습니다.
- `PptxContentPlanner`는 LLM 기반이 아니라 deterministic planner입니다.

향후 고도화 시 교체/확장 지점:

| 개선 항목 | 확장 대상 |
|-----------|-----------|
| LLM 기반 슬라이드 기획 | `PptxContentPlanner` |
| placeholder-aware 치환 | `PptxPlaceholderRenderer` |
| 표/차트 생성 | `PptxPlaceholderRenderer` 또는 별도 renderer 클래스 |
| 렌더링 기반 QA | `PptxQualityChecker` |
| 비동기 job 처리 | `pptx_api.py`, `PptxJobStore` |
| PowerPoint Add-in 연동 | 현재 `/api/v1/pptx/*` API 재사용 |

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
