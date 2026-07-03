"""
태스크별 command.md 템플릿 관리.
실제 운영 시 템플릿을 파일이나 DB로 분리해서 버전 관리할 수 있습니다.
"""

from string import Template


_TEMPLATES: dict[str, str] = {

    "classify": """\
# TASK
classify

# INSTRUCTION
전달된 문서를 읽고 아래 JSON 스키마로만 응답하라. JSON 외 어떤 텍스트도 출력하지 마라.

# OUTPUT_FORMAT
```json
{
  "node_path": "대분류/소분류",
  "summary": "한 문단 요약",
  "keywords": ["키워드1", "키워드2"],
  "sample_questions": ["질문1", "질문2"],
  "classification_reason": "분류 근거",
  "confidence_score": 0.0
}
```

# CONTEXT
$context
""",

    "qna": """\
# TASK
qna

# INSTRUCTION
전달된 문서들을 근거로 아래 질문에 답하라.
문서에 없는 내용은 "문서에서 확인할 수 없습니다"라고 답하라.

# QUESTION
$question

# CONTEXT
$context
""",

    "summarize": """\
# TASK
summarize

# INSTRUCTION
전달된 문서를 읽고 아래 조건에 맞게 요약하라.
- 분량: $length
- 형식: $format

# CONTEXT
$context
""",

    "translate": """\
# TASK
translate

# INSTRUCTION
전달된 문서를 $target_lang 로 번역하라.
원문의 구조와 형식을 유지하고, 전문 용어는 원어를 병기하라.

# CONTEXT
$context
""",

}


def get(task: str, **kwargs) -> str:
    """
    태스크에 맞는 command.md 텍스트를 반환합니다.

    Args:
        task: "classify" | "qna" | "summarize" | "translate"
        **kwargs: 템플릿 변수 (question, length, format, target_lang, context 등)

    Returns:
        완성된 command.md 문자열
    """
    if task not in _TEMPLATES:
        raise ValueError(f"지원하지 않는 태스크: {task}. 가능한 태스크: {list(_TEMPLATES)}")

    kwargs.setdefault("context", "")
    return Template(_TEMPLATES[task]).safe_substitute(**kwargs)
