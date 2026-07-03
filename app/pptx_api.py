"""
PPTX generation API.

Deck generation uses a two-step workflow:
  1. POST /api/v1/pptx/plan uploads a template and returns a reviewable outline.
  2. POST /api/v1/pptx/jobs/{job_id}/generate renders the approved outline.

POST /generate remains as a convenience shortcut that performs both steps.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from config import PPTX_MAX_UPLOAD_MB
from pptx_service import (
    PptxGenerateRequest,
    PptxPlanRequest,
    get_default_service,
    result_to_dict,
)


router = APIRouter(prefix="/api/v1/pptx", tags=["pptx"])
_service = get_default_service()


def _validate_upload(file: UploadFile) -> None:
    filename = file.filename or ""
    if not filename.lower().endswith(".pptx"):
        raise HTTPException(400, "template_file은 .pptx 파일이어야 합니다.")


async def _save_upload_to_temp(template_file: UploadFile) -> Path:
    _validate_upload(template_file)
    max_bytes = PPTX_MAX_UPLOAD_MB * 1024 * 1024
    suffix = Path(template_file.filename or "template.pptx").suffix or ".pptx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while True:
            chunk = await template_file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(413, f"업로드 최대 크기({PPTX_MAX_UPLOAD_MB}MB)를 초과했습니다.")
            tmp.write(chunk)
    return tmp_path


def _validate_strictness(strictness: str) -> None:
    if strictness not in ("strict", "balanced", "flexible"):
        raise HTTPException(400, "strictness는 strict, balanced, flexible 중 하나여야 합니다.")


@router.post("/plan")
async def plan_pptx(
    template_file: UploadFile = File(...),
    content: str = Form(...),
    instruction: str = Form(""),
    output_filename: str = Form("generated_deck.pptx"),
    slide_count: int | None = Form(None),
    purpose: str = Form("general"),
    strictness: str = Form("strict"),
):
    _validate_strictness(strictness)
    tmp_path = await _save_upload_to_temp(template_file)
    try:
        result = _service.create_plan(
            PptxPlanRequest(
                template_path=tmp_path,
                content=content,
                instruction=instruction,
                output_filename=output_filename,
                slide_count=slide_count,
                purpose=purpose,
                strictness=strictness,  # type: ignore[arg-type]
            )
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"task": "pptx_plan_from_template", "result": result}


@router.post("/jobs/{job_id}/generate")
def generate_pptx_from_plan(
    job_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
):
    try:
        result = _service.generate(
            PptxGenerateRequest(
                job_id=job_id,
                plan=payload.get("plan"),
                output_filename=payload.get("output_filename"),
            )
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    data = result_to_dict(result)
    data["download_url"] = f"/api/v1/pptx/files/{result.job_id}/{result.file_name}"
    return {"task": "pptx_generate_from_template", "result": data}


@router.post("/generate")
async def generate_pptx_shortcut(
    template_file: UploadFile = File(...),
    content: str = Form(...),
    instruction: str = Form(""),
    output_filename: str = Form("generated_deck.pptx"),
    slide_count: int | None = Form(None),
    purpose: str = Form("general"),
    strictness: str = Form("strict"),
):
    """Compatibility shortcut: create a plan and immediately render it."""
    _validate_strictness(strictness)
    tmp_path = await _save_upload_to_temp(template_file)
    try:
        planned = _service.create_plan(
            PptxPlanRequest(
                template_path=tmp_path,
                content=content,
                instruction=instruction,
                output_filename=output_filename,
                slide_count=slide_count,
                purpose=purpose,
                strictness=strictness,  # type: ignore[arg-type]
            )
        )
        result = _service.generate(PptxGenerateRequest(job_id=planned["job_id"]))
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)

    data = result_to_dict(result)
    data["download_url"] = f"/api/v1/pptx/files/{result.job_id}/{result.file_name}"
    return {"task": "pptx_generate_from_template", "result": data}


@router.get("/jobs/{job_id}")
def read_job(job_id: str):
    job = _service.read_job(job_id)
    if not job:
        raise HTTPException(404, "job을 찾을 수 없습니다.")
    return job


@router.get("/files/{job_id}/{filename}")
def download_file(job_id: str, filename: str):
    path = _service.output_path(job_id, filename)
    if not path:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=path.name,
    )
