#!/usr/bin/env python
"""
Template-driven JSON-to-PDF filling engine.

This module creates reviewable template mapping artifacts and fills payer PDF
forms deterministically from the stable database JSON schema. It intentionally
does not depend on converting forms to AcroForms.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from textwrap import wrap
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import black
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth


try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional production dependency
    fitz = None


DATE_INPUT_FORMATS = ["%Y%m%d", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]
LABEL_VALUE_GAP = 18
FIELD_RIGHT_PADDING = 4
MIN_FILL_WIDTH = 24


@dataclass
class TextRun:
    text: str
    page: int
    x: float
    y: float
    size: float

    @property
    def x2(self) -> float:
        return self.x + stringWidth(self.text, "Helvetica", self.size)


@dataclass
class RectCandidate:
    page: int
    source: str
    label: str
    labelRect: list[float] | None
    fillRect: list[float]
    sectionPath: list[str]
    nearbyText: str
    confidence: float
    kind: str = "text"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def slug(text: str, fallback: str = "field") -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    return value[:80] or fallback


def load_env_value(name: str, env_path: Path | None = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for path in [p for p in [env_path, Path("work/.env"), Path(".env")] if p]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            if key.strip() == name:
                value = raw.strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError(f"{name} was not found in the environment or local .env.")


def openai_json_response(
    pdf_path: Path,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    env_path: Path | None = None,
) -> dict[str, Any]:
    api_key = load_env_value("OPENAI_API_KEY", env_path)
    file_data = "data:application/pdf;base64," + base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "filename": pdf_path.name, "file_data": file_data},
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "pdf_template_mapping",
                "schema": schema,
                "strict": True,
            }
        },
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc.reason}") from exc

    chunks: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"output_text", "text"} and isinstance(value.get("text"), str):
                chunks.append(value["text"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    if isinstance(payload.get("output_text"), str):
        text = payload["output_text"]
    else:
        walk(payload.get("output", []))
        text = "\n".join(chunks).strip()
    return json.loads(text)


def extract_json_paths(data: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.extend(extract_json_paths(value, child))
    elif isinstance(data, list):
        child = f"{prefix}[]"
        if data:
            paths.extend(extract_json_paths(data[0], child))
        else:
            paths.append(child)
    else:
        paths.append(prefix)
    return paths


def is_nonempty_json_path(path: Any) -> bool:
    return isinstance(path, str) and bool(path.strip())


def resolve_json_path(data: Any, path: str | None) -> Any:
    if not is_nonempty_json_path(path):
        return None
    current = data
    for part in str(path).strip().split("."):
        if not part:
            return None
        if part.endswith("[]"):
            key = part[:-2]
            if not key:
                return None
            current = current.get(key) if isinstance(current, dict) else None
            if not isinstance(current, list):
                return None
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def pdf_text(reader: PdfReader) -> str:
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def compute_fingerprint(pdf_path: Path) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    page_sizes = [
        [float(page.mediabox.width), float(page.mediabox.height)]
        for page in reader.pages
    ]
    text = pdf_text(reader)
    digest = hashlib.sha256()
    digest.update(str(page_sizes).encode("utf-8"))
    digest.update(re.sub(r"\s+", " ", text).encode("utf-8"))
    return {
        "algorithm": "sha256(page_sizes+normalized_text)",
        "value": digest.hexdigest(),
        "pageCount": len(reader.pages),
        "pageSizes": page_sizes,
        "textLength": len(text),
    }


def extract_text_runs_pypdf(reader: PdfReader) -> list[TextRun]:
    runs: list[TextRun] = []

    for page_index, page in enumerate(reader.pages):
        def visitor(text, cm, tm, font, size):
            clean = re.sub(r"\s+", " ", text or "").strip()
            if clean:
                runs.append(
                    TextRun(
                        text=clean,
                        page=page_index + 1,
                        x=float(tm[4]),
                        y=float(tm[5]),
                        size=float(size),
                    )
                )

        page.extract_text(visitor_text=visitor)
    return runs


def extract_text_runs_pymupdf(pdf_path: Path) -> list[TextRun]:
    if fitz is None:
        return []
    runs: list[TextRun] = []
    doc = fitz.open(str(pdf_path))
    for page_index, page in enumerate(doc):
        height = float(page.rect.height)
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = re.sub(r"\s+", " ", span.get("text", "")).strip()
                    if not text:
                        continue
                    x0, y0, _x1, y1 = span["bbox"]
                    runs.append(
                        TextRun(
                            text=text,
                            page=page_index + 1,
                            x=float(x0),
                            y=round(height - float(y1), 2),
                            size=float(span.get("size", 9)),
                        )
                    )
    return runs


def extract_rectangles(reader: PdfReader) -> list[dict[str, Any]]:
    rects: list[dict[str, Any]] = []
    for page_index, page in enumerate(reader.pages):
        def before(op, args, cm, tm):
            op = op.decode() if isinstance(op, bytes) else op
            if op == "re" and len(args) >= 4:
                x, y, w, h = [round(float(v), 2) for v in args[:4]]
                if w > 0 and h > 0:
                    rects.append({"page": page_index + 1, "rect": [x, y, x + w, y + h]})

        page.extract_text(visitor_operand_before=before)
    return rects


def group_lines(runs: list[TextRun], tolerance: float = 3.0) -> list[list[TextRun]]:
    groups: list[list[TextRun]] = []
    for run in sorted(runs, key=lambda r: (r.page, -r.y, r.x)):
        for group in groups:
            if group[0].page == run.page and abs(group[0].y - run.y) <= tolerance:
                group.append(run)
                break
        else:
            groups.append([run])
    return [sorted(group, key=lambda r: r.x) for group in groups]


def line_text(line: list[TextRun]) -> str:
    return re.sub(r"\s+", " ", " ".join(run.text for run in line)).strip()


def infer_section_path(line: list[TextRun], current: list[str]) -> list[str]:
    text = line_text(line)
    lower = text.lower()
    if not text:
        return current
    section_terms = [
        "information",
        "claim",
        "provider",
        "subscriber",
        "patient",
        "requestor",
        "reason",
        "supporting documentation",
        "section",
    ]
    is_section = (
        any(term in lower for term in section_terms)
        and len(text) <= 90
        and ":" not in text
        and not re.match(r"^\d+\.", text)
    )
    if is_section:
        return [text]
    return current


def nearby_text(runs: list[TextRun], page: int, rect: list[float], radius: float = 35) -> str:
    x1, y1, x2, y2 = rect
    selected = [
        r
        for r in runs
        if r.page == page
        and x1 - radius <= r.x <= x2 + radius
        and y1 - radius <= r.y <= y2 + radius
    ]
    return re.sub(r"\s+", " ", " ".join(r.text for r in sorted(selected, key=lambda r: (-r.y, r.x))))[:500]


def run_rect(run: TextRun) -> list[float]:
    return [run.x, run.y - 2, run.x2, run.y + run.size + 2]


def vertical_overlap(a: list[float], b: list[float]) -> float:
    return max(0, min(a[3], b[3]) - max(a[1], b[1]))


def rect_intersects(a: list[float], b: list[float]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def protected_text_runs(runs: list[TextRun], page: int, rect: list[float], label_rect: list[float] | None = None) -> list[TextRun]:
    protected: list[TextRun] = []
    rect_height = max(1, rect[3] - rect[1])
    for run in runs:
        if run.page != page:
            continue
        candidate = run_rect(run)
        if label_rect and rect_intersects(candidate, label_rect):
            continue
        overlap = vertical_overlap(candidate, rect)
        if overlap <= 0 or overlap / min(rect_height, max(1, candidate[3] - candidate[1])) < 0.35:
            continue
        if candidate[2] <= rect[0] + 1 or candidate[0] >= rect[2] - 1:
            continue
        protected.append(run)
    return sorted(protected, key=lambda item: item.x)


def sanitize_fill_rect(field: dict[str, Any], page_width: float, runs: list[TextRun] | None = None) -> tuple[list[float] | None, list[str]]:
    warnings: list[str] = []
    rect = field.get("fillRect")
    if not valid_rect(rect):
        return None, ["invalid_fill_rect"]
    x1, y1, x2, y2 = [float(v) for v in rect]
    label_rect = field.get("labelRect")
    if valid_rect(label_rect):
        label_x1, label_y1, label_x2, label_y2 = [float(v) for v in label_rect]
        min_x = min(page_width - MIN_FILL_WIDTH, label_x2 + LABEL_VALUE_GAP)
        if x1 < min_x:
            x1 = min_x
            warnings.append("fillRect_shifted_after_label")
        y1 = max(y1, label_y1 - 1)
        y2 = min(max(y2, y1 + 10), label_y2 + 3)
    x1 = max(0, x1)
    x2 = min(page_width - FIELD_RIGHT_PADDING, x2)
    if runs:
        for run in protected_text_runs(runs, int(field.get("page") or 0), [x1, y1, x2, y2], label_rect):
            blocker = run_rect(run)
            if blocker[0] > x1 + 1:
                new_x2 = min(x2, blocker[0] - FIELD_RIGHT_PADDING)
                if new_x2 < x2:
                    x2 = new_x2
                    warnings.append(f"fillRect_trimmed_before_pdf_text:{preview_value(run.text, 40)}")
                break
            if blocker[2] >= x1:
                x1 = blocker[2] + FIELD_RIGHT_PADDING
                warnings.append(f"fillRect_shifted_after_pdf_text:{preview_value(run.text, 40)}")
    if x2 - x1 < MIN_FILL_WIDTH:
        warnings.append("fillRect_too_narrow_after_sanitization")
        return None, warnings
    return [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)], warnings


def build_candidates(pdf_path: Path) -> list[RectCandidate]:
    reader = PdfReader(str(pdf_path))
    runs = extract_text_runs_pymupdf(pdf_path) or extract_text_runs_pypdf(reader)
    candidates: list[RectCandidate] = []
    current_section_by_page: dict[int, list[str]] = {}

    for line in group_lines(runs):
        page = line[0].page
        current = current_section_by_page.get(page, [])
        current = infer_section_path(line, current)
        current_section_by_page[page] = current
        text = line_text(line)
        if ":" not in text:
            continue
        colon_runs = [i for i, r in enumerate(line) if ":" in r.text]
        segment_start = 0
        for pos, colon_index in enumerate(colon_runs):
            label_runs = line[segment_start : colon_index + 1]
            if not label_runs:
                continue
            label = line_text(label_runs)
            label_x1 = min(r.x for r in label_runs)
            label_y1 = min(r.y for r in label_runs) - 2
            label_x2 = max(r.x2 for r in label_runs)
            label_y2 = max(r.y + r.size for r in label_runs)
            next_x = float(reader.pages[page - 1].mediabox.width) - 36
            if pos + 1 < len(colon_runs):
                next_colon = colon_runs[pos + 1]
                next_x = line[max(colon_index + 1, next_colon - 2)].x - 8
            fill_x1 = min(label_x2 + LABEL_VALUE_GAP, next_x - 35)
            fill_x2 = next_x
            if fill_x2 - fill_x1 >= 28:
                raw_fill = [round(fill_x1, 2), round(label_y1, 2), round(fill_x2, 2), round(label_y1 + 16, 2)]
                label_rect = [round(label_x1, 2), round(label_y1, 2), round(label_x2, 2), round(label_y2, 2)]
                field_stub = {"page": page, "fillRect": raw_fill, "labelRect": label_rect}
                sanitized, _warnings = sanitize_fill_rect(field_stub, float(reader.pages[page - 1].mediabox.width), runs)
                if not sanitized:
                    segment_start = colon_index + 1
                    continue
                fill = sanitized
                candidates.append(
                    RectCandidate(
                        page=page,
                        source="label",
                        label=label,
                        labelRect=label_rect,
                        fillRect=fill,
                        sectionPath=current,
                        nearbyText=nearby_text(runs, page, fill),
                        confidence=0.82,
                    )
                )
            segment_start = colon_index + 1

    for item in extract_rectangles(reader):
        page = item["page"]
        x1, y1, x2, y2 = item["rect"]
        w = x2 - x1
        h = y2 - y1
        if 5 <= w <= 18 and 5 <= h <= 18 and abs(w - h) <= 4:
            fill = [x1, y1, x2, y2]
            candidates.append(
                RectCandidate(
                    page=page,
                    source="checkbox",
                    label="checkbox",
                    labelRect=None,
                    fillRect=fill,
                    sectionPath=current_section_by_page.get(page, []),
                    nearbyText=nearby_text(runs, page, fill, radius=60),
                    confidence=0.85,
                    kind="checkbox",
                )
            )
        elif w >= 200 and h >= 60:
            fill = [x1 + 3, y1 + 3, x2 - 3, y2 - 3]
            context = nearby_text(runs, page, fill, radius=70)
            if any(term in context.lower() for term in ["explain", "reason", "comments", "other"]):
                candidates.append(
                    RectCandidate(
                        page=page,
                        source="response_box",
                        label="free text",
                        labelRect=None,
                        fillRect=fill,
                        sectionPath=current_section_by_page.get(page, []),
                        nearbyText=context,
                        confidence=0.7,
                    )
                )

    return dedupe_candidates(candidates)


def rect_overlap(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x = max(0, min(ax2, bx2) - max(ax1, bx1))
    y = max(0, min(ay2, by2) - max(ay1, by1))
    intersection = x * y
    area = min(max(1, (ax2 - ax1) * (ay2 - ay1)), max(1, (bx2 - bx1) * (by2 - by1)))
    return intersection / area


def dedupe_candidates(candidates: list[RectCandidate]) -> list[RectCandidate]:
    rank = {"label": 0, "checkbox": 0, "response_box": 2, "azure": 3}
    kept: list[RectCandidate] = []
    for cand in sorted(candidates, key=lambda c: (c.page, rank.get(c.source, 9), -c.confidence)):
        if any(cand.kind == other.kind and cand.page == other.page and rect_overlap(cand.fillRect, other.fillRect) > 0.45 for other in kept):
            continue
        kept.append(cand)
    return sorted(kept, key=lambda c: (c.page, -c.fillRect[1], c.fillRect[0]))


def heuristic_json_path(label: str, section: list[str], schema_paths: list[str]) -> str | None:
    text = " ".join(section + [label]).lower()
    candidates = {
        "provider": {
            "name": "result.provider.facilityName",
            "tin": "result.provider.billingTin",
            "tax": "result.provider.billingTaxID",
            "npi": "result.provider.billingNpi",
            "address": "result.provider.facilityAddress",
            "phone": "result.payer.phone",
            "fax": "result.payer.fax",
        },
        "patient": {
            "name": "result.patient.firstName",
            "id": "result.patient.memberID",
            "dob": "result.patient.dateOfBirth",
        },
        "subscriber": {
            "name": "result.subscriber.firstName",
            "id": "result.subscriber.memberID",
            "dob": "result.subscriber.dateOfBirth",
        },
        "claim": {
            "number": "result.claim.claimNumber",
            "authorization": "result.claim.claimAuthorizationNumber",
            "service": "result.claim.claimServiceDateStart",
            "billed": "result.claim.claimSubmittedCharges",
            "disputed": "result.claim.totalDeniedChargedAmount",
            "amount": "result.claim.claimSubmittedCharges",
            "process": "result.claim.dateReceived",
            "received": "result.claim.dateReceived",
            "paid": "result.claim.claimPaidAmount",
            "denial": "result.claim.denialSummary",
            "explain": "result.claim.denialSummary",
            "reason": "result.claim.denialSummary",
        },
    }
    if "subscriber" in text:
        group = candidates["subscriber"]
    elif "patient" in text or "member" in text:
        group = candidates["patient"]
    elif "provider" in text or "tin" in text or "npi" in text:
        group = candidates["provider"]
    else:
        group = candidates["claim"]

    for key, path in group.items():
        if key in text and path in schema_paths:
            return path
    if "office" in text and "name" in text and "result.provider.facilityName" in schema_paths:
        return "result.provider.facilityName"
    if "practice" in text and "name" in text and "result.provider.facilityName" in schema_paths:
        return "result.provider.facilityName"
    if "provider" in text and "name" in text and "result.provider.facilityName" in schema_paths:
        return "result.provider.facilityName"
    if "provider" in text and ("tin" in text or "#" in text) and "result.provider.billingTin" in schema_paths:
        return "result.provider.billingTin"
    if "billed amount" in text and "result.claim.claimSubmittedCharges" in schema_paths:
        return "result.claim.claimSubmittedCharges"
    if "disputed amount" in text and "result.claim.totalDeniedChargedAmount" in schema_paths:
        return "result.claim.totalDeniedChargedAmount"
    if "process date" in text and "result.claim.dateReceived" in schema_paths:
        return "result.claim.dateReceived"
    if "explain" in text and "result.claim.denialSummary" in schema_paths:
        return "result.claim.denialSummary"
    if "claim" in text and "number" in text and "result.claim.claimNumber" in schema_paths:
        return "result.claim.claimNumber"
    if "date" in text and "service" in text and "result.claim.claimServiceDateStart" in schema_paths:
        return "result.claim.claimServiceDateStart"
    if "email" in text and "result.payer.email" in schema_paths:
        return "result.payer.email"
    return None


def mapping_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "candidateId": {"type": "integer"},
                        "fieldId": {"type": "string"},
                        "type": {"type": "string", "enum": ["text", "date"]},
                        "jsonPath": {"type": ["string", "null"]},
                        "format": {"type": ["string", "null"]},
                    },
                    "required": ["candidateId", "fieldId", "type", "jsonPath", "format"],
                },
            },
            "checkboxGroups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fieldId": {"type": "string"},
                        "jsonPath": {"type": ["string", "null"]},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "candidateId": {"type": "integer"},
                                    "value": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                                "required": ["candidateId", "value", "label"],
                            },
                        },
                    },
                    "required": ["fieldId", "jsonPath", "options"],
                },
            },
            "repeaters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repeaterId": {"type": "string"},
                        "jsonPath": {"type": "string"},
                        "page": {"type": "integer"},
                        "startY": {"type": "number"},
                        "rowHeight": {"type": "number"},
                        "maxRows": {"type": "integer"},
                        "columns": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "jsonPath": {"type": "string"},
                                    "name": {"type": "string"},
                                    "rectOffset": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 4,
                                        "maxItems": 4,
                                    },
                                    "format": {"type": ["string", "null"]},
                                },
                                "required": ["jsonPath", "name", "rectOffset", "format"],
                            },
                        },
                        "overflow": {"type": "string"},
                    },
                    "required": ["repeaterId", "jsonPath", "page", "startY", "rowHeight", "maxRows", "columns", "overflow"],
                },
            },
        },
        "required": ["fields", "checkboxGroups", "repeaters"],
    }


def llm_select_mapping(pdf_path: Path, candidates: list[RectCandidate], schema_paths: list[str], model: str, env_path: Path | None) -> dict[str, Any]:
    candidate_payload = []
    for i, cand in enumerate(candidates, 1):
        item = asdict(cand)
        item["candidateId"] = i
        candidate_payload.append(item)

    allowed_paths = [p for p in schema_paths if p.startswith("result.")]
    prompt = f"""
