"""
에이전트 테스트 웹 UI
실행: python3 test_ui.py
접속: http://localhost:7800
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import traceback
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_core
from test_scenarios import SCENARIOS

PORT = int(os.environ.get("TEST_UI_PORT", 7800))
HOST = os.environ.get("TEST_UI_HOST", "0.0.0.0")

# 실행 결과 캐시 (시나리오 ID → 결과)
_results: dict[str, dict] = {}
_lock = threading.Lock()


# ── 에이전트 실행 ────────────────────────────────────────────

def _run_scenario(scenario: dict) -> dict:
    sid = scenario["id"]
    with _lock:
        _results[sid] = {"status": "running", "started_at": time.time()}

    try:
        docs_fn = scenario["documents"]
        command = scenario.get("command") or ""
        use_critic = scenario.get("use_critic", False)
        task = scenario.get("task", "qna")

        if scenario.get("batch"):
            # 배치: 문서 목록의 목록
            doc_groups = docs_fn()
            outputs = []
            for docs in doc_groups:
                result = agent_core.run(
                    command=command or _default_command(task),
                    documents=docs,
                    use_critic=use_critic,
                    task=task,
                )
                outputs.append(result)
            final = outputs
        else:
            docs = docs_fn()
            final = agent_core.run(
                command=command or _default_command(task),
                documents=docs,
                use_critic=use_critic,
                task=task,
            )

        elapsed = time.time() - _results[sid]["started_at"]
        with _lock:
            _results[sid] = {
                "status": "done",
                "result": final,
                "elapsed": round(elapsed, 1),
            }
    except Exception as e:
        with _lock:
            _results[sid] = {
                "status": "error",
                "error": traceback.format_exc(),
                "elapsed": round(time.time() - _results[sid]["started_at"], 1),
            }


def _default_command(task: str) -> str:
    defaults = {
        "classify":  "문서를 분류하라.",
        "qna":       "문서 내용을 설명하라.",
        "summarize": "문서를 요약하라.",
        "translate": "문서를 번역하라.",
    }
    return defaults.get(task, "문서를 분석하라.")


# ── HTML 렌더링 ──────────────────────────────────────────────

GROUPS = {
    "classify": "분류",
    "qna":      "QnA",
    "summarize":"요약",
    "tool":     "툴 검증",
}

def _render_result(sid: str) -> str:
    with _lock:
        r = _results.get(sid)
    if not r:
        return ""
    if r["status"] == "running":
        return '<span class="badge running">실행 중...</span>'
    elapsed = f'<span class="elapsed">{r["elapsed"]}s</span>'
    if r["status"] == "error":
        return f'<span class="badge error">오류</span>{elapsed}<pre class="error-box">{_esc(r["error"])}</pre>'
    result = r["result"]
    if isinstance(result, list):
        parts = [json.dumps(item, ensure_ascii=False, indent=2) if isinstance(item, dict) else str(item) for item in result]
        text = "\n\n---\n\n".join(parts)
    elif isinstance(result, dict):
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        text = str(result)
    return f'<span class="badge done">완료</span>{elapsed}<pre class="result-box">{_esc(text)}</pre>'


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_html() -> str:
    env_info = f"""
    <div class="env-bar">
        <b>모델:</b> {os.environ.get('MODEL_NAME','?')} &nbsp;|&nbsp;
        <b>서버:</b> {os.environ.get('MODEL_SERVER','?')} &nbsp;|&nbsp;
        <b>포트:</b> {PORT}
    </div>"""

    # 그룹별 시나리오 카드
    sections = ""
    for gid, glabel in GROUPS.items():
        group_scenarios = [s for s in SCENARIOS if s["group"] == gid]
        cards = ""
        for s in group_scenarios:
            sid = s["id"]
            with _lock:
                r = _results.get(sid)
            status_cls = r["status"] if r else "idle"
            result_html = _render_result(sid)
            cards += f"""
            <div class="card {status_cls}" id="card-{sid}">
                <div class="card-header">
                    <span class="card-label">{_esc(s['label'])}</span>
                    <button onclick="runScenario('{sid}')" {'disabled' if r and r['status']=='running' else ''}>
                        ▶ 실행
                    </button>
                </div>
                <div class="card-desc">{_esc(s['desc'])}</div>
                <div class="card-result" id="result-{sid}">{result_html}</div>
            </div>"""

        sections += f"""
        <section>
            <div class="section-header">
                <h2>{glabel}</h2>
                <button class="run-all" onclick="runGroup('{gid}')">그룹 전체 실행</button>
            </div>
            <div class="cards">{cards}</div>
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>에이전트 테스트</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }}
.top-bar {{ background: #1a1d27; border-bottom: 1px solid #2d3148; padding: 16px 24px; display: flex; align-items: center; gap: 16px; }}
.top-bar h1 {{ font-size: 18px; font-weight: 700; color: #a78bfa; }}
.env-bar {{ font-size: 12px; color: #94a3b8; background: #161925; border: 1px solid #2d3148; border-radius: 6px; padding: 6px 12px; }}
.global-btns {{ margin-left: auto; display: flex; gap: 8px; }}
button {{ background: #6366f1; color: white; border: none; border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; transition: background .15s; }}
button:hover {{ background: #4f46e5; }}
button:disabled {{ background: #374151; color: #6b7280; cursor: not-allowed; }}
.run-all {{ background: #0f766e; font-size: 12px; padding: 4px 10px; }}
.run-all:hover {{ background: #0d9488; }}
main {{ padding: 24px; max-width: 1200px; margin: 0 auto; }}
section {{ margin-bottom: 32px; }}
.section-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
h2 {{ font-size: 15px; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: .05em; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 12px; }}
.card {{ background: #1a1d27; border: 1px solid #2d3148; border-radius: 10px; padding: 14px; transition: border-color .2s; }}
.card.running {{ border-color: #f59e0b; }}
.card.done {{ border-color: #10b981; }}
.card.error {{ border-color: #ef4444; }}
.card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.card-label {{ font-weight: 600; font-size: 14px; flex: 1; }}
.card-desc {{ font-size: 12px; color: #64748b; margin-bottom: 10px; line-height: 1.5; }}
.card-result {{ margin-top: 8px; }}
.badge {{ display: inline-block; font-size: 11px; font-weight: 600; border-radius: 4px; padding: 2px 7px; margin-right: 6px; }}
.badge.running {{ background: #78350f; color: #fbbf24; }}
.badge.done {{ background: #064e3b; color: #34d399; }}
.badge.error {{ background: #7f1d1d; color: #f87171; }}
.elapsed {{ font-size: 11px; color: #64748b; margin-right: 8px; }}
pre.result-box {{ background: #0d1117; border: 1px solid #2d3148; border-radius: 6px; padding: 10px; font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow-y: auto; margin-top: 8px; color: #a3e635; }}
pre.error-box {{ background: #1c0a0a; border: 1px solid #7f1d1d; border-radius: 6px; padding: 10px; font-size: 11px; white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; margin-top: 8px; color: #f87171; }}
</style>
</head>
<body>
<div class="top-bar">
    <h1>🤖 에이전트 테스트</h1>
    {env_info}
    <div class="global-btns">
        <button onclick="runAll()">전체 실행</button>
        <button onclick="clearAll()" style="background:#374151">결과 초기화</button>
    </div>
</div>
<main>{sections}</main>
<script>
function runScenario(sid) {{
    fetch('/run/' + sid, {{method: 'POST'}})
        .then(() => pollResult(sid));
}}
function runGroup(gid) {{
    fetch('/run_group/' + gid, {{method: 'POST'}})
        .then(r => r.json())
        .then(ids => ids.forEach(sid => pollResult(sid)));
}}
function runAll() {{
    fetch('/run_all', {{method: 'POST'}})
        .then(r => r.json())
        .then(ids => ids.forEach(sid => pollResult(sid)));
}}
function clearAll() {{
    fetch('/clear', {{method: 'POST'}}).then(() => location.reload());
}}
function pollResult(sid) {{
    const el = document.getElementById('result-' + sid);
    const card = document.getElementById('card-' + sid);
    let tries = 0;
    const iv = setInterval(() => {{
        fetch('/result/' + sid)
            .then(r => r.json())
            .then(data => {{
                el.innerHTML = data.html;
                card.className = 'card ' + (data.status || 'idle');
                if (data.status !== 'running' || ++tries > 300) clearInterval(iv);
            }});
    }}, 1500);
}}
</script>
</body>
</html>"""


# ── HTTP 핸들러 ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 로그 억제

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._html(_build_html())
        elif path.startswith("/result/"):
            sid = path[len("/result/"):]
            with _lock:
                r = _results.get(sid)
            status = r["status"] if r else "idle"
            self._json({"status": status, "html": _render_result(sid)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path.startswith("/run/"):
            sid = path[len("/run/"):]
            scenario = next((s for s in SCENARIOS if s["id"] == sid), None)
            if not scenario:
                self._json({"error": "not found"}, 404)
                return
            threading.Thread(target=_run_scenario, args=(scenario,), daemon=True).start()
            self._json({"status": "started"})

        elif path.startswith("/run_group/"):
            gid = path[len("/run_group/"):]
            group = [s for s in SCENARIOS if s["group"] == gid]
            ids = [s["id"] for s in group]
            for s in group:
                threading.Thread(target=_run_scenario, args=(s,), daemon=True).start()
            self._json(ids)

        elif path == "/run_all":
            ids = [s["id"] for s in SCENARIOS]
            for s in SCENARIOS:
                threading.Thread(target=_run_scenario, args=(s,), daemon=True).start()
            self._json(ids)

        elif path == "/clear":
            with _lock:
                _results.clear()
            self._json({"status": "cleared"})

        else:
            self.send_response(404)
            self.end_headers()


# ── 진입점 ───────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"테스트 UI: http://localhost:{PORT}")
    print(f"모델: {os.environ.get('MODEL_NAME','(MODEL_NAME 미설정)')}")
    print(f"서버: {os.environ.get('MODEL_SERVER','(MODEL_SERVER 미설정)')}")
    print("Ctrl+C로 종료\n")
    server = HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
