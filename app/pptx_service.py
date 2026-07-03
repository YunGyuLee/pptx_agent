"""
PPTX generation service.

Workflow:
  1. plan: upload a corporate PPTX template and rough content, then create a
     reviewable slide outline.
  2. generate: accept the approved/edited outline and render a PPTX by copying
     the template and replacing text inside inherited slides.

The implementation is intentionally class-based. Each class owns one concern so
planning, layout mapping, rendering, storage, and QA can evolve independently.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from config import PPTX_WORKSPACE_DIR
from events import EventBus, cli_handler


PptxStrictness = Literal["strict", "balanced", "flexible"]

_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("p", _P_NS)
ET.register_namespace("a", _A_NS)
ET.register_namespace("r", _R_NS)


@dataclass(frozen=True)
class PptxPlanRequest:
    template_path: Path
    content: str
    instruction: str = ""
    output_filename: str = "generated_deck.pptx"
    slide_count: int | None = None
    purpose: str = "general"
    strictness: PptxStrictness = "strict"


@dataclass(frozen=True)
class PptxGenerateRequest:
    job_id: str
    plan: dict | None = None
    output_filename: str | None = None


@dataclass(frozen=True)
class PptxGenerateResult:
    job_id: str
    status: str
    file_name: str
    file_path: str
    slide_count: int
    template_used: str
    plan: dict
    template_profile: dict
    warnings: list[str]


@dataclass(frozen=True)
class PptxSlidePlan:
    slide_no: int
    layout_role: str
    title: str
    bullets: list[str] = field(default_factory=list)
    speaker_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PptxDeckPlan:
    title: str
    purpose: str
    strictness: PptxStrictness
    slides: list[PptxSlidePlan]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "purpose": self.purpose,
            "strictness": self.strictness,
            "slides": [slide.to_dict() for slide in self.slides],
        }


class PptxFilenamePolicy:
    """Sanitize user-provided artifact names for local file storage."""

    @staticmethod
    def safe_pptx_name(name: str, default: str) -> str:
        cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " ")).strip()
        cleaned = cleaned.replace(" ", "_")
        if not cleaned:
            cleaned = default
        if not cleaned.lower().endswith(".pptx"):
            cleaned += ".pptx"
        return cleaned


class PptxTemplateInspector:
    """Extract lightweight template information without external dependencies."""

    def inspect(self, template_path: Path) -> dict:
        profile: dict = {
            "file_name": template_path.name,
            "slide_count": 0,
            "slide_size": {},
            "layouts": [],
            "theme_files": [],
        }
        with zipfile.ZipFile(template_path) as zf:
            names = zf.namelist()
            slide_files = [
                n for n in names
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            ]
            layout_files = [
                n for n in names
                if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml")
            ]
            sorted_slide_files = sorted(slide_files, key=self._slide_number)
            profile["slide_count"] = len(sorted_slide_files)
            profile["slide_files"] = sorted_slide_files
            profile["slides"] = self._read_slides(zf, sorted_slide_files)
            profile["theme_files"] = sorted(
                n for n in names if n.startswith("ppt/theme/") and n.endswith(".xml")
            )
            profile["slide_size"] = self._read_slide_size(zf)
            profile["layouts"] = self._read_layouts(zf, layout_files)
        return profile

    def _read_xml(self, zf: zipfile.ZipFile, name: str) -> ET.Element | None:
        try:
            return ET.fromstring(zf.read(name))
        except KeyError:
            return None

    def _slide_number(self, name: str) -> int:
        match = re.search(r"slide(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0

    def _read_slide_size(self, zf: zipfile.ZipFile) -> dict:
        pres = self._read_xml(zf, "ppt/presentation.xml")
        if pres is None:
            return {}
        sld_sz = pres.find(f".//{{{_P_NS}}}sldSz")
        if sld_sz is None:
            return {}
        return {
            "cx": sld_sz.attrib.get("cx"),
            "cy": sld_sz.attrib.get("cy"),
            "type": sld_sz.attrib.get("type", ""),
        }

    def _read_slides(self, zf: zipfile.ZipFile, slide_files: list[str]) -> list[dict]:
        slides = []
        for idx, slide_file in enumerate(slide_files, start=1):
            root = self._read_xml(zf, slide_file)
            texts = []
            has_table = False
            if root is not None:
                texts = [node.text or "" for node in root.findall(f".//{{{_A_NS}}}t") if node.text]
                has_table = root.find(f".//{{{_A_NS}}}tbl") is not None
            preview = " ".join(texts)[:160]
            slides.append({
                "slide_no": idx,
                "file": slide_file,
                "role": self._infer_slide_role(idx, texts, has_table),
                "text_node_count": len(texts),
                "text_preview": preview,
                "has_table": has_table,
            })
        return slides

    def _infer_slide_role(self, slide_no: int, texts: list[str], has_table: bool) -> str:
        joined = " ".join(texts).lower()
        if "[role:cover]" in joined or "{{cover}}" in joined:
            return "cover"
        if "[role:agenda]" in joined or "{{agenda}}" in joined:
            return "agenda"
        if "[role:table]" in joined or "{{table}}" in joined:
            return "table"
        if "[role:summary]" in joined or "{{summary}}" in joined:
            return "summary"
        if slide_no == 1:
            return "cover"
        if any(word in joined for word in ("목차", "agenda", "contents", "순서")):
            return "agenda"
        if has_table or any(word in joined for word in ("표", "table", "현황", "리스트")):
            return "table"
        if any(word in joined for word in ("요약", "결론", "summary", "next step", "향후")):
            return "summary"
        return "content"

    def _read_layouts(self, zf: zipfile.ZipFile, layout_files: list[str]) -> list[dict]:
        layouts = []
        for layout_file in sorted(layout_files):
            root = self._read_xml(zf, layout_file)
            if root is None:
                continue
            c_sld = root.find(f".//{{{_P_NS}}}cSld")
            layout_name = c_sld.attrib.get("name", "") if c_sld is not None else ""
            placeholders = []
            for ph in root.findall(f".//{{{_P_NS}}}ph"):
                placeholders.append({
                    "type": ph.attrib.get("type", "body"),
                    "idx": ph.attrib.get("idx", ""),
                })
            layouts.append({
                "file": layout_file,
                "name": layout_name,
                "placeholders": placeholders,
            })
        return layouts


class PptxContentPlanner:
    """
    Creates a reviewable deck plan from rough text.

    This deterministic planner is an MVP. It can later be replaced by an LLM
    planner while keeping the same PptxDeckPlan contract.
    """

    def create_plan(self, req: PptxPlanRequest, profile: dict) -> PptxDeckPlan:
        lines = self._content_lines(req.content)
        title = self._derive_title(lines, req.instruction)
        desired = req.slide_count or self._default_slide_count(lines, profile)
        desired = max(1, min(desired, 30))

        slides: list[PptxSlidePlan] = []
        if desired == 1:
            slides.append(PptxSlidePlan(1, "cover", title, lines[:5], req.instruction))
            return PptxDeckPlan(title, req.purpose, req.strictness, slides)

        slides.append(PptxSlidePlan(1, "cover", title, [req.purpose] if req.purpose != "general" else []))
        body_slots = desired - 1
        chunks = self._chunk(lines, body_slots)
        roles = self._roles_for(body_slots)
        for idx, chunk in enumerate(chunks, start=2):
            slide_title = chunk[0] if chunk else f"주요 내용 {idx - 1}"
            bullets = chunk[1:6] if len(chunk) > 1 else []
            slides.append(PptxSlidePlan(idx, roles[idx - 2], slide_title, bullets))
        return PptxDeckPlan(title, req.purpose, req.strictness, slides)

    def _content_lines(self, content: str) -> list[str]:
        lines = []
        for raw in content.replace("\r", "").split("\n"):
            line = raw.strip().strip("-•* ").strip()
            if line:
                lines.append(line)
        if not lines and content.strip():
            sentences = re.split(r"(?<=[.!?。！？])\s+", content.strip())
            lines = [s.strip() for s in sentences if s.strip()]
        return lines or ["작성 내용 미입력"]

    def _derive_title(self, lines: list[str], instruction: str) -> str:
        if instruction.strip():
            first = instruction.strip().split("\n", 1)[0]
            return first[:60]
        return lines[0][:60]

    def _default_slide_count(self, lines: list[str], profile: dict) -> int:
        content_count = 1 + max(1, (len(lines) + 4) // 5)
        return min(12, content_count)

    def _chunk(self, lines: list[str], chunks: int) -> list[list[str]]:
        if chunks <= 0:
            return []
        size = max(1, (len(lines) + chunks - 1) // chunks)
        result = [lines[i:i + size] for i in range(0, len(lines), size)]
        while len(result) < chunks:
            result.append([])
        return result[:chunks]

    def _roles_for(self, count: int) -> list[str]:
        roles = ["agenda", "content", "content", "table", "summary"]
        while len(roles) < count:
            roles.insert(-1, "content")
        return roles[:count]


class PptxLayoutMapper:
    """Maps approved slide plans to representative template slides by role."""

    def map(self, plan: dict, profile: dict) -> list[dict]:
        catalog = profile.get("slides") or []
        if not catalog:
            return []
        planned_slides = plan.get("slides") or []
        mappings = []
        for idx, slide in enumerate(planned_slides, start=1):
            role = slide.get("layout_role", "content")
            source = self._select_source(role, catalog, idx)
            mappings.append({
                "slide_no": slide.get("slide_no", idx),
                "source_slide": source["file"],
                "source_role": source.get("role", "content"),
                "layout_role": role,
                "title": slide.get("title", ""),
                "bullets": slide.get("bullets", []),
            })
        return mappings

    def _select_source(self, role: str, catalog: list[dict], idx: int) -> dict:
        exact = [s for s in catalog if s.get("role") == role]
        if exact:
            return exact[(idx - 1) % len(exact)]
        if role != "content":
            content = [s for s in catalog if s.get("role") == "content"]
            if content:
                return content[(idx - 1) % len(content)]
        return catalog[(idx - 1) % len(catalog)]


class PptxJobStore:
    """Owns job directories, uploaded templates, plans, metadata, and outputs."""

    def __init__(self, root: Path = PPTX_WORKSPACE_DIR):
        self.root = root

    def create_job_id(self) -> str:
        return uuid.uuid4().hex

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def upload_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "uploads"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "outputs"

    def prepare(self, job_id: str) -> None:
        self.upload_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.output_dir(job_id).mkdir(parents=True, exist_ok=True)

    def store_template(self, job_id: str, template_path: Path) -> Path:
        filename = PptxFilenamePolicy.safe_pptx_name(template_path.name, "template.pptx")
        stored = self.upload_dir(job_id) / filename
        if template_path.resolve() != stored.resolve():
            shutil.copy2(template_path, stored)
        return stored

    def template_path(self, job_id: str) -> Path | None:
        candidates = sorted(self.upload_dir(job_id).glob("*.pptx"))
        return candidates[0] if candidates else None

    def write_plan(self, job_id: str, req: PptxPlanRequest, profile: dict, plan: dict, warnings: list[str]) -> None:
        data = {
            "request": {
                "content": req.content,
                "instruction": req.instruction,
                "output_filename": req.output_filename,
                "slide_count": req.slide_count,
                "purpose": req.purpose,
                "strictness": req.strictness,
            },
            "template_profile": profile,
            "plan": plan,
            "warnings": warnings,
        }
        (self.job_dir(job_id) / "plan.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def read_plan_record(self, job_id: str) -> dict | None:
        path = self.job_dir(job_id) / "plan.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_generation_notes(self, job_id: str, result: PptxGenerateResult) -> None:
        (self.job_dir(job_id) / "generation_result.json").write_text(
            json.dumps(result_to_dict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def read_job(self, job_id: str) -> dict | None:
        record = self.read_plan_record(job_id)
        if not record:
            return None
        outputs = sorted(self.output_dir(job_id).glob("*.pptx"))
        return {
            "job_id": job_id,
            "status": "done" if outputs else "planned",
            "outputs": [p.name for p in outputs],
            "request": record.get("request", {}),
            "template_profile": record.get("template_profile", {}),
            "plan": record.get("plan", {}),
            "warnings": record.get("warnings", []),
        }

    def output_path(self, job_id: str, filename: str | None = None) -> Path | None:
        output_dir = self.output_dir(job_id)
        if not output_dir.exists():
            return None
        if filename:
            path = (output_dir / filename).resolve()
            if str(path).startswith(str(output_dir.resolve())) and path.exists():
                return path
            return None
        outputs = sorted(output_dir.glob("*.pptx"))
        return outputs[0] if outputs else None


class PptxPlaceholderRenderer:
    """
    Renders a new deck by cloning representative template slides.

    It preserves masters, layouts, themes, media, and per-slide relationships,
    while rebuilding the presentation slide list so the output contains exactly
    the approved plan slides.
    """

    def render(self, template_path: Path, output_path: Path, mappings: list[dict]) -> list[str]:
        warnings: list[str] = []
        with zipfile.ZipFile(template_path, "r") as src, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as dst:
            self._copy_non_slide_parts(src, dst)
            rels_root = self._presentation_rels_root(src)
            self._write_slides(src, dst, mappings, warnings)
            self._write_presentation(src, dst, mappings)
            self._write_presentation_rels(dst, rels_root, len(mappings))
            self._write_content_types(src, dst, len(mappings))
        return warnings

    def _copy_non_slide_parts(self, src: zipfile.ZipFile, dst: zipfile.ZipFile) -> None:
        for item in src.infolist():
            name = item.filename
            if name == "ppt/presentation.xml":
                continue
            if name == "ppt/_rels/presentation.xml.rels":
                continue
            if name == "[Content_Types].xml":
                continue
            if re.match(r"ppt/slides/slide\d+\.xml$", name):
                continue
            if re.match(r"ppt/slides/_rels/slide\d+\.xml\.rels$", name):
                continue
            dst.writestr(item, src.read(name))

    def _write_slides(self, src: zipfile.ZipFile, dst: zipfile.ZipFile, mappings: list[dict], warnings: list[str]) -> None:
        for idx, mapping in enumerate(mappings, start=1):
            source_slide = mapping["source_slide"]
            data = src.read(source_slide)
            data, slide_warnings = self._replace_slide_text(data, mapping)
            warnings.extend(slide_warnings)
            dst.writestr(f"ppt/slides/slide{idx}.xml", data)

            source_rels = self._slide_rels_name(source_slide)
            if source_rels in src.namelist():
                dst.writestr(f"ppt/slides/_rels/slide{idx}.xml.rels", src.read(source_rels))

    def _slide_rels_name(self, slide_name: str) -> str:
        file_name = slide_name.rsplit("/", 1)[-1]
        return f"ppt/slides/_rels/{file_name}.rels"

    def _replace_slide_text(self, xml_bytes: bytes, mapping: dict) -> tuple[bytes, list[str]]:
        warnings: list[str] = []
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            return xml_bytes, [f"{mapping['source_slide']} XML 파싱 실패: {e}"]

        text_nodes = root.findall(f".//{{{_A_NS}}}t")
        replacement_lines = self._replacement_lines(mapping)
        if not text_nodes:
            warnings.append(f"{mapping['source_slide']}에서 텍스트 노드를 찾지 못했습니다.")
            return xml_bytes, warnings

        for idx, node in enumerate(text_nodes):
            node.text = replacement_lines[idx] if idx < len(replacement_lines) else ""
        if len(replacement_lines) > len(text_nodes):
            warnings.append(
                f"{mapping['source_slide']} 텍스트 영역 부족: {len(replacement_lines) - len(text_nodes)}개 항목 미반영"
            )
        return ET.tostring(root, encoding="utf-8", xml_declaration=True), warnings

    def _replacement_lines(self, mapping: dict) -> list[str]:
        lines = [str(mapping.get("title") or "")]
        for bullet in mapping.get("bullets") or []:
            lines.append(str(bullet))
        return lines

    def _presentation_rels_root(self, src: zipfile.ZipFile) -> ET.Element:
        try:
            return ET.fromstring(src.read("ppt/_rels/presentation.xml.rels"))
        except KeyError:
            return ET.Element(f"{{{_REL_NS}}}Relationships")

    def _write_presentation_rels(self, dst: zipfile.ZipFile, rels_root: ET.Element, slide_count: int) -> None:
        for rel in list(rels_root):
            if rel.attrib.get("Type", "").endswith("/slide"):
                rels_root.remove(rel)
        used_ids = {rel.attrib.get("Id", "") for rel in rels_root}
        for idx in range(1, slide_count + 1):
            rid = f"rId{1000 + idx}"
            while rid in used_ids:
                rid = f"rId{uuid.uuid4().hex[:8]}"
            used_ids.add(rid)
            ET.SubElement(rels_root, f"{{{_REL_NS}}}Relationship", {
                "Id": rid,
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
                "Target": f"slides/slide{idx}.xml",
            })
        dst.writestr("ppt/_rels/presentation.xml.rels", ET.tostring(rels_root, encoding="utf-8", xml_declaration=True))

    def _write_presentation(self, src: zipfile.ZipFile, dst: zipfile.ZipFile, mappings: list[dict]) -> None:
        try:
            root = ET.fromstring(src.read("ppt/presentation.xml"))
        except KeyError:
            root = ET.Element(f"{{{_P_NS}}}presentation")
        sld_id_lst = root.find(f".//{{{_P_NS}}}sldIdLst")
        if sld_id_lst is None:
            sld_id_lst = ET.SubElement(root, f"{{{_P_NS}}}sldIdLst")
        for child in list(sld_id_lst):
            sld_id_lst.remove(child)
        for idx, _mapping in enumerate(mappings, start=1):
            ET.SubElement(sld_id_lst, f"{{{_P_NS}}}sldId", {
                "id": str(255 + idx),
                f"{{{_R_NS}}}id": f"rId{1000 + idx}",
            })
        dst.writestr("ppt/presentation.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))

    def _write_content_types(self, src: zipfile.ZipFile, dst: zipfile.ZipFile, slide_count: int) -> None:
        try:
            root = ET.fromstring(src.read("[Content_Types].xml"))
        except KeyError:
            root = ET.Element(f"{{{_CT_NS}}}Types")
        for child in list(root):
            if child.attrib.get("PartName", "").startswith("/ppt/slides/slide"):
                root.remove(child)
        for idx in range(1, slide_count + 1):
            ET.SubElement(root, f"{{{_CT_NS}}}Override", {
                "PartName": f"/ppt/slides/slide{idx}.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
            })
        dst.writestr("[Content_Types].xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))


class PptxQualityChecker:
    """MVP structural QA. Visual/render QA can be added behind this interface."""

    def check_plan(self, req: PptxPlanRequest, profile: dict, plan: dict) -> list[str]:
        warnings: list[str] = []
        template_slide_count = int(profile.get("slide_count") or 0)
        planned_count = len(plan.get("slides") or [])
        if template_slide_count == 0:
            warnings.append("템플릿에서 슬라이드를 찾지 못했습니다.")
        if not profile.get("layouts"):
            warnings.append("템플릿 레이아웃 정보를 찾지 못했습니다.")
        if not req.content.strip():
            warnings.append("작성할 내용이 비어 있습니다.")
        if template_slide_count > 0 and planned_count > template_slide_count:
            warnings.append("계획된 슬라이드 수가 템플릿 슬라이드 수보다 많아 대표 슬라이드를 재사용합니다.")
        return warnings

    def check_generation(self, plan: dict, mappings: list[dict], render_warnings: list[str]) -> list[str]:
        warnings = list(render_warnings)
        planned_count = len(plan.get("slides") or [])
        if len(mappings) < planned_count:
            warnings.append(f"템플릿 슬라이드 부족으로 {planned_count - len(mappings)}개 계획 슬라이드를 렌더링하지 못했습니다.")
        return warnings


class PptxGenerationService:
    """Coordinates planning, mapping, rendering, metadata, and QA."""

    def __init__(
        self,
        store: PptxJobStore | None = None,
        inspector: PptxTemplateInspector | None = None,
        planner: PptxContentPlanner | None = None,
        mapper: PptxLayoutMapper | None = None,
        renderer: PptxPlaceholderRenderer | None = None,
        quality_checker: PptxQualityChecker | None = None,
    ):
        self.store = store or PptxJobStore()
        self.inspector = inspector or PptxTemplateInspector()
        self.planner = planner or PptxContentPlanner()
        self.mapper = mapper or PptxLayoutMapper()
        self.renderer = renderer or PptxPlaceholderRenderer()
        self.quality_checker = quality_checker or PptxQualityChecker()

    def create_plan(self, req: PptxPlanRequest, bus: EventBus | None = None) -> dict:
        bus = bus or EventBus(handler=cli_handler)
        self._validate_plan_request(req)

        job_id = self.store.create_job_id()
        self.store.prepare(job_id)

        bus.phase("PPTX 템플릿 분석 중...")
        stored_template = self.store.store_template(job_id, req.template_path)
        profile = self.inspector.inspect(stored_template)

        bus.phase("슬라이드 구성안 생성 중...")
        plan = self.planner.create_plan(req, profile).to_dict()
        warnings = self.quality_checker.check_plan(req, profile, plan)
        self.store.write_plan(job_id, req, profile, plan, warnings)
        bus.done()

        return {
            "job_id": job_id,
            "status": "planned",
            "template_profile": profile,
            "plan": plan,
            "warnings": warnings,
        }

    def generate(self, req: PptxGenerateRequest, bus: EventBus | None = None) -> PptxGenerateResult:
        bus = bus or EventBus(handler=cli_handler)
        record = self.store.read_plan_record(req.job_id)
        if not record:
            raise ValueError("job을 찾을 수 없습니다. 먼저 /plan을 호출하세요.")
        template_path = self.store.template_path(req.job_id)
        if not template_path:
            raise ValueError("job에 저장된 템플릿 파일이 없습니다.")

        plan = req.plan or record.get("plan") or {}
        profile = record.get("template_profile") or self.inspector.inspect(template_path)
        output_name = PptxFilenamePolicy.safe_pptx_name(
            req.output_filename or record.get("request", {}).get("output_filename") or "generated_deck.pptx",
            "generated_deck.pptx",
        )
        output_path = self.store.output_dir(req.job_id) / output_name

        bus.phase("승인된 구성안을 템플릿 슬라이드에 매핑 중...")
        mappings = self.mapper.map(plan, profile)
        bus.phase("템플릿 슬라이드 텍스트 치환 중...")
        render_warnings = self.renderer.render(template_path, output_path, mappings)
        warnings = self.quality_checker.check_generation(plan, mappings, render_warnings)
        bus.done()

        result = PptxGenerateResult(
            job_id=req.job_id,
            status="done",
            file_name=output_name,
            file_path=str(output_path),
            slide_count=len(mappings),
            template_used=template_path.name,
            plan=plan,
            template_profile=profile,
            warnings=warnings,
        )
        self.store.write_generation_notes(req.job_id, result)
        return result

    def read_job(self, job_id: str) -> dict | None:
        return self.store.read_job(job_id)

    def output_path(self, job_id: str, filename: str | None = None) -> Path | None:
        return self.store.output_path(job_id, filename)

    def _validate_plan_request(self, req: PptxPlanRequest) -> None:
        if req.template_path.suffix.lower() != ".pptx":
            raise ValueError("template_file은 .pptx 파일이어야 합니다.")
        if not req.template_path.exists():
            raise FileNotFoundError(str(req.template_path))
        if req.strictness not in ("strict", "balanced", "flexible"):
            raise ValueError("strictness는 strict, balanced, flexible 중 하나여야 합니다.")


_default_service = PptxGenerationService()


def get_default_service() -> PptxGenerationService:
    return _default_service


def result_to_dict(result: PptxGenerateResult) -> dict:
    return asdict(result)
