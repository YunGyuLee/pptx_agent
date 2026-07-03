# TASK
classify

# INSTRUCTION
전달된 문서 전체를 읽고 내용, 구조, 맥락을 파악하여 분류하라.
아래 JSON 스키마로만 응답하라. JSON 외 어떤 텍스트도 출력하지 마라.

# OUTPUT_FORMAT
```json
{
  "node_path": "대분류/소분류/세부분류",
  "summary": "문서 전체를 한 문단으로 요약",
  "keywords": ["핵심키워드1", "핵심키워드2"],
  "sample_questions": ["이 문서로 답할 수 있는 질문1", "질문2"],
  "classification_reason": "이 분류를 선택한 이유",
  "confidence_score": 0.0
}
```

# RULES
- node_path는 전달된 문서 내용에 근거하여 결정하라
- keywords는 최대 15개
- sample_questions는 최대 10개
- confidence_score는 0.0~1.0 사이 실수
