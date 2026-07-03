"""
시나리오별 테스트 케이스 정의
웹 UI(test_ui.py)와 CLI(run_test.py) 공용
"""

from __future__ import annotations
from pathlib import Path

BASE = Path(__file__).parent
DOCS_DIR = BASE / "docs"
CMD_DIR  = BASE / "commands"


def _doc(name: str) -> dict:
    return {"name": Path(name).stem, "content": (DOCS_DIR / name).read_text(encoding="utf-8")}

def _cmd(name: str) -> str:
    return (CMD_DIR / name).read_text(encoding="utf-8")

def _all_regs() -> list[dict]:
    return [_doc(f.name) for f in sorted(DOCS_DIR.glob("여신규정_*.md"))]


SCENARIOS: list[dict] = [
    # ── classify ──────────────────────────────────────────────
    {
        "id": "classify_single",
        "label": "분류 — 단일 문서 (담보관리)",
        "group": "classify",
        "task": "classify",
        "use_critic": True,
        "documents": lambda: [_doc("여신규정_03_담보관리.md"), _doc("taxonomy_분류체계.md")],
        "desc": "담보관리 규정 1개를 taxonomy 기준으로 분류. Critic 포함.",
    },
    {
        "id": "classify_ambiguous",
        "label": "분류 — 경계 문서 (신용평가)",
        "group": "classify",
        "task": "classify",
        "use_critic": True,
        "documents": lambda: [_doc("여신규정_02_신용평가.md"), _doc("taxonomy_분류체계.md")],
        "desc": "신용평가는 여신/신용평가 vs 리스크관리/신용리스크 경계. 분류 판단 확인.",
    },
    {
        "id": "classify_batch",
        "label": "분류 — 5개 문서 순차 (전체 규정)",
        "group": "classify",
        "task": "classify",
        "use_critic": True,
        "batch": True,
        "documents": lambda: [(
            [_doc(f.name), _doc("taxonomy_분류체계.md")]
        ) for f in sorted(DOCS_DIR.glob("여신규정_*.md"))],
        "desc": "전체 규정 5개를 각각 분류. node_path 중복/누락 확인.",
    },
    {
        "id": "classify_no_taxonomy",
        "label": "분류 — taxonomy 없이",
        "group": "classify",
        "task": "classify",
        "use_critic": False,
        "documents": lambda: [_doc("여신규정_04_심사절차.md")],
        "desc": "taxonomy 미제공 시 에이전트가 자체 판단하는지 확인.",
    },

    # ── qna ──────────────────────────────────────────────────
    {
        "id": "qna_specific",
        "label": "QnA — 특정 수치 질문 (LTV)",
        "group": "qna",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: [_doc("여신규정_03_담보관리.md")],
        "command": "담보 종류별 담보인정비율(LTV)을 모두 정리해줘. 부동산/동산/유가증권/예금담보 각각의 비율과 조건을 표로 만들어줘.",
        "desc": "수치 정확도 검증. 문서에서 LTV % 수치를 정확히 인용하는지 확인.",
    },
    {
        "id": "qna_cross_doc",
        "label": "QnA — 다중 문서 교차 질문",
        "group": "qna",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: _all_regs(),
        "command": "신용등급 BB 기업이 담보여신을 신청할 때 심사 절차와 필요 서류를 단계별로 설명해줘. 신용평가 기준과 담보 요건도 함께 알려줘.",
        "desc": "02(신용평가) + 03(담보) + 04(심사절차) 교차 참조 필요. 다중 문서 탐색 확인.",
    },
    {
        "id": "qna_with_context",
        "label": "QnA — 이전 대화 컨텍스트 포함",
        "group": "qna",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: [
            _doc("여신규정_03_담보관리.md"),
            _doc("여신규정_04_심사절차.md"),
            {"name": "context_이전대화", "content": (DOCS_DIR / "context_이전대화.md").read_text(encoding="utf-8")},
        ],
        "command": "이전에 물어본 담보 종류별 LTV에 이어서, 연대보증 예외 규정과 심사 소요 기간을 추가로 알려줘.",
        "desc": "이전 대화 요약을 컨텍스트로 받아 연속 질문 처리. context_이전대화 문서 활용 확인.",
    },
    {
        "id": "qna_not_in_doc",
        "label": "QnA — 문서에 없는 내용 질문",
        "group": "qna",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: [_doc("여신규정_01_총칙.md")],
        "command": "해외 여신 취급 시 적용되는 환율 헤지 기준을 설명해줘.",
        "desc": "문서에 없는 내용 → '문서에서 확인할 수 없습니다' 응답 여부 확인. 할루시네이션 방지 검증.",
    },

    # ── summarize ─────────────────────────────────────────────
    {
        "id": "summarize_single",
        "label": "요약 — 단일 문서 (사후관리)",
        "group": "summarize",
        "task": "summarize",
        "use_critic": False,
        "documents": lambda: [_doc("여신규정_05_사후관리.md")],
        "command": _cmd("command_summarize.md"),
        "desc": "단일 문서 요약. 핵심 수치 포함 여부 확인.",
    },
    {
        "id": "summarize_multi",
        "label": "요약 — 전체 규정 통합 요약",
        "group": "summarize",
        "task": "summarize",
        "use_critic": False,
        "documents": lambda: _all_regs(),
        "command": "전달된 여신업무 규정 전체(5편)를 편별로 핵심 내용을 요약하고, 실무자가 반드시 알아야 할 주요 수치와 기준을 정리해줘.",
        "desc": "5개 문서 통합 요약. 문서 탐색 효율(find_docs/extract_section 활용 여부) 확인.",
    },

    # ── 툴 검증 ───────────────────────────────────────────────
    {
        "id": "tool_extract_section",
        "label": "툴 — extract_section 활용 유도",
        "group": "tool",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: [_doc("여신규정_03_담보관리.md")],
        "command": "제13조(담보가치 평가기준) 전문을 그대로 인용해줘.",
        "desc": "특정 조항 전문 요청 → extract_section 툴 활용 여부 확인.",
    },
    {
        "id": "tool_find_docs",
        "label": "툴 — find_docs 활용 유도",
        "group": "tool",
        "task": "qna",
        "use_critic": False,
        "documents": lambda: _all_regs(),
        "command": "부도 처리 절차와 관련된 규정을 찾아서 설명해줘.",
        "desc": "5개 문서 중 관련 문서 탐색 → find_docs로 좁히는지 확인.",
    },
    {
        "id": "tool_metadata",
        "label": "툴 — get_doc_metadata 활용",
        "group": "tool",
        "task": "classify",
        "use_critic": False,
        "documents": lambda: [
            _doc("여신규정_01_총칙.md"),
            _doc("taxonomy_분류체계.md"),
            {
                "name": "doc_metadata",
                "content": '{"여신규정_01_총칙": {"node_path": "여신/총칙", "keywords": ["여신", "목적", "정의"], "confidence_score": 0.88}}',
            },
        ],
        "desc": "doc_metadata 문서 포함 시 get_doc_metadata 툴 활용 및 참고 여부 확인.",
    },
]
