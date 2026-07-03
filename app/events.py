"""
EventBus — 에이전트 진행 상태 이벤트 시스템

CLI  → 즉시 print
API  → SSE 큐에 적재, 클라이언트로 스트리밍
Test → 수집만 (핸들러 없음)
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Callable


# ── 이벤트 타입 정의 ──────────────────────────────────────────

EVENT_PHASE     = "phase"       # [Worker: 문서 탐색 시작]
EVENT_TOOL      = "tool_call"   # 📋 문서 목록 파악 중...
EVENT_REFLECT   = "reflect"     # 🔄 정보 충분 → 결과 생성 단계로
EVENT_THINK     = "think"       # 💭 내부 추론 미리보기 (선택적)
EVENT_DONE      = "done"        # 완료 (sentinel)
EVENT_ERROR     = "error"       # 오류


@dataclass
class AgentEvent:
    type: str
    message: str
    meta: dict = field(default_factory=dict)  # tool_name, doc_name 등 부가정보


# ── 핸들러 프리셋 ─────────────────────────────────────────────

_ICONS = {
    EVENT_PHASE:   "",
    EVENT_TOOL:    "  ",
    EVENT_REFLECT: "  ",
    EVENT_THINK:   "  ",
    EVENT_DONE:    "",
    EVENT_ERROR:   "  ⚠ ",
}

def cli_handler(event: AgentEvent) -> None:
    """터미널에 즉시 출력. phase는 헤더 형식."""
    if event.type == EVENT_PHASE:
        print(f"\n[{event.message}]")
    elif event.type == EVENT_DONE:
        print("\n[완료]")
    elif event.type == EVENT_ERROR:
        print(f"  ⚠ {event.message}")
    else:
        icon = _ICONS.get(event.type, "  ")
        print(f"{icon}{event.message}")


def silent_handler(event: AgentEvent) -> None:
    """이벤트 무시 (테스트용)."""
    pass


# ── EventBus ─────────────────────────────────────────────────

class EventBus:
    """
    emit()으로 이벤트 발행.
    handler는 호출 스레드에서 동기 실행됨.
    SSE용으로 쓸 때는 QueueEventBus 사용.
    """

    def __init__(self, handler: Callable[[AgentEvent], None] = cli_handler):
        self._handler = handler
        self._events: list[AgentEvent] = []
        self._lock = threading.RLock()  # 재진입 허용 — 핸들러 내 emit() 호출 시 데드락 방지

    def emit(self, type: str, message: str, **meta) -> None:
        event = AgentEvent(type=type, message=message, meta=meta)
        with self._lock:
            self._events.append(event)
        self._handler(event)

    # 단축 메서드
    def phase(self, msg: str)   -> None: self.emit(EVENT_PHASE, msg)
    def tool(self, msg: str, **meta) -> None: self.emit(EVENT_TOOL, msg, **meta)
    def reflect(self, msg: str) -> None: self.emit(EVENT_REFLECT, msg)
    def think(self, msg: str)   -> None: self.emit(EVENT_THINK, msg)
    def done(self)              -> None: self.emit(EVENT_DONE, "")
    def error(self, msg: str)   -> None: self.emit(EVENT_ERROR, msg)

    @property
    def events(self) -> list[AgentEvent]:
        with self._lock:
            return list(self._events)


class QueueEventBus(EventBus):
    """
    SSE/WebSocket 스트리밍용.
    emit()된 이벤트를 내부 큐에 쌓음.
    get() 또는 iter()로 소비.
    """

    def __init__(self, timeout: float = 300.0):
        self._timeout = timeout
        self._q: queue.Queue[AgentEvent | None] = queue.Queue()
        super().__init__(handler=self._enqueue)

    def _enqueue(self, event: AgentEvent) -> None:
        self._q.put(event)

    def close(self) -> None:
        """스트림 종료 신호 (None sentinel)."""
        self._q.put(None)

    def __iter__(self):
        """None sentinel이 올 때까지 이벤트 yield. _timeout 초 내 신호 없으면 종료."""
        while True:
            try:
                event = self._q.get(timeout=self._timeout)
            except queue.Empty:
                break
            if event is None:
                break
            yield event
