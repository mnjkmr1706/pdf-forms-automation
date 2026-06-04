#!/usr/bin/env python
"""
Convert a non-editable, printed PDF form into an editable AcroForm PDF.

This script preserves the original PDF page content and overlays AcroForm
fields where it detects likely fillable areas: table cells, underlines, and
printed checkbox squares.

Usage:
    python pdf_to_acroform.py input.pdf output.pdf
    python pdf_to_acroform.py input.pdf output.pdf --debug-json fields.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject
from reportlab.lib.colors import black
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h


@dataclass(frozen=True)
class TextRun:
    text: str
    x: float
    y: float
    size: float

    @property
    def x2(self) -> float:
        try:
            return self.x + stringWidth(self.text, "Helvetica", self.size)
        except Exception:
            return self.x + len(self.text) * self.size * 0.5


@dataclass
class FieldSpec:
    page: int
    kind: str
    name: str
    x: float
    y: float
    w: float
    h: float
    multiline: bool = False
    source: str = "geometry"


def round_rect(values) -> Rect:
    x, y, w, h = (float(v) for v in values[:4])
    return Rect(round(x, 2), round(y, 2), round(w, 2), round(h, 2))


def extract_rects(page) -> list[Rect]:
    rects: list[Rect] = []

    def before(op, args, cm, tm):
        op = op.decode() if isinstance(op, bytes) else op
        if op == "re" and len(args) >= 4:
            rects.append(round_rect(args))

    page.extract_text(visitor_operand_before=before)
    seen = set()
    unique: list[Rect] = []
    for rect in rects:
        key = (rect.x, rect.y, rect.w, rect.h)
        if key not in seen:
            seen.add(key)
            unique.append(rect)
    return unique


def extract_text_runs(page) -> list[TextRun]:
    runs: list[TextRun] = []

    def visitor(text, cm, tm, font, size):
        text = text.replace("\n", " ").strip()
        if text:
            runs.append(TextRun(text=text, x=float(tm[4]), y=float(tm[5]), size=float(size)))

    page.extract_text(visitor_text=visitor)
    return runs


def text_in_rect(runs: list[TextRun], rect: Rect, y_pad: float = 4) -> list[TextRun]:
    return [
        run
        for run in runs
        if rect.x - 2 <= run.x <= rect.x2 + 2
        and rect.y - y_pad <= run.y <= rect.y2 + y_pad
    ]


def nearby_text(runs: list[TextRun], field: FieldSpec, radius: float = 28) -> str:
    x1 = field.x - radius
    x2 = field.x + field.w + radius
    y1 = field.y - radius
    y2 = field.y + field.h + radius
    selected = [
        run
        for run in runs
        if x1 <= run.x <= x2 and y1 <= run.y <= y2
    ]
    selected = sorted(selected, key=lambda run: (-run.y, run.x))
    text = " ".join(run.text for run in selected)
    return re.sub(r"\s+", " ", text).strip()[:300]


def grouped_text_lines(runs: list[TextRun], tolerance: float = 3.0) -> list[list[TextRun]]:
    groups: list[list[TextRun]] = []
    for run in sorted(runs, key=lambda item: (-item.y, item.x)):
        for group in groups:
            if abs(group[0].y - run.y) <= tolerance:
                group.append(run)
                break
        else:
            groups.append([run])
    return [sorted(group, key=lambda item: item.x) for group in groups]


def row_text(runs: list[TextRun], y: float, tolerance: float = 16.0) -> str:
    selected = [run for run in runs if abs(run.y - y) <= tolerance]
    selected = sorted(selected, key=lambda run: (-run.y, run.x))
    return re.sub(r"\s+", " ", " ".join(run.text for run in selected)).strip()[:600]


def label_slug(text: str, fallback: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = re.sub(r"_+", "_", text)
    return text[:50] or fallback


def overlaps(a: FieldSpec, b: FieldSpec, pad: float = 1.0) -> bool:
    x_overlap = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    y_overlap = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if x_overlap <= pad or y_overlap <= pad:
        return False
    intersection = x_overlap * y_overlap
    smaller = min(a.w * a.h, b.w * b.h)
    return smaller > 0 and (intersection / smaller) > 0.35


def dedupe_fields(fields: list[FieldSpec]) -> list[FieldSpec]:
    def priority(field: FieldSpec) -> tuple:
        source_rank = {
            "checkbox": 0,
            "azure_checkbox": 0,
            "label": 1,
            "azure_label": 2,
            "response_box": 3,
            "cell_label": 4,
            "empty_cell": 5,
            "line": 6,
            "geometry": 7,
        }.get(field.source, 6)
        area = field.w * field.h
        # For text fields, smaller label-derived rectangles are usually better
        # than full table rows. For checkboxes, exact small boxes are best.
        size_rank = area if field.kind == "text" else abs(area - 81)
        return (field.page, field.kind != "checkbox", source_rank, size_rank)

    kept: list[FieldSpec] = []
    for field in sorted(fields, key=priority):
        duplicate = False
        for existing in kept:
            if existing.page == field.page and existing.kind == field.kind and overlaps(existing, field):
                duplicate = True
                break
        if not duplicate:
            kept.append(field)
    return sorted(kept, key=lambda f: (f.page, -f.y, f.x, f.kind))


def load_env_value(var_name: str, env_path: Path | None = None) -> str:
    key = os.environ.get(var_name, "").strip()
    if key:
        return key

    paths = []
    if env_path:
        paths.append(env_path)
    paths.extend([Path("work/.env"), Path(".env")])

    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            found_name, value = line.split("=", 1)
            if found_name.strip() == var_name:
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError(f"{var_name} was not found in the environment or a local .env file.")


def load_api_key(env_path: Path | None = None) -> str:
    return load_env_value("OPENAI_API_KEY", env_path)


def page_sizes(reader: PdfReader) -> list[dict[str, float]]:
    return [
        {"page": i + 1, "width": float(page.mediabox.width), "height": float(page.mediabox.height)}
        for i, page in enumerate(reader.pages)
    ]


def load_azure_config(env_path: Path | None = None) -> tuple[str, str, str]:
    endpoint = load_env_value("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", env_path).rstrip("/")
    key = load_env_value("AZURE_DOCUMENT_INTELLIGENCE_KEY", env_path)
    try:
        api_version = load_env_value("AZURE_DOCUMENT_INTELLIGENCE_API_VERSION", env_path)
    except RuntimeError:
        api_version = "2024-11-30"
    return endpoint, key, api_version


def analyze_with_azure_document_intelligence(
    input_pdf: Path,
    env_path: Path | None = None,
    timeout_seconds: int = 180,
) -> dict:
    endpoint, key, api_version = load_azure_config(env_path)
    query = urlencode({"api-version": api_version})
    url = f"{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze?{query}"
    req = Request(
        url,
        data=input_pdf.read_bytes(),
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/pdf",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as response:
            operation_url = response.headers.get("Operation-Location")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Azure Document Intelligence request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Azure Document Intelligence request failed: {exc.reason}") from exc

    if not operation_url:
        raise RuntimeError("Azure Document Intelligence did not return an Operation-Location header.")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        poll_req = Request(
            operation_url,
            headers={"Ocp-Apim-Subscription-Key": key},
            method="GET",
        )
        try:
            with urlopen(poll_req, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Azure Document Intelligence poll failed with HTTP {exc.code}: {detail}") from exc

        status = str(payload.get("status", "")).lower()
        if status == "succeeded":
            return payload
        if status == "failed":
            raise RuntimeError(f"Azure Document Intelligence analysis failed: {payload}")
        time.sleep(2)

    raise RuntimeError("Azure Document Intelligence analysis timed out.")


def polygon_bounds(polygon) -> tuple[float, float, float, float] | None:
    if not polygon:
        return None
    points: list[tuple[float, float]] = []
    if all(isinstance(item, (int, float)) for item in polygon):
        nums = [float(item) for item in polygon]
        points = list(zip(nums[0::2], nums[1::2]))
    elif all(isinstance(item, dict) for item in polygon):
        points = [(float(item["x"]), float(item["y"])) for item in polygon if "x" in item and "y" in item]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def azure_rect_to_pdf(
    bounds: tuple[float, float, float, float],
    azure_page: dict,
    pdf_page,
) -> tuple[float, float, float, float] | None:
    az_w = float(azure_page.get("width") or 0)
    az_h = float(azure_page.get("height") or 0)
    if az_w <= 0 or az_h <= 0:
        return None
    pdf_w = float(pdf_page.mediabox.width)
    pdf_h = float(pdf_page.mediabox.height)
    x1, y1, x2, y2 = bounds
    sx = pdf_w / az_w
    sy = pdf_h / az_h
    return (
        round(x1 * sx, 2),
        round(pdf_h - y2 * sy, 2),
        round((x2 - x1) * sx, 2),
        round((y2 - y1) * sy, 2),
    )


def infer_fields_from_azure(reader: PdfReader, azure_result: dict) -> list[FieldSpec]:
    result = azure_result.get("analyzeResult", azure_result)
    pages = result.get("pages") or []
    specs: list[FieldSpec] = []
    counters: dict[str, int] = {}

    def next_name(page_num: int, prefix: str, label: str | None = None) -> str:
        base = label_slug(label or "", f"{prefix}_{page_num}")
        if not base.startswith(prefix):
            base = f"{prefix}_{base}"
        counters[base] = counters.get(base, 0) + 1
        return base if counters[base] == 1 else f"{base}_{counters[base]}"

    for azure_page in pages:
        page_num = int(azure_page.get("pageNumber") or azure_page.get("page_number") or 0)
        if page_num < 1 or page_num > len(reader.pages):
            continue
        pdf_page = reader.pages[page_num - 1]

        for mark in azure_page.get("selectionMarks") or azure_page.get("selection_marks") or []:
            bounds = polygon_bounds(mark.get("polygon"))
            if not bounds:
                continue
            rect = azure_rect_to_pdf(bounds, azure_page, pdf_page)
            if not rect:
                continue
            x, y, w, h = rect
            if 4 <= w <= 30 and 4 <= h <= 30:
                specs.append(
                    FieldSpec(
                        page=page_num,
                        kind="checkbox",
                        name=next_name(page_num, "azure_checkbox"),
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                        source="azure_checkbox",
                    )
                )

        for line in azure_page.get("lines") or []:
            content = str(line.get("content") or "").strip()
            if ":" not in content:
                continue
            bounds = polygon_bounds(line.get("polygon"))
            if not bounds:
                continue
            rect = azure_rect_to_pdf(bounds, azure_page, pdf_page)
            if not rect:
                continue
            x, y, w, h = rect
            for match in re.finditer(r":", content):
                label = content[: match.end()]
                # Approximate the colon position within this OCR line.
                ratio = min(0.92, max(0.05, match.end() / max(1, len(content))))
                start_x = x + w * ratio + 14
                end_x = float(pdf_page.mediabox.width) - 36
                if end_x - start_x >= 35:
                    specs.append(
                        FieldSpec(
                            page=page_num,
                            kind="text",
                            name=next_name(page_num, "azure_field", label),
                            x=round(start_x, 2),
                            y=round(y - 2, 2),
                            w=round(end_x - start_x, 2),
                            h=max(12, round(h + 6, 2)),
                            source="azure_label",
                        )
                    )
                break

    return dedupe_fields(specs)


def field_from_dict(item: dict, fallback_name: str) -> FieldSpec | None:
    try:
        page = int(item["page"])
        kind = str(item["kind"]).lower()
        if kind not in {"text", "checkbox"}:
            return None
        rect = item.get("rect") or item.get("bbox")
        if not isinstance(rect, list) or len(rect) != 4:
            return None
        x1, y1, x2, y2 = [float(v) for v in rect]
        if x2 <= x1 or y2 <= y1:
            return None
        name = label_slug(str(item.get("name") or fallback_name), fallback_name)
        multiline = bool(item.get("multiline", False))
        return FieldSpec(page=page, kind=kind, name=name, x=x1, y=y1, w=x2 - x1, h=y2 - y1, multiline=multiline)
    except Exception:
        return None


def field_from_candidate(item: dict, candidates: list[FieldSpec], fallback_name: str) -> FieldSpec | None:
    try:
        candidate_id = int(item["candidate_id"])
        if candidate_id < 1 or candidate_id > len(candidates):
            return None
        source = candidates[candidate_id - 1]
        kind = source.kind
        if kind not in {"text", "checkbox"}:
            return None
        name = label_slug(str(item.get("name") or fallback_name), fallback_name)
        multiline = bool(item.get("multiline", source.multiline))
        return FieldSpec(
            page=source.page,
            kind=kind,
            name=name,
            x=source.x,
            y=source.y,
            w=source.w,
            h=source.h,
            multiline=multiline,
            source=source.source,
        )
    except Exception:
        return None


def clean_candidate_name(name: str) -> str:
    name = re.sub(r"^field_", "", name)
    name = re.sub(r"^\d+_", "", name)
    return name


def clone_field(field: FieldSpec, name: str | None = None, multiline: bool | None = None) -> FieldSpec:
    return FieldSpec(
        page=field.page,
        kind=field.kind,
        name=name or field.name,
        x=field.x,
        y=field.y,
        w=field.w,
        h=field.h,
        multiline=field.multiline if multiline is None else multiline,
        source=field.source,
    )


def uniquify_field_names(fields: list[FieldSpec]) -> list[FieldSpec]:
    counts: dict[str, int] = {}
    unique: list[FieldSpec] = []
    for field in fields:
        base = label_slug(field.name, "field")
        counts[base] = counts.get(base, 0) + 1
        name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        unique.append(clone_field(field, name=name))
    return unique


def recover_high_confidence_fields(
    selected: list[FieldSpec],
    candidates: list[FieldSpec],
    reader: PdfReader,
) -> list[FieldSpec]:
    recovered = list(selected)
    runs_by_page = {i + 1: extract_text_runs(page) for i, page in enumerate(reader.pages)}
    code_counter: dict[int, int] = {}

    recoverable_label_patterns = [
        "provider_name",
        "tin_npi",
        "provider_group",
        "contact_name",
        "contact_number",
        "title",
        "contact_address",
        "phone",
        "fax",
        "email",
        "patient_name",
        "ins_id",
        "insurance_id",
        "claim_number",
        "date_of_service",
        "authorization_number",
        "member_name",
        "dos",
        "signature",
    ]

    def has_similar(field: FieldSpec) -> bool:
        return any(
            existing.page == field.page
            and existing.kind == field.kind
            and overlaps(existing, field, pad=0.5)
            for existing in recovered
        )

    for candidate in candidates:
        name = clean_candidate_name(candidate.name)
        page_runs = runs_by_page.get(candidate.page, [])
        context = nearby_text(page_runs, candidate).lower()

        if candidate.source == "checkbox" and candidate.kind == "checkbox":
            if not has_similar(candidate):
                recovered.append(clone_field(candidate, name=name))
            continue

        if candidate.source == "label" and any(pattern in name for pattern in recoverable_label_patterns):
            if "or_fax_to" not in name and not has_similar(candidate):
                recovered.append(clone_field(candidate, name=name))
            continue

        if candidate.source == "line" and candidate.kind == "text":
            line_context = row_text(page_runs, candidate.y).lower()
            combined_context = f"{context} {line_context}"
            if "codes in dispute" in combined_context and 20 <= candidate.w <= 70:
                code_counter[candidate.page] = code_counter.get(candidate.page, 0) + 1
                field = clone_field(candidate, name=f"code_in_dispute_{code_counter[candidate.page]}")
            elif "date of denial" in combined_context and candidate.w > 70:
                field = clone_field(candidate, name="denial_date")
            elif "additional information was requested" in combined_context and "date" in combined_context and candidate.w > 70:
                field = clone_field(candidate, name="additional_info_requested_date")
            elif "additional information provided" in combined_context and "date" in combined_context and candidate.w > 70:
                field = clone_field(candidate, name="additional_info_provided_date")
            elif "signature:" in combined_context and candidate.y < 320 and 80 < candidate.w < 360:
                field = clone_field(candidate, name="signature")
            else:
                continue
            if not has_similar(field):
                recovered.append(field)

        if candidate.source == "response_box":
            if ("reason for appeal" in context or "required" in context) and not has_similar(candidate):
                recovered.append(clone_field(candidate, name="reason_for_appeal", multiline=True))

    return uniquify_field_names(dedupe_fields(recovered))


def filter_llm_selected_fields(fields: list[FieldSpec]) -> list[FieldSpec]:
    # Let deterministic recovery own raw line candidates. The model is useful
    # for semantic filtering, but line candidates are the easiest place for it
    # to make confident coordinate/name mistakes.
    return [field for field in fields if field.source not in {"line", "response_box"}]


def extract_response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    chunks: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") in {"output_text", "text"} and isinstance(value.get("text"), str):
                chunks.append(value["text"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload.get("output", []))
    return "\n".join(chunks).strip()


def parse_json_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def refine_fields_with_llm(
    input_pdf: Path,
    reader: PdfReader,
    candidates: list[FieldSpec],
    model: str,
    env_path: Path | None = None,
) -> list[FieldSpec]:
    api_key = load_api_key(env_path)
    runs_by_page = {i + 1: extract_text_runs(page) for i, page in enumerate(reader.pages)}
    candidate_payload = []
    for index, field in enumerate(candidates, start=1):
        item = asdict(field)
        item["candidate_id"] = index
        item["rect"] = [field.x, field.y, field.x + field.w, field.y + field.h]
        item["nearby_text"] = nearby_text(runs_by_page.get(field.page, []), field)
        candidate_payload.append(item)
    pdf_b64 = base64.b64encode(input_pdf.read_bytes()).decode("ascii")
    pdf_data_url = f"data:application/pdf;base64,{pdf_b64}"

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "candidate_id": {"type": "integer"},
                        "kind": {"type": "string", "enum": ["text", "checkbox"]},
                        "name": {"type": "string"},
                        "multiline": {"type": "boolean"},
                    },
                    "required": ["candidate_id", "kind", "name", "multiline"],
                },
            }
        },
        "required": ["fields"],
    }

    prompt = f"""
