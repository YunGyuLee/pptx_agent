"""
CLI 기반 대화형 어시스턴트
실행: python3 cli_chat.py
문서 첨부: python3 cli_chat.py --docs 파일1.md 파일2.md
"""

import argparse
import sys

from qwen_agent.agents import Assistant

from config import LLM_CONFIG


def load_docs(paths: list[str]) -> list[dict]:
    docs = []
    for path in paths:
        try:
            content = open(path, encoding="utf-8").read()
            docs.append({"name": path, "content": content})
            print(f"  [문서 로드] {path}")
        except FileNotFoundError:
            print(f"  [경고] 파일 없음: {path}")
    return docs


def build_system_prompt(docs: list[dict]) -> str:
    if not docs:
        return "당신은 유능한 AI 어시스턴트입니다."

    doc_block = "\n\n".join(
        f"### {d['name']}\n{d['content']}" for d in docs
    )
    return f"당신은 유능한 AI 어시스턴트입니다. 아래 문서를 참고하여 답하라.\n\n{doc_block}"


MAX_HISTORY_TURNS = 10  # 유지할 최대 대화 턴 수 (user+ai 1쌍 = 1턴)


def _trim_history(messages: list[dict]) -> list[dict]:
    """최근 MAX_HISTORY_TURNS 턴만 유지."""
    max_msgs = MAX_HISTORY_TURNS * 2
    return messages[-max_msgs:] if len(messages) > max_msgs else messages


def chat(docs: list[dict]) -> None:
    system_prompt = build_system_prompt(docs)
    agent = Assistant(llm=LLM_CONFIG, system_message=system_prompt)

    messages: list[dict] = []

    print("\n대화를 시작합니다. 종료하려면 'exit' 또는 Ctrl+C\n")

    while True:
        try:
            user_input = input("나: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n종료합니다.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "종료"):
            print("종료합니다.")
            break

        messages.append({"role": "user", "content": user_input})

        print("AI: ", end="", flush=True)
        response = None
        for response in agent.run(_trim_history(messages)):
            pass

        if response is None:
            print("[응답 없음]")
            continue

        answer = response[-1]["content"]
        print(answer)
        print()

        messages.append({"role": "assistant", "content": answer})


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI 대화형 어시스턴트")
    parser.add_argument("--docs", nargs="*", default=[], metavar="FILE",
                        help="참고할 문서 파일 경로 (md 등)")
    args = parser.parse_args()

    docs = load_docs(args.docs) if args.docs else []
    chat(docs)


if __name__ == "__main__":
    main()
