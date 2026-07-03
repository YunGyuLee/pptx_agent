"""
테스트 실행 스크립트
실행: python3 run_test.py [classify|qna|summarize|all]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import agent_core

BASE = Path(__file__).parent
DOCS_DIR = BASE / "docs"
CMD_DIR = BASE / "commands"
OUT_DIR = BASE / "output"
OUT_DIR.mkdir(exist_ok=True)

# 여신 규정 문서 5개
REGULATION_DOCS = [
    {"name": f.stem, "content": f.read_text(encoding="utf-8")}
    for f in sorted(DOCS_DIR.glob("여신규정_*.md"))
]

# 부가 파일
CONTEXT_MD   = (DOCS_DIR / "context_이전대화.md").read_text(encoding="utf-8")
TAXONOMY_MD  = (DOCS_DIR / "taxonomy_분류체계.md").read_text(encoding="utf-8")


def run_task(task_name: str, command: str, documents: list, context: str | None = None, use_critic: bool = False) -> str:
    print(f"\n{'='*60}")
    print(f"[{task_name}] 시작")
    print(f"  문서 수: {len(documents)}개  |  Critic: {'ON' if use_critic else 'OFF'}")
    print(f"  컨텍스트: {'있음' if context else '없음'}")
    print(f"{'='*60}")

    docs = list(documents)
    if context:
        docs.append({"name": "context_이전대화", "content": f"# 이전 대화 요약\n\n{context}"})

    start = time.time()
    result = agent_core.run(command=command, documents=docs, use_critic=use_critic)
    elapsed = time.time() - start

    result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)

    print(f"\n[결과] ({elapsed:.1f}초)")
    print(result_str)

    out_file = OUT_DIR / f"result_{task_name}.txt"
    out_file.write_text(result_str, encoding="utf-8")
    print(f"\n  → 저장: {out_file}")

    return result_str


def test_classify():
    """분류: taxonomy 포함해서 넘김 — Worker가 스스로 읽고 판단, Critic 검토"""
    command = (CMD_DIR / "command_classify.md").read_text(encoding="utf-8")

    for doc in REGULATION_DOCS:
        # taxonomy도 같이 넘김 — Worker가 list_docs로 발견 후 먼저 읽음
        docs = [doc, {"name": "taxonomy_분류체계", "content": TAXONOMY_MD}]
        result_str = run_task(
            task_name=f"classify_{doc['name']}",
            command=command,
            documents=docs,
            use_critic=True,
        )
        try:
            parsed = json.loads(result_str)
            print(f"\n  node_path: {parsed.get('node_path')}")
            print(f"  confidence: {parsed.get('confidence_score')}")
        except Exception:
            print("  [경고] JSON 파싱 실패 — 모델 응답 형식 확인 필요")


def test_qna():
    """QnA: 전체 규정 + taxonomy + 이전대화 컨텍스트"""
    command = (CMD_DIR / "command_qna.md").read_text(encoding="utf-8")

    docs = REGULATION_DOCS + [{"name": "taxonomy_분류체계", "content": TAXONOMY_MD}]
    run_task(
        task_name="qna",
        command=command,
        documents=docs,
        context=CONTEXT_MD,
        use_critic=False,
    )


def test_summarize():
    """요약: 전체 규정 + taxonomy"""
    command = (CMD_DIR / "command_summarize.md").read_text(encoding="utf-8")

    docs = REGULATION_DOCS + [{"name": "taxonomy_분류체계", "content": TAXONOMY_MD}]
    run_task(
        task_name="summarize",
        command=command,
        documents=docs,
        use_critic=False,
    )


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    tasks = {
        "classify":  test_classify,
        "qna":       test_qna,
        "summarize": test_summarize,
    }

    if mode == "all":
        for fn in tasks.values():
            fn()
    elif mode in tasks:
        tasks[mode]()
    else:
        print(f"사용법: python3 run_test.py [{'|'.join(tasks)}|all]")
        sys.exit(1)

    print(f"\n\n완료. 결과 파일: {OUT_DIR}")


if __name__ == "__main__":
    main()