You are converting a non-editable PDF form into editable AcroForm fields.

Use the PDF visual layout and the candidate geometry below. Return only real user-fillable fields.

Important rules:
- Ignore logos, letterheads, mailing addresses, page numbers, instructions, section headers, decorative boxes, and table/grid borders.
- Keep fields only where a human would type, check, sign, or enter a date.
- Do not estimate or invent coordinates. Choose candidate_id values from the list.
- If a candidate is a table/grid border, header line, logo/address box, or full row separator, omit it.
- Prefer candidates whose rectangle begins after the printed label text and does not overlap labels.
- Use each candidate's nearby_text to distinguish checkbox choices and label-adjacent text fields.
- Candidate source guidance: prefer source="label" for text fields and source="checkbox" for checkbox fields. Use source="line" only for true underlined blanks such as dates, code slots, signatures, or large response areas.
- Include all meaningful user choices in checkbox groups, including yes/no/NA options and appeal-reason checkboxes.
- Use concise snake_case field names based on labels.
- For large explanation/description boxes, set multiline=true.
- For checkboxes, use kind="checkbox" and a tight rectangle around the printed square.

Page sizes:
{json.dumps(page_sizes(reader), indent=2)}

Candidate fields from geometry:
{json.dumps(candidate_payload, indent=2)}
""".strip()

    request_body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": input_pdf.name,
                        "file_data": pdf_data_url,
                    },
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "fillable_pdf_fields",
                "schema": schema,
                "strict": True,
            }
        },
    }

    req = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=180) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc.reason}") from exc

    text = extract_response_text(response_payload)
    data = parse_json_text(text)
    fields = []
    for index, item in enumerate(data.get("fields", []), start=1):
        field = field_from_candidate(item, candidates, f"field_{index}")
        if field:
            fields.append(field)
    return recover_high_confidence_fields(filter_llm_selected_fields(dedupe_fields(fields)), candidates, reader)


def infer_fields(reader: PdfReader) -> list[FieldSpec]:
    specs: list[FieldSpec] = []
    counters: dict[str, int] = {}

    def next_name(page_num: int, prefix: str, label: str | None = None) -> str:
        base = label_slug(label or "", f"{prefix}_{page_num}")
        if not base.startswith(prefix):
            base = f"{prefix}_{base}"
        counters[base] = counters.get(base, 0) + 1
        return base if counters[base] == 1 else f"{base}_{counters[base]}"

    for page_index, page in enumerate(reader.pages):
        page_num = page_index + 1
        media = page.mediabox
        page_w = float(media.width)
        page_h = float(media.height)
        rects = extract_rects(page)
        runs = extract_text_runs(page)

        # Add semantic candidates for labels that end in a colon, e.g.
        # "Provider Name: ______" or "Claim Number (if known): ______".
        for line_runs in grouped_text_lines(runs):
            colon_indexes = [i for i, run in enumerate(line_runs) if ":" in run.text]
            if not colon_indexes:
                continue
            segment_start = 0
            for pos, colon_index in enumerate(colon_indexes):
                label_runs = line_runs[segment_start : colon_index + 1]
                if not label_runs:
                    continue
                label_text = " ".join(run.text for run in label_runs)
                label_end = max(run.x2 for run in label_runs)
                next_label_x = page_w - 33
                if pos + 1 < len(colon_indexes):
                    next_colon_index = colon_indexes[pos + 1]
                    next_label_index = max(colon_index + 1, next_colon_index - 2)
                    next_label_x = line_runs[next_label_index].x
                x = label_end + 18
                w = next_label_x - x - 8
                y = line_runs[colon_index].y
                if w >= 30 and 45 <= y <= page_h - 45:
                    specs.append(
                        FieldSpec(
                            page=page_num,
                            kind="text",
                            name=next_name(page_num, "field", label_text),
                            x=round(x, 2),
                            y=round(y - 11.5, 2),
                            w=round(w, 2),
                            h=16,
                            multiline=False,
                            source="label",
                        )
                    )
                segment_start = colon_index + 1

        for rect in rects:
            if rect.x < 0 or rect.y < 0 or rect.x2 > page_w + 1 or rect.y2 > page_h + 1:
                continue

            # Printed checkbox squares are usually small stroked rectangles.
            if 6 <= rect.w <= 15 and 6 <= rect.h <= 15 and abs(rect.w - rect.h) <= 3:
                specs.append(
                    FieldSpec(
                        page=page_num,
                        kind="checkbox",
                        name=next_name(page_num, "checkbox"),
                        x=rect.x,
                        y=rect.y,
                        w=rect.w,
                        h=rect.h,
                        source="checkbox",
                    )
                )
                continue

            # Large bordered boxes are often free-text response areas.
            if rect.w >= 200 and rect.h >= 70 and 45 <= rect.y <= page_h - 45:
                specs.append(
                    FieldSpec(
                        page=page_num,
                        kind="text",
                        name=next_name(page_num, "response"),
                        x=rect.x + 3,
                        y=rect.y + 3,
                        w=rect.w - 6,
                        h=rect.h - 6,
                        multiline=True,
                        source="response_box",
                    )
                )
                continue

            # Underlined blanks are often drawn as very thin rectangles.
            if rect.w >= 35 and 0 < rect.h <= 1.2:
                if rect.y < 45 or rect.y > page_h - 45:
                    continue
                specs.append(
                    FieldSpec(
                        page=page_num,
                        kind="text",
                        name=next_name(page_num, "line"),
                        x=rect.x,
                        y=rect.y + 1.0,
                        w=rect.w,
                        h=12,
                        source="line",
                    )
                )
                continue

            # Bordered table cells often contain a label followed by blank space.
            if rect.w >= 45 and 8 <= rect.h <= 65:
                cell_runs = text_in_rect(runs, rect)
                if not cell_runs:
                    specs.append(
                        FieldSpec(
                            page=page_num,
                            kind="text",
                            name=next_name(page_num, "field"),
                            x=rect.x + 3,
                            y=rect.y + 3,
                            w=rect.w - 6,
                            h=max(10, rect.h - 6),
                            multiline=rect.h >= 28,
                            source="empty_cell",
                        )
                    )
                    continue

                label_text = " ".join(run.text for run in sorted(cell_runs, key=lambda r: (r.y, r.x)))
                label_end = max(run.x2 for run in cell_runs)
                # Leave breathing room after label text, especially after colons.
                start_x = max(rect.x + 3, min(label_end + 10, rect.x2 - 25))
                width = rect.x2 - start_x - 4
                labelish = ":" in label_text or len(cell_runs) <= 4
                if labelish and width >= 28:
                    specs.append(
                        FieldSpec(
                            page=page_num,
                            kind="text",
                            name=next_name(page_num, "field", label_text),
                            x=start_x,
                            y=rect.y + 3,
                            w=width,
                            h=max(10, rect.h - 6),
                            multiline=rect.h >= 28,
                            source="cell_label",
                        )
                    )

    return dedupe_fields(specs)


def build_overlay(reader: PdfReader, fields: list[FieldSpec], overlay_path: Path) -> None:
    first = reader.pages[0].mediabox
    c = canvas.Canvas(str(overlay_path), pagesize=(float(first.width), float(first.height)))
    form = c.acroForm
    by_page: dict[int, list[FieldSpec]] = {}
    for field in fields:
        by_page.setdefault(field.page, []).append(field)

    for page_index, page in enumerate(reader.pages):
        box = page.mediabox
        c.setPageSize((float(box.width), float(box.height)))
        for field in by_page.get(page_index + 1, []):
            if field.kind == "checkbox":
                form.checkbox(
                    name=field.name,
                    tooltip=field.name,
                    x=field.x,
                    y=field.y,
                    size=min(field.w, field.h),
                    buttonStyle="check",
                    borderWidth=0,
                    borderColor=None,
                    fillColor=None,
                    textColor=black,
                    fieldFlags="",
                    forceBorder=False,
                )
            else:
                form.textfield(
                    name=field.name,
                    tooltip=field.name,
                    x=field.x,
                    y=field.y,
                    width=field.w,
                    height=field.h,
                    borderWidth=0,
                    borderColor=None,
                    fillColor=None,
                    textColor=black,
                    fontName="Helvetica",
                    fontSize=9,
                    fieldFlags="multiline" if field.multiline else "",
                    maxlen=3000 if field.multiline else 200,
                    forceBorder=False,
                )
        c.showPage()
    c.save()


def merge_original_with_overlay(input_pdf: Path, overlay_path: Path, output_pdf: Path) -> None:
    source = PdfReader(str(input_pdf))
    writer = PdfWriter(clone_from=str(overlay_path))
    for i, source_page in enumerate(source.pages):
        writer.pages[i].merge_page(source_page, over=True)

    if "/AcroForm" in writer._root_object:
        writer.set_need_appearances_writer(True)
        writer._root_object["/AcroForm"].update({NameObject("/NeedAppearances"): BooleanObject(True)})

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as fh:
        writer.write(fh)


def convert(
    input_pdf: Path,
    output_pdf: Path,
    debug_json: Path | None = None,
    use_llm: bool = False,
    model: str = "gpt-4.1-mini",
    env_path: Path | None = None,
    azure_mode: str = "off",
) -> list[FieldSpec]:
    reader = PdfReader(str(input_pdf))
    native_candidates = infer_fields(reader)
    candidates = list(native_candidates)
    azure_candidates: list[FieldSpec] = []
    azure_used = False
    azure_error: str | None = None

    text_lengths = [len(page.extract_text() or "") for page in reader.pages]
    weak_native_extraction = sum(text_lengths) < 100 or len(native_candidates) < max(8, len(reader.pages) * 4)
    should_use_azure = azure_mode == "always" or (azure_mode == "auto" and weak_native_extraction)

    if should_use_azure:
        try:
            azure_result = analyze_with_azure_document_intelligence(input_pdf, env_path)
            azure_candidates = infer_fields_from_azure(reader, azure_result)
            candidates = dedupe_fields(native_candidates + azure_candidates)
            azure_used = True
        except Exception as exc:
            azure_error = str(exc)
            if azure_mode == "always":
                raise

    fields = refine_fields_with_llm(input_pdf, reader, candidates, model, env_path) if use_llm else candidates
    fields = uniquify_field_names(fields)
    with tempfile.TemporaryDirectory(prefix="pdf_to_acroform_") as tmp:
        overlay_path = Path(tmp) / "overlay.pdf"
        build_overlay(reader, fields, overlay_path)
        merge_original_with_overlay(input_pdf, overlay_path, output_pdf)

    if debug_json:
        debug_json.parent.mkdir(parents=True, exist_ok=True)
        debug_json.write_text(
            json.dumps(
                {
                    "mode": "llm" if use_llm else "geometry",
                    "model": model if use_llm else None,
                    "azure_mode": azure_mode,
                    "azure_used": azure_used,
                    "azure_error": azure_error,
                    "candidate_count": len(candidates),
                    "native_candidate_count": len(native_candidates),
                    "azure_candidate_count": len(azure_candidates),
                    "candidate_sources": dict(Counter(field.source for field in candidates)),
                    "field_count": len(fields),
                    "field_sources": dict(Counter(field.source for field in fields)),
                    "fields": [asdict(field) for field in fields],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("output_pdf", type=Path)
    parser.add_argument("--debug-json", type=Path, help="Optional path to save detected field coordinates.")
    parser.add_argument("--llm", action="store_true", help="Use OpenAI vision/PDF understanding to filter and adjust fields.")
    parser.add_argument("--model", default="gpt-4.1-mini", help="OpenAI model to use with --llm.")
    parser.add_argument("--env-file", type=Path, help="Optional .env file containing API credentials.")
    parser.add_argument(
        "--azure-mode",
        choices=["off", "auto", "always"],
        default="off",
        help="Use Azure Document Intelligence as an OCR/layout fallback or enrichment layer.",
    )
    args = parser.parse_args()

    fields = convert(
        args.input_pdf,
        args.output_pdf,
        args.debug_json,
        args.llm,
        args.model,
        args.env_file,
        args.azure_mode,
    )
    print(f"Wrote {args.output_pdf}")
    print(f"Detected {len(fields)} fields")


if __name__ == "__main__":
    main()