Create a draft mapping for filling this payer PDF from the fixed database JSON schema.

Rules:
- Select only fields a user would actually fill. Ignore instruction text, page headers, payer addresses, logos, reply/office-use sections, and legal footers.
- Never invent coordinates. Use candidateId only.
- Same label in different sections must map by section context, for example Patient First Name vs Subscriber First Name.
- Use only jsonPath values from the allowed list. Use null if no safe mapping exists.
- Date fields must use type="date" and format="MM/DD/YYYY".
- Group related checkboxes into checkboxGroups. Long checkbox descriptions should remain labels, not fieldIds.
- Do not map provider identifiers to payer identifiers.
- Do not create repeaters unless the PDF has a clear service-line table.

Allowed JSON paths:
{json.dumps(allowed_paths, indent=2)}

Candidates:
{json.dumps(candidate_payload, indent=2)}
""".strip()
    return openai_json_response(pdf_path, prompt, mapping_schema(), model, env_path)


def unique_id(base: str, seen: dict[str, int]) -> str:
    base = slug(base, "field")
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}_{seen[base]}"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def infer_value_transform(label: str, field_id: str = "", json_path: str | None = None, kind: str = "text") -> str:
    text = f"{field_id} {label} {json_path or ''}".lower()
    if kind == "date":
        if "service" in text and "date" in text and ("dates" in text or "(s)" in text or "range" in text):
            return "date_range"
        return "date"
    if "date" in text or "dob" in text or "dos" in text:
        if "service" in text and ("dates" in text or "(s)" in text or "range" in text):
            return "date_range"
        return "date"
    if "amount" in text or "charge" in text or "paid" in text or "balance" in text:
        return "currency"
    if "name" in text and json_path:
        if any(prefix in json_path for prefix in ["result.patient.", "result.subscriber.", "result.dependent."]):
            return "name_full"
    return "text"


def add_transform_metadata(field: dict[str, Any], schema_paths: list[str], kind: str | None = None) -> None:
    transform = infer_value_transform(
        str(field.get("label") or ""),
        str(field.get("fieldId") or ""),
        field.get("jsonPath"),
        kind or str(field.get("type") or "text"),
    )
    field.setdefault("valueTransform", transform)
    if transform == "date_range":
        secondary = "result.claim.claimServiceDateEnd"
        if not field.get("jsonPathSecondary") and secondary in schema_paths:
            field["jsonPathSecondary"] = secondary
        field.setdefault("format", "MM/DD/YYYY")
    elif transform == "date":
        field.setdefault("format", "MM/DD/YYYY")


def apply_runtime_backfills(mapping: dict[str, Any], schema_paths: list[str]) -> None:
    allowed = set(schema_paths)
    for collection in ["fields", "dateFields"]:
        for field in mapping.get(collection, []):
            text = f"{field.get('fieldId') or ''} {field.get('label') or ''} {' '.join(field.get('sectionPath') or [])}".lower()
            if "disputed" in text and "amount" in text and "result.claim.totalDeniedChargedAmount" in allowed:
                if field.get("jsonPath") in {None, "", "result.claim.claimSubmittedCharges"}:
                    field["jsonPath"] = "result.claim.totalDeniedChargedAmount"
                    field["review"] = "ok"
                field["valueTransform"] = "currency"
            elif "date" in text and "service" in text and "result.claim.claimServiceDateStart" in allowed:
                field["jsonPath"] = field.get("jsonPath") or "result.claim.claimServiceDateStart"
                if "result.claim.claimServiceDateEnd" in allowed:
                    field["jsonPathSecondary"] = field.get("jsonPathSecondary") or "result.claim.claimServiceDateEnd"
                field["valueTransform"] = "date_range"
                field["format"] = "MM/DD/YYYY"
            elif "process" in text and "date" in text:
                field["valueTransform"] = "date"
                field["format"] = "MM/DD/YYYY"
            else:
                add_transform_metadata(field, schema_paths, "date" if collection == "dateFields" else str(field.get("type") or "text"))


def is_date_like_path(path: Any) -> bool:
    text = str(path or "").lower()
    return any(term in text for term in ["date", "dob", "service"])


def production_skip_reason(field: dict[str, Any], kind: str = "field") -> str | None:
    label = normalize_space(field.get("label") or "").lower()
    field_id = normalize_space(field.get("fieldId") or "").lower()
    combined = f"{field_id} {label}"
    path = str(field.get("jsonPath") or "")
    if kind == "checkbox_group":
        return None
    static_label_terms = [
        "fax to:",
        "mail to:",
        "send to:",
        "for more information",
        "do not submit",
        "you may submit",
        "our determination",
    ]
    if any(term in label for term in static_label_terms):
        return "non_fillable_static_text"
    if label.startswith("-") and label.count("-") >= 2:
        return "option_list_not_text_field"
    if len(label) > 110 and not any(term in label for term in ["explain", "comments:", "remarks:"]):
        return "long_instruction_not_fill_target"
    instruction_terms = ["you must be specific", "handling of the claim", "hand_ling", "billing code", "reason for contesting"]
    if any(term in combined for term in instruction_terms):
        return "long_instruction_not_fill_target"
    if "signature" in label or field_id.endswith("_signature"):
        if "signature" not in path.lower():
            return "unsafe_signature_mapping"
    if "attachment" in label or "attachments" in label or "submitted with your appeal" in label:
        return "non_text_attachment_prompt"
    if "provide more information" in label:
        path_lower = path.lower()
        if not any(term in path_lower for term in ["summary", "description", "reason", "note", "comment"]):
            return "narrative_prompt_mapped_to_non_narrative_path"
    if ("date" in label or label in {"dos", "dob"}) and not is_date_like_path(path):
        return "date_label_mapped_to_non_date_path"
    return None


def build_mapping_from_llm(
    pdf_path: Path,
    schema_paths: list[str],
    candidates: list[RectCandidate],
    llm_mapping: dict[str, Any],
    template_id: str | None,
) -> dict[str, Any]:
    fingerprint = compute_fingerprint(pdf_path)
    by_id = {i: cand for i, cand in enumerate(candidates, 1)}
    seen: dict[str, int] = {}
    fields: list[dict[str, Any]] = []
    date_fields: list[dict[str, Any]] = []
    checkbox_groups: list[dict[str, Any]] = []
    allowed_paths = set(schema_paths)

    for item in llm_mapping.get("fields", []):
        cand = by_id.get(int(item.get("candidateId", 0)))
        if not cand or cand.kind != "text":
            continue
        json_path = item.get("jsonPath")
        if json_path not in allowed_paths:
            json_path = heuristic_json_path(cand.label, cand.sectionPath, schema_paths)
        field = {
            "fieldId": unique_id(str(item.get("fieldId") or cand.label), seen),
            "type": item.get("type") or "text",
            "jsonPath": json_path,
            "page": cand.page,
            "sectionPath": cand.sectionPath,
            "label": cand.label,
            "labelRect": cand.labelRect,
            "fillRect": cand.fillRect,
            "fontSize": 9,
            "overflow": "shrink_then_clip",
            "confidence": cand.confidence,
            "nearbyText": cand.nearbyText,
            "review": "ok" if json_path else "needs_json_path",
        }
        if field["type"] == "date":
            field.update({"format": item.get("format") or "MM/DD/YYYY", "mode": "single", "separators": "draw"})
            add_transform_metadata(field, schema_paths, "date")
            reason = production_skip_reason(field, "date")
            if reason:
                field["review"] = reason
            date_fields.append(field)
        else:
            add_transform_metadata(field, schema_paths, "text")
            reason = production_skip_reason(field, "field")
            if reason:
                field["review"] = reason
            fields.append(field)

    for group in llm_mapping.get("checkboxGroups", []):
        options = []
        page = None
        for option in group.get("options", []):
            cand = by_id.get(int(option.get("candidateId", 0)))
            if not cand or cand.kind != "checkbox":
                continue
            page = cand.page
            options.append({"value": option.get("value") or "true", "label": option.get("label") or cand.nearbyText, "boxRect": cand.fillRect})
        if not options:
            continue
        json_path = group.get("jsonPath")
        if json_path not in allowed_paths:
            json_path = None
        checkbox_groups.append(
            {
                "fieldId": unique_id(str(group.get("fieldId") or "checkbox_group"), seen),
                "type": "checkbox_group",
                "jsonPath": json_path,
                "page": page,
                "sectionPath": [],
                "label": " / ".join(o["label"] for o in options)[:240],
                "options": options,
                "confidence": 0.8,
                "multiSelect": False,
                "matchMode": "contains_normalized",
                "review": "ok" if json_path else "needs_json_path",
            }
        )

    return {
        "templateId": template_id or slug(pdf_path.stem, "template"),
        "templateVersion": "v1",
        "templateFingerprint": fingerprint,
        "pageCount": fingerprint["pageCount"],
        "pageSizes": fingerprint["pageSizes"],
        "fields": fields,
        "checkboxGroups": checkbox_groups,
        "dateFields": date_fields,
        "repeaters": llm_mapping.get("repeaters", []),
        "createdBy": "pdf_fill_engine.create_mapping",
        "reviewStatus": "draft_needs_review",
        "schemaPaths": schema_paths,
        "candidateCount": len(candidates),
        "mappingMode": "llm",
    }


def build_mapping(
    pdf_path: Path,
    schema_path: Path,
    out_path: Path,
    template_id: str | None = None,
    use_llm: bool = False,
    model: str = "gpt-4.1-mini",
    env_path: Path | None = None,
) -> dict[str, Any]:
    schema = load_json(schema_path)
    schema_paths = extract_json_paths(schema)
    fingerprint = compute_fingerprint(pdf_path)
    candidates = build_candidates(pdf_path)

    if use_llm:
        llm_mapping = llm_select_mapping(pdf_path, candidates, schema_paths, model, env_path)
        mapping = build_mapping_from_llm(pdf_path, schema_paths, candidates, llm_mapping, template_id)
        write_json(out_path, mapping)
        return mapping

    fields = []
    checkbox_groups = []
    date_fields = []
    for idx, cand in enumerate(candidates, 1):
        field_id = slug("_".join(cand.sectionPath + [cand.label]), f"field_{idx}")
        if cand.kind == "checkbox":
            checkbox_groups.append(
                {
                    "fieldId": f"{field_id}_{idx}",
                    "type": "checkbox_group",
                    "jsonPath": None,
                    "page": cand.page,
                    "sectionPath": cand.sectionPath,
                    "label": cand.nearbyText,
                    "options": [
                        {
                            "value": "true",
                            "label": cand.nearbyText[:160],
                            "boxRect": cand.fillRect,
                        }
                    ],
                    "confidence": cand.confidence,
                    "multiSelect": False,
                    "matchMode": "contains_normalized",
                    "review": "needs_json_path_and_option_value",
                }
            )
            continue
        json_path = heuristic_json_path(cand.label, cand.sectionPath, schema_paths)
        is_date = any(term in cand.label.lower() for term in ["date", "dob", "dos"])
        item = {
            "fieldId": field_id,
            "type": "date" if is_date else "text",
            "jsonPath": json_path,
            "page": cand.page,
            "sectionPath": cand.sectionPath,
            "label": cand.label,
            "labelRect": cand.labelRect,
            "fillRect": cand.fillRect,
            "fontSize": 9,
            "overflow": "shrink_then_clip",
            "confidence": cand.confidence,
            "nearbyText": cand.nearbyText,
            "review": "ok" if json_path else "needs_json_path",
        }
        if is_date:
            item.update({"format": "MM/DD/YYYY", "mode": "single", "separators": "draw"})
            add_transform_metadata(item, schema_paths, "date")
            reason = production_skip_reason(item, "date")
            if reason:
                item["review"] = reason
            date_fields.append(item)
        else:
            add_transform_metadata(item, schema_paths, "text")
            reason = production_skip_reason(item, "field")
            if reason:
                item["review"] = reason
            fields.append(item)

    mapping = {
        "templateId": template_id or slug(pdf_path.stem, "template"),
        "templateVersion": "v1",
        "templateFingerprint": fingerprint,
        "pageCount": fingerprint["pageCount"],
        "pageSizes": fingerprint["pageSizes"],
        "fields": fields,
        "checkboxGroups": checkbox_groups,
        "dateFields": date_fields,
        "repeaters": [],
        "createdBy": "pdf_fill_engine.create_mapping",
        "reviewStatus": "draft_needs_review",
        "schemaPaths": schema_paths,
        "candidateCount": len(candidates),
        "mappingMode": "heuristic",
    }
    write_json(out_path, mapping)
    return mapping


def parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            pass
    return None


def format_currency(value: Any, currency_symbol: bool = False) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = normalize_space(value).replace(",", "")
    if not text:
        return None
    try:
        rendered = f"{float(text):.2f}"
    except ValueError:
        return text
    return f"${rendered}" if currency_symbol else rendered


def infer_name_base_path(path: str | None) -> str | None:
    if not path:
        return None
    for base in ["result.patient", "result.subscriber", "result.dependent"]:
        if path == base or path.startswith(base + "."):
            return base
    return None


def format_full_name(data: Any, path: str | None) -> str | None:
    base = infer_name_base_path(path)
    if not base:
        value = resolve_json_path(data, path)
        if isinstance(value, str):
            return normalize_space(value) or None
        return None
    parts = [
        resolve_json_path(data, f"{base}.firstName"),
        resolve_json_path(data, f"{base}.middleName"),
        resolve_json_path(data, f"{base}.lastName"),
    ]
    rendered = " ".join(normalize_space(part) for part in parts if normalize_space(part))
    return rendered or None


def format_date_range(start: Any, end: Any) -> str | None:
    first = parse_date(start)
    second = parse_date(end)
    if first and second and first != second:
        return f"{first} - {second}"
    return first or second


def scalar_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = normalize_space(value)
    return text or None


def preview_value(value: Any, limit: int = 120) -> str:
    text = normalize_space(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_mapped_value(data: Any, field: dict[str, Any], default_transform: str = "text") -> tuple[str | None, str | None]:
    path = field.get("jsonPath")
    if not is_nonempty_json_path(path):
        return None, "missing_json_path"
    transform = field.get("valueTransform") or infer_value_transform(
        str(field.get("label") or ""),
        str(field.get("fieldId") or ""),
        path,
        default_transform,
    )
    value = resolve_json_path(data, path)
    if value is data or isinstance(value, (dict, list)):
        return None, "non_scalar_value"
    if value is None or value == "":
        return None, "empty_value"

    if transform == "name_full":
        rendered = format_full_name(data, path)
    elif transform == "date":
        rendered = parse_date(value)
        if not rendered:
            return None, "invalid_date"
    elif transform == "date_range":
        secondary = field.get("jsonPathSecondary")
        end_value = resolve_json_path(data, secondary) if is_nonempty_json_path(secondary) else None
        rendered = format_date_range(value, end_value)
        if not rendered:
            return None, "invalid_date_range"
    elif transform == "currency":
        rendered = format_currency(value, bool(field.get("currencySymbol")))
    else:
        rendered = scalar_text(value)

    if not rendered:
        return None, "empty_rendered_value"
    return rendered, None


def valid_rect(rect: Any) -> bool:
    if not isinstance(rect, list) or len(rect) != 4:
        return False
    try:
        x1, y1, x2, y2 = [float(v) for v in rect]
    except (TypeError, ValueError):
        return False
    return x2 > x1 and y2 > y1


def validate_mapping(mapping: dict[str, Any], schema_paths: list[str]) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed_paths = set(schema_paths)
    seen_ids: set[str] = set()
    rects_by_page: dict[int, list[tuple[str, list[float]]]] = {}

    def check_id(item: dict[str, Any], collection: str) -> None:
        field_id = str(item.get("fieldId") or "")
        if not field_id:
            errors.append(f"{collection}: missing fieldId")
        elif field_id in seen_ids:
            errors.append(f"duplicate fieldId: {field_id}")
        else:
            seen_ids.add(field_id)

    def check_path(item: dict[str, Any], collection: str) -> None:
        field_id = item.get("fieldId", "<unknown>")
        path = item.get("jsonPath")
        if item.get("review") == "ok" and not is_nonempty_json_path(path):
            warnings.append(f"{collection}.{field_id}: review ok but jsonPath is missing")
        if is_nonempty_json_path(path) and path not in allowed_paths:
            warnings.append(f"{collection}.{field_id}: jsonPath not found in schemaPaths: {path}")
        secondary = item.get("jsonPathSecondary")
        if is_nonempty_json_path(secondary) and secondary not in allowed_paths:
            warnings.append(f"{collection}.{field_id}: jsonPathSecondary not found in schemaPaths: {secondary}")

    def add_rect(item: dict[str, Any], rect_key: str, collection: str) -> None:
        field_id = str(item.get("fieldId") or collection)
        rect = item.get(rect_key)
        if not valid_rect(rect):
            errors.append(f"{collection}.{field_id}: invalid {rect_key}")
            return
        page = int(item.get("page") or 0)
        if page <= 0:
            errors.append(f"{collection}.{field_id}: invalid page")
            return
        rects_by_page.setdefault(page, []).append((field_id, [float(v) for v in rect]))

    for collection in ["fields", "dateFields"]:
        for item in mapping.get(collection, []):
            check_id(item, collection)
            check_path(item, collection)
            add_rect(item, "fillRect", collection)
            reason = production_skip_reason(item, "date" if collection == "dateFields" else "field")
            if reason and item.get("review") == "ok":
                warnings.append(f"{collection}.{item.get('fieldId')}: {reason}")
            if collection == "dateFields" and not item.get("format"):
                warnings.append(f"{collection}.{item.get('fieldId')}: date field is missing format")
            if collection == "dateFields" and item.get("valueTransform") not in {None, "date", "date_range"}:
                warnings.append(f"{collection}.{item.get('fieldId')}: date field has non-date transform")

    for group in mapping.get("checkboxGroups", []):
        check_id(group, "checkboxGroups")
        check_path(group, "checkboxGroups")
        if group.get("review") == "ok" and not is_nonempty_json_path(group.get("jsonPath")):
            warnings.append(f"checkboxGroups.{group.get('fieldId')}: review ok but jsonPath is missing")
        if not group.get("options"):
            warnings.append(f"checkboxGroups.{group.get('fieldId')}: no options")
        for index, option in enumerate(group.get("options", []), 1):
            rect = option.get("boxRect")
            if not valid_rect(rect):
                errors.append(f"checkboxGroups.{group.get('fieldId')}.option{index}: invalid boxRect")

    for page, items in rects_by_page.items():
        for i, (left_id, left_rect) in enumerate(items):
            for right_id, right_rect in items[i + 1:]:
                if rect_overlap(left_rect, right_rect) > 0.65:
                    warnings.append(f"overlapping fill rects on page {page}: {left_id} and {right_id}")

    return {"errors": errors, "warnings": warnings}


def fit_text(c: canvas.Canvas, text: str, rect: list[float], font_size: float) -> float:
    x1, _y1, x2, _y2 = rect
    size = font_size
    while size > 5 and stringWidth(text, "Helvetica", size) > (x2 - x1):
        size -= 0.5
    c.setFont("Helvetica", size)
    return size


def draw_text_field(c: canvas.Canvas, value: Any, rect: list[float], font_size: float = 9, overflow: str = "shrink_then_clip") -> bool:
    if value is None or value == "":
        return False
    text = str(value)
    x1, y1, x2, y2 = rect
    if "\n" in text or len(text) > 80 or (y2 - y1) > 24:
        c.saveState()
        path = c.beginPath()
        path.rect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))
        c.clipPath(path, stroke=0, fill=0)
        c.setFont("Helvetica", font_size)
        max_chars = max(10, int((x2 - x1) / (font_size * 0.48)))
        lines: list[str] = []
        for para in text.splitlines() or [text]:
            lines.extend(wrap(para, max_chars) or [""])
        line_height = font_size + 2
        y = y2 - font_size
        drawn = 0
        for line in lines:
            if y < y1 + 2:
                break
            c.drawString(x1 + 1, y, line)
            y -= line_height
            drawn += 1
        c.restoreState()
        return drawn < len(lines)
    c.saveState()
    path = c.beginPath()
    path.rect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))
    c.clipPath(path, stroke=0, fill=0)
    size = fit_text(c, text, rect, font_size)
    c.drawString(x1 + 1, y1 + max(2, (y2 - y1 - font_size) / 2), text)
    c.restoreState()
    return stringWidth(text, "Helvetica", size) > (x2 - x1)


def draw_check(c: canvas.Canvas, rect: list[float]) -> None:
    x1, y1, x2, y2 = rect
    c.setStrokeColor(black)
    c.setLineWidth(1.2)
    c.line(x1 + 1, y1 + 1, x2 - 1, y2 - 1)
    c.line(x1 + 1, y2 - 1, x2 - 1, y1 + 1)


def should_check(value: Any, option: dict[str, Any]) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = normalize_space(value).lower()
    targets = [normalize_space(option.get("value", "")).lower(), normalize_space(option.get("label", "")).lower()]
    return any(target and target in text for target in targets)


def fill_pdf(pdf_path: Path, mapping_path: Path, data_path: Path, output_path: Path, allow_drift: bool = False) -> dict[str, Any]:
    mapping = load_json(mapping_path)
    data = load_json(data_path)
    current_fp = compute_fingerprint(pdf_path)
    expected_fp = mapping.get("templateFingerprint", {})
    drift = expected_fp.get("value") != current_fp.get("value")
    if drift and not allow_drift:
        raise RuntimeError("mapping_stale: PDF fingerprint does not match mapping. Use --allow-drift to override.")

    schema_paths = mapping.get("schemaPaths") or extract_json_paths(data)
    apply_runtime_backfills(mapping, schema_paths)
    validation = validate_mapping(mapping, schema_paths)
    if validation["errors"]:
        raise RuntimeError("mapping_invalid: " + "; ".join(validation["errors"]))

    diagnostics: dict[str, Any] = {
        "filledFields": [],
        "skippedFields": [],
        "skippedReasons": {},
        "warnings": list(validation["warnings"]),
    }

    def skip(field: dict[str, Any], reason: str) -> None:
        field_id = str(field.get("fieldId") or field.get("label") or "<unknown>")
        diagnostics["skippedFields"].append(
            {
                "fieldId": field_id,
                "reason": reason,
                "jsonPath": field.get("jsonPath"),
            }
        )
        diagnostics["skippedReasons"][reason] = diagnostics["skippedReasons"].get(reason, 0) + 1

    def filled(field: dict[str, Any], value: Any, kind: str, truncated: bool = False, rect: list[float] | None = None) -> None:
        item = {
            "fieldId": field.get("fieldId"),
            "type": kind,
            "jsonPath": field.get("jsonPath"),
            "renderedValuePreview": preview_value(value),
        }
        if rect:
            item["renderedRect"] = rect
        if truncated:
            item["truncated"] = True
            diagnostics["warnings"].append(f"{field.get('fieldId')}: rendered value was truncated to fit fillRect")
        diagnostics["filledFields"].append(item)

    reader = PdfReader(str(pdf_path))
    text_runs = extract_text_runs_pymupdf(pdf_path) or extract_text_runs_pypdf(reader)
    with tempfile.TemporaryDirectory(prefix="pdf_fill_") as tmp:
        overlay = Path(tmp) / "overlay.pdf"
        first = reader.pages[0].mediabox
        c = canvas.Canvas(str(overlay), pagesize=(float(first.width), float(first.height)))
        by_page: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        for field in mapping.get("fields", []):
            if field.get("page"):
                by_page.setdefault(int(field["page"]), []).append(("field", field))
        for field in mapping.get("dateFields", []):
            if field.get("page"):
                by_page.setdefault(int(field["page"]), []).append(("date", field))
        for group in mapping.get("checkboxGroups", []):
            page = None
            for option in group.get("options", []):
                rect = option.get("boxRect")
                if rect:
                    page = group.get("page") or option.get("page")
                    break
            if page:
                by_page.setdefault(int(page), []).append(("checkbox_group", group))

        for page_index, page in enumerate(reader.pages):
            c.setPageSize((float(page.mediabox.width), float(page.mediabox.height)))
            for kind, field in by_page.get(page_index + 1, []):
                if kind in {"field", "date"}:
                    if field.get("review") != "ok":
                        skip(field, "review_not_ok")
                        continue
                    semantic_reason = production_skip_reason(field, kind)
                    if semantic_reason:
                        skip(field, semantic_reason)
                        continue
                    value, reason = format_mapped_value(data, field, "date" if kind == "date" else "text")
                    if reason:
                        skip(field, reason)
                        if reason in {"invalid_date", "invalid_date_range"}:
                            diagnostics["warnings"].append(f"{field.get('fieldId')}: {reason} for {field.get('jsonPath')}")
                        continue
                    safe_rect, layout_warnings = sanitize_fill_rect(field, float(page.mediabox.width), text_runs)
                    for warning in layout_warnings:
                        diagnostics["warnings"].append(f"{field.get('fieldId')}: {warning}")
                    if not safe_rect:
                        skip(field, "layout_no_safe_fill_rect")
                        continue
                    truncated = draw_text_field(c, value, safe_rect, field.get("fontSize", 9), field.get("overflow", "shrink_then_clip"))
                    filled(field, value, kind, truncated, safe_rect)
                elif kind == "checkbox_group":
                    if field.get("review") != "ok":
                        skip(field, "review_not_ok")
                        continue
                    if not is_nonempty_json_path(field.get("jsonPath")):
                        skip(field, "missing_json_path")
                        continue
                    value = resolve_json_path(data, field.get("jsonPath"))
                    if value is None or value == "" or isinstance(value, (dict, list)):
                        skip(field, "empty_or_non_scalar_value")
                        continue
                    matched = False
                    for option in field.get("options", []):
                        if should_check(value, option):
                            draw_check(c, option["boxRect"])
                            matched = True
                            if not field.get("multiSelect", False):
                                break
                    if matched:
                        filled(field, value, kind)
                    else:
                        diagnostics["warnings"].append(f"{field.get('fieldId')}: no checkbox option matched value '{preview_value(value)}'")
                        skip(field, "checkbox_no_match")
            c.showPage()
        c.save()

        writer = PdfWriter()
        overlay_reader = PdfReader(str(overlay))
        for i, page in enumerate(reader.pages):
            page.merge_page(overlay_reader.pages[i])
            writer.add_page(page)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fh:
            writer.write(fh)

    return {
        "output": str(output_path),
        "mappingDrift": drift,
        "fields": len(mapping.get("fields", [])),
        "dateFields": len(mapping.get("dateFields", [])),
        "checkboxGroups": len(mapping.get("checkboxGroups", [])),
        "diagnostics": diagnostics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create template mappings or fill payer PDFs from JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create_mapping")
    create.add_argument("--pdf", type=Path, required=True)
    create.add_argument("--schema", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--template-id")
    create.add_argument("--llm", action="store_true", help="Use GPT to select candidates and map them to schema paths.")
    create.add_argument("--model", default="gpt-4.1-mini")
    create.add_argument("--env-file", type=Path)

    fill = sub.add_parser("fill_pdf")
    fill.add_argument("--pdf", type=Path, required=True)
    fill.add_argument("--mapping", type=Path, required=True)
    fill.add_argument("--data", type=Path, required=True)
    fill.add_argument("--out", type=Path, required=True)
    fill.add_argument("--allow-drift", action="store_true")

    args = parser.parse_args()
    if args.command == "create_mapping":
        mapping = build_mapping(args.pdf, args.schema, args.out, args.template_id, args.llm, args.model, args.env_file)
        print(json.dumps({"out": str(args.out), "candidateCount": mapping["candidateCount"], "reviewStatus": mapping["reviewStatus"]}, indent=2))
    elif args.command == "fill_pdf":
        result = fill_pdf(args.pdf, args.mapping, args.data, args.out, args.allow_drift)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
