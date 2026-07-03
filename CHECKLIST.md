# 폐쇄망 반입 체크리스트

## 반입 전 확인

- [ ] `app/.env` 생성 — MODEL_NAME, MODEL_SERVER, MODEL_API_KEY 설정
- [ ] vLLM 서버 주소 확인 (기본: http://localhost:8000/v1)
- [ ] Python 3.9 이상 확인
- [ ] pip 패키지 설치 (`requirements_check.txt` 참고)

## 필수 패키지

```
fastapi
uvicorn
pydantic
python-dotenv
requests
```

## 반입 후 검증 순서

1. API 서버 기동:
   ```bash
   cd app/
   python3 agent_api.py
   ```

2. 헬스체크:
   ```bash
   curl http://localhost:8100/api/v1/health
   # → {"status": "ok", "service": "qwen-agent-api"}
   ```

3. 전체 기능 테스트:
   ```bash
   cd test_package/
   python3 run_test.py all
   # 결과: test_package/output/ 폴더에 저장
   ```

4. 결과 확인 항목:
   - `result_classify_*.txt` — node_path, confidence_score 포함 여부
   - `result_qna.txt` — 출처 문서명 포함 여부
   - `result_summarize.txt` — 항목별 정리 여부

## 반입 파일 목록

```
dist/
├── app/
│   ├── agent_api.py
│   ├── agent_core.py
│   ├── config.py
│   ├── events.py
│   ├── prompt_manager.py
│   ├── cli_chat.py
│   ├── .env.example
│   └── qwen_agent/          ← vendored 라이브러리
├── test_package/
│   ├── run_test.py
│   ├── docs/                ← 샘플 문서 (검증용)
│   └── commands/            ← 태스크별 커맨드 MD
├── README.md
├── CHECKLIST.md             ← 이 파일
└── requirements_check.txt
```
