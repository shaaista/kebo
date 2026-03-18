from __future__ import annotations

import csv
import copy
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List

import fitz  # type: ignore
from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI

import numpy as np
from config import LegendConfig, ALLOWED_LABELS, TOKEN_SYNONYMS, PHRASE_SYNONYMS
from docai_client import process_pdf_with_docai, DocAIConfig
from full_menu_ocr import FullMenuConfig, FullMenuOCR
from legend_extractor import LegendExtractor
from menu_pipeline import copy_icon_details, create_output_dir


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_OCR_EXTS = {
    ".pdf",
    *SUPPORTED_IMAGE_EXTS,
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".ppt",
    ".pptx",
    ".odp",
    ".ods",
    ".xls",
    ".xlsx",
}

MODEL_PRICING_USD_PER_1M: Dict[str, Dict[str, float]] = {
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "gpt-4.1-nano": {"input": 0.1, "output": 0.4},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
}

OPENAI_MENU_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["menu_name", "items", "other_text", "footer_text", "notes"],
    "properties": {
        "menu_name": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "description",
                    "price",
                    "kcal",
                    "allergens",
                    "veg",
                    "non_veg",
                    "page",
                    "dish_type",
                    "confidence",
                ],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "price": {"type": ["string", "null"]},
                    "kcal": {"type": ["string", "number", "null"]},
                    "allergens": {"type": "array", "items": {"type": "string"}},
                    "veg": {"type": ["string", "null"]},
                    "non_veg": {"type": ["string", "null"]},
                    "page": {"type": "integer"},
                    "dish_type": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "overall",
                            "name",
                            "price",
                            "dish_type",
                            "dietary",
                            "allergens",
                            "reason",
                        ],
                        "properties": {
                            "overall": {"type": ["number", "null"]},
                            "name": {"type": ["number", "null"]},
                            "price": {"type": ["number", "null"]},
                            "dish_type": {"type": ["number", "null"]},
                            "dietary": {"type": ["number", "null"]},
                            "allergens": {"type": ["number", "null"]},
                            "reason": {"type": ["string", "null"]},
                        },
                    },
                    "extras": {"type": "object", "additionalProperties": True},
                },
            },
        },
        "other_text": {"type": "array", "items": {"type": "string"}},
        "footer_text": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "page_extras": {"type": "object", "additionalProperties": True},
    },
}


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_json_maybe(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_text_with_fallbacks(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _find_soffice_executable() -> str | None:
    for candidate in ("soffice", "soffice.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    common_paths = (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    )
    for path in common_paths:
        p = Path(path)
        if p.exists():
            return str(p)
    return None


def _convert_office_to_pdf(input_path: Path, timeout_sec: int = 240) -> Path:
    soffice = _find_soffice_executable()
    if not soffice:
        raise RuntimeError("Document conversion requires LibreOffice (soffice).")
    out_dir = input_path.parent
    before = {p.resolve() for p in out_dir.glob("*.pdf")}
    cmd = [
        soffice,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(input_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    expected = input_path.with_suffix(".pdf")
    if expected.exists():
        return expected
    after = {p.resolve() for p in out_dir.glob("*.pdf")}
    created = [Path(p) for p in (after - before)]
    if len(created) == 1 and created[0].exists():
        return created[0]
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    detail = stderr or stdout or f"exit code {proc.returncode}"
    raise RuntimeError(f"Could not convert document to PDF: {detail}")


def _convert_image_to_pdf(image_path: Path) -> Path:
    out_pdf = image_path.with_suffix(".pdf")
    img_doc = fitz.open(str(image_path))
    try:
        pdf_bytes = img_doc.convert_to_pdf()
    finally:
        img_doc.close()
    pdf_doc = fitz.open("pdf", pdf_bytes)
    try:
        pdf_doc.save(str(out_pdf))
    finally:
        pdf_doc.close()
    return out_pdf


def _ensure_pdf_input(input_path: Path) -> Path:
    ext = input_path.suffix.lower()
    if ext == ".pdf":
        return input_path
    if ext in SUPPORTED_IMAGE_EXTS:
        return _convert_image_to_pdf(input_path)
    if ext in SUPPORTED_OCR_EXTS:
        return _convert_office_to_pdf(input_path)
    raise ValueError(f"Unsupported OCR input file type: {ext or '(no extension)'}")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _env_float(name: str) -> float | None:
    raw = _normalize_text(os.getenv(name))
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _normalize_text(os.getenv(name))
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _resolve_pricing_per_1m(model: str | None) -> tuple[float | None, float | None, str]:
    env_in = _env_float("OPENAI_INPUT_COST_PER_1M_TOKENS")
    env_out = _env_float("OPENAI_OUTPUT_COST_PER_1M_TOKENS")
    if env_in is not None and env_out is not None:
        return env_in, env_out, "env"

    key = _normalize_text(model).lower()
    for model_key, rates in MODEL_PRICING_USD_PER_1M.items():
        if key == model_key or key.startswith(model_key + "-"):
            return float(rates["input"]), float(rates["output"]), "builtin"
    return None, None, "unknown"


def _extract_usage_from_response(response: Any, model: str | None) -> Dict[str, Any]:
    usage_obj = getattr(response, "usage", None)
    if usage_obj is None and isinstance(response, dict):
        usage_obj = response.get("usage")

    def _pick_int(obj: Any, *names: str) -> int:
        for name in names:
            if isinstance(obj, dict) and (name in obj):
                return _to_int(obj.get(name), 0)
            try:
                val = getattr(obj, name)
            except Exception:
                val = None
            if val is not None:
                return _to_int(val, 0)
        return 0

    input_tokens = _pick_int(usage_obj, "input_tokens", "prompt_tokens")
    output_tokens = _pick_int(usage_obj, "output_tokens", "completion_tokens")
    total_tokens = _pick_int(usage_obj, "total_tokens")
    if total_tokens <= 0:
        total_tokens = max(0, input_tokens + output_tokens)

    in_rate, out_rate, rate_source = _resolve_pricing_per_1m(model)
    est_cost = None
    if in_rate is not None and out_rate is not None:
        est_cost = round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0, 6)

    return {
        "model": _normalize_text(model) or None,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
        "estimated_cost_usd": est_cost,
        "pricing_source": rate_source,
        "input_cost_per_1m_tokens_usd": in_rate,
        "output_cost_per_1m_tokens_usd": out_rate,
    }


def _empty_usage(model: str | None) -> Dict[str, Any]:
    in_rate, out_rate, rate_source = _resolve_pricing_per_1m(model)
    return {
        "model": _normalize_text(model) or None,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0 if (in_rate is not None and out_rate is not None) else None,
        "pricing_source": rate_source,
        "input_cost_per_1m_tokens_usd": in_rate,
        "output_cost_per_1m_tokens_usd": out_rate,
    }


def _usage_summary(usage: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    return {
        "model": usage.get("model"),
        "input_tokens": int(_to_int(usage.get("input_tokens"), 0)),
        "output_tokens": int(_to_int(usage.get("output_tokens"), 0)),
        "total_tokens": int(_to_int(usage.get("total_tokens"), 0)),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "pricing_source": usage.get("pricing_source"),
    }

def _ensure_legend_icons_for_pdf(pdf_path: Path, out_dir: Path, processor: FullMenuOCR) -> Path:
    icons_dir = out_dir / "legend_icons"
    if icons_dir.exists() and list(icons_dir.glob("*.png")):
        return icons_dir

    legend = LegendExtractor(
        LegendConfig(dpi=processor.config.legend_dpi, tesseract_cmd=getattr(processor, "_tesseract_cmd", None))
    )
    legend_results = legend.process_pdf(pdf_path, out_dir / "legend")
    (out_dir / "legend_summary.json").write_text(json.dumps(legend_results, indent=2), encoding="utf-8")
    src_icons = out_dir / "legend" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    for p in src_icons.glob("*.png"):
        (icons_dir / p.name).write_bytes(p.read_bytes())

    if list(icons_dir.glob("*.png")):
        return icons_dir

    # Fallback: reuse the most recent successful legend icons for the same document stem.
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    stem_slug = _slug(pdf_path.stem)
    root = out_dir.parent
    candidates = []
    for d in root.iterdir():
        if not d.is_dir() or d.resolve() == out_dir.resolve():
            continue
        if stem_slug and stem_slug not in _slug(d.name):
            continue
        c_icons = d / "legend_icons"
        if c_icons.exists() and list(c_icons.glob("*.png")):
            try:
                mtime = c_icons.stat().st_mtime
            except Exception:
                mtime = 0.0
            candidates.append((mtime, c_icons))
    if candidates:
        candidates.sort(key=lambda t: t[0], reverse=True)
        best = candidates[0][1]
        for p in best.glob("*.png"):
            (icons_dir / p.name).write_bytes(p.read_bytes())
    return icons_dir


def _clone_legend_icons(src_icons_dir: Path, dst_icons_dir: Path) -> None:
    dst_icons_dir.mkdir(parents=True, exist_ok=True)
    for p in src_icons_dir.glob("*.png"):
        (dst_icons_dir / p.name).write_bytes(p.read_bytes())


def _page_raw_text_from_page_data(page_no: int, page_data: Dict[str, Any]) -> str:
    lines = page_data.get("lines", []) if isinstance(page_data, dict) else []
    out: List[str] = [f"[PAGE {page_no}]"]
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        text = _normalize_text(line.get("text"))
        if text:
            out.append(text)
    if len(out) <= 1:
        fallback = _normalize_text(page_data.get("page_text") if isinstance(page_data, dict) else "")
        if fallback:
            out.append(fallback)
    return "\n".join(out).strip()


def _docai_text_from_anchor(doc_text: str, layout: Dict[str, Any]) -> str:
    if not isinstance(layout, dict):
        return ""
    anchor = layout.get("textAnchor")
    if not isinstance(anchor, dict):
        anchor = layout.get("text_anchor")
    if not isinstance(anchor, dict):
        return ""
    segments = anchor.get("textSegments")
    if not isinstance(segments, list):
        segments = anchor.get("text_segments")
    if not isinstance(segments, list):
        return ""
    parts: List[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        start_raw = seg.get("startIndex", seg.get("start_index", 0))
        end_raw = seg.get("endIndex", seg.get("end_index", 0))
        try:
            start = int(start_raw)
        except Exception:
            start = 0
        try:
            end = int(end_raw)
        except Exception:
            end = start
        if end > start and start < len(doc_text):
            parts.append(doc_text[start:end])
    return "".join(parts)


def _docai_bbox_from_layout(layout: Dict[str, Any], page_w: float | None, page_h: float | None) -> List[float] | None:
    if not isinstance(layout, dict):
        return None
    poly = layout.get("boundingPoly")
    if not isinstance(poly, dict):
        poly = layout.get("bounding_poly")
    if not isinstance(poly, dict):
        return None

    norm_vertices = poly.get("normalizedVertices")
    vertices = poly.get("vertices")

    xs: List[float] = []
    ys: List[float] = []
    if isinstance(norm_vertices, list) and norm_vertices and page_w and page_h:
        for v in norm_vertices:
            if not isinstance(v, dict):
                continue
            xs.append(_to_float(v.get("x")) * page_w)
            ys.append(_to_float(v.get("y")) * page_h)
    elif isinstance(vertices, list) and vertices:
        for v in vertices:
            if not isinstance(v, dict):
                continue
            xs.append(_to_float(v.get("x")))
            ys.append(_to_float(v.get("y")))
    elif isinstance(norm_vertices, list) and norm_vertices:
        for v in norm_vertices:
            if not isinstance(v, dict):
                continue
            xs.append(_to_float(v.get("x")))
            ys.append(_to_float(v.get("y")))
    if not xs or not ys:
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _infer_page_canvas_size(page_obj: Dict[str, Any]) -> tuple[float | None, float | None]:
    if not isinstance(page_obj, dict):
        return None, None
    x_max = 0.0
    y_max = 0.0

    def _read_bbox(item: Any) -> List[float] | None:
        if not isinstance(item, dict):
            return None
        bbox = item.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            return None
        return [_to_float(bbox[0]), _to_float(bbox[1]), _to_float(bbox[2]), _to_float(bbox[3])]

    for key in ("lines", "blocks", "icons"):
        values = page_obj.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            bbox = _read_bbox(item)
            if not bbox:
                continue
            x_max = max(x_max, bbox[2])
            y_max = max(y_max, bbox[3])

    if x_max <= 0 or y_max <= 0:
        return None, None
    return x_max, y_max


def _bbox_center(box: List[float] | None) -> tuple[float, float] | None:
    if not isinstance(box, list) or len(box) < 4:
        return None
    return ((float(box[0]) + float(box[2])) / 2.0, (float(box[1]) + float(box[3])) / 2.0)


def _line_match_for_icon(icon_bbox: List[float] | None, lines: List[Dict[str, Any]]) -> int | None:
    icon_center = _bbox_center(icon_bbox)
    if icon_center is None:
        return None
    ix, iy = icon_center
    best_idx: int | None = None
    best_score: float | None = None
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        bbox = line.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        lx0, ly0, lx1, ly1 = _to_float(bbox[0]), _to_float(bbox[1]), _to_float(bbox[2]), _to_float(bbox[3])
        lcx, lcy = (lx0 + lx1) / 2.0, (ly0 + ly1) / 2.0
        y_dist = abs(iy - lcy)
        if ix < lx0:
            x_dist = lx0 - ix
        elif ix > lx1:
            x_dist = ix - lx1
        else:
            x_dist = 0.0
        score = (y_dist * 2.0) + x_dist
        if best_score is None or score < best_score:
            best_score = score
            best_idx = idx
    return best_idx


def _extract_docai_page_raw(docai_entry: Dict[str, Any]) -> Dict[str, Any]:
    page_no = _to_int(docai_entry.get("page"), 1)
    docai = docai_entry.get("docai")
    doc = docai.get("document") if isinstance(docai, dict) and isinstance(docai.get("document"), dict) else docai
    if not isinstance(doc, dict):
        return {
            "page": page_no,
            "width": None,
            "height": None,
            "raw_text": f"[PAGE {page_no}]",
            "lines": [],
        }

    pages = doc.get("pages")
    page_obj = pages[0] if isinstance(pages, list) and pages and isinstance(pages[0], dict) else {}
    dim = page_obj.get("dimension") if isinstance(page_obj, dict) else {}
    dim = dim if isinstance(dim, dict) else {}
    page_w = _to_float(dim.get("width"), 0.0) or None
    page_h = _to_float(dim.get("height"), 0.0) or None

    line_items = page_obj.get("lines") if isinstance(page_obj, dict) else None
    if not isinstance(line_items, list) or not line_items:
        line_items = page_obj.get("paragraphs") if isinstance(page_obj, dict) else None
    if not isinstance(line_items, list) or not line_items:
        line_items = page_obj.get("blocks") if isinstance(page_obj, dict) else None
    if not isinstance(line_items, list):
        line_items = []

    doc_text = str(doc.get("text") or "")
    lines_out: List[Dict[str, Any]] = []
    for i, line_item in enumerate(line_items):
        if not isinstance(line_item, dict):
            continue
        layout = line_item.get("layout") if isinstance(line_item.get("layout"), dict) else {}
        text_raw = _docai_text_from_anchor(doc_text, layout)
        if not _normalize_text(text_raw):
            continue
        bbox = _docai_bbox_from_layout(layout, page_w, page_h)
        lines_out.append(
            {
                "line_index": i,
                "text": text_raw.rstrip("\r\n"),
                "bbox": bbox,
                "icons": [],
            }
        )

    raw_text = doc_text.strip()
    if raw_text and not raw_text.lower().startswith("[page"):
        raw_text = f"[PAGE {page_no}]\n{raw_text}"
    if not raw_text:
        raw_text = _page_raw_text_from_page_data(page_no, {"lines": lines_out})

    return {
        "page": page_no,
        "width": page_w,
        "height": page_h,
        "raw_text": raw_text,
        "lines": lines_out,
    }


def _normalize_token_simple(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tok.lower())

def check_footer_has_icons(merged: Dict[str, Any], footer_ratio: float = 0.25) -> List[str]:
    all_text = merged.get("text", "") or ""
    if not all_text.strip():
        return []
    words = re.findall(r"[A-Za-z0-9]+", all_text)
    tokens = [_normalize_token_simple(w) for w in words]
    found_labels = set()
    for tok in tokens:
        label = TOKEN_SYNONYMS.get(tok)
        if label and label in ALLOWED_LABELS:
            found_labels.add(label)
    for length in (3, 2):
        for i in range(len(tokens) - length + 1):
            phrase = tuple(tokens[i:i + length])
            label = PHRASE_SYNONYMS.get(phrase)
            if label and label in ALLOWED_LABELS:
                found_labels.add(label)
    if len(found_labels) >= 2:
        return sorted(found_labels)
    return []

def _extract_lines_from_fitz_page(page, dpi: float = 350.0) -> List[Dict[str, Any]]:
    scale = dpi / 72.0
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    lines: List[Dict[str, Any]] = []
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line_data in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line_data.get("spans", []))
            text = text.strip()
            if not text:
                continue
            bx0, by0, bx1, by1 = line_data["bbox"]
            lines.append({
                "text": text,
                "bbox": [bx0 * scale, by0 * scale, bx1 * scale, by1 * scale],
                "icons": [],
            })
    return lines

def run_icon_detection_logic(
    pdf_input: Path,
    merged_data: Dict[str, Any],
    out_dir: Path,
) -> Dict[str, Any]:
    """
    Core icon logic matching reference project (ocrbadv6):
    1. Extract legend & load templates
    2. For each page: convert DocAI lines to image-pixel coords, run icon detection,
       propagate/merge/filter icons, then write icons back to merged_data lines.
    """
    DPI = 600
    results = {"pages": {}, "legend": {}}
    pdf_bytes = pdf_input.read_bytes()
    
    # Setup directories
    legend_dir = out_dir / "legend"
    legend_dir.mkdir(parents=True, exist_ok=True)
    icons_dir = out_dir / "legend_icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Legend Extraction
    legend_cfg = LegendConfig(dpi=600)
    extractor = LegendExtractor(legend_cfg)
    try:
        legend_results = extractor.process_pdf(pdf_input, legend_dir)
        (out_dir / "legend_summary.json").write_text(
             json.dumps(legend_results, indent=2, default=str), encoding="utf-8"
        )
    except Exception as e:
        return {"error": f"LegendExtractor failed: {e}", "pages": {}}

    # Copy raw icons
    src_icons = legend_dir / "icons"
    if src_icons.exists():
        for p in src_icons.glob("*.png"):
            (icons_dir / p.name).write_bytes(p.read_bytes())
            
    # Labels summary
    legend_labels: Dict[str, str] = {}
    for page_res in legend_results:
        for label, icon_path in (page_res.get("icon_map", {}) or {}).items():
            if label not in legend_labels:
                legend_labels[label] = str(icon_path)
    
    results["legend"] = {
        "labels_found": sorted(legend_labels.keys()),
        "icon_count": len(legend_labels),
        "icons_dir": str(icons_dir),
    }
    
    if not legend_labels:
        print("DEBUG: No legend labels found via LegendExtractor. Skipping icon detection.")
        return results

    print(f"DEBUG: LegendExtractor found {len(legend_labels)} labels: {list(legend_labels.keys())}")

    # 2. Match Icons on Pages â€” following reference project (ocrbadv6) flow exactly
    ocr_config = FullMenuConfig(dpi=DPI, legend_dpi=600)
    ocr_processor = FullMenuOCR(ocr_config)
    templates = ocr_processor._load_templates(icons_dir)
    
    if not templates:
        print("DEBUG: No icon templates loaded â€” skipping icon detection.")
        return results
    print(f"DEBUG: Loaded {len(templates)} icon templates.")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    for page_idx in range(len(doc)):
        page_num = page_idx + 1
        page = doc[page_idx]
        
        # Render page to image at DPI
        mat = fitz.Matrix(DPI / 72.0, DPI / 72.0)
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.samples
        if pix.n == 4:
            image_rgb = np.frombuffer(img_data, dtype=np.uint8).reshape(pix.h, pix.w, 4)[:, :, :3]
        else:
            image_rgb = np.frombuffer(img_data, dtype=np.uint8).reshape(pix.h, pix.w, 3)
        image_rgb = np.ascontiguousarray(image_rgb)
        img_h, img_w = image_rgb.shape[:2]
        
        # Get DocAI page data
        docai_page = next((p for p in merged_data.get("pages", []) if p.get("page") == page_num), None)
        if not docai_page or not isinstance(docai_page, dict):
            print(f"DEBUG: Page {page_num} - No DocAI page data, skipping.")
            continue
        
        # Get page dimensions from DocAI (in points)
        page_dim = docai_page.get("page_dimension", {})
        doc_w = page_dim.get("width") or img_w
        doc_h = page_dim.get("height") or img_h
        
        # Scale factors: convert DocAI point coords â†’ image pixel coords
        # This is exactly how the reference project does it in _docai_extract_lines
        scale_x = img_w / float(doc_w) if doc_w else 1.0
        scale_y = img_h / float(doc_h) if doc_h else 1.0
        
        # Build lines list with bboxes in IMAGE PIXEL coordinates
        lines = []
        for i, ln in enumerate(docai_page.get("lines", [])):
            if not isinstance(ln, dict):
                continue
            text = (ln.get("text") or "").strip()
            if not text:
                continue
            bbox = ln.get("bbox", [0, 0, 0, 0])
            if bbox == [0, 0, 0, 0] or bbox == [0.0, 0.0, 0.0, 0.0]:
                continue
            # Convert from DocAI point coords to image pixel coords
            lines.append({
                "text": text,
                "bbox": [
                    bbox[0] * scale_x,
                    bbox[1] * scale_y,
                    bbox[2] * scale_x,
                    bbox[3] * scale_y,
                ],
                "icons": [],
                "original_index": i  # Track index to map back to docai_lines
            })
        
        if not lines:
            print(f"DEBUG: Page {page_num} - No lines with valid bboxes, skipping.")
            continue
        
        # Step 2: Detect icons by line (same as reference project)
        try:
            detections, lines_with_icons = ocr_processor._detect_icons_by_line(
                image_rgb=image_rgb,
                templates=templates,
                lines=lines,
                output_dir=out_dir,
            )
            print(f"DEBUG: Page {page_num} - DetectIconsByLine found {len(detections)} detections on {len(lines)} lines.")
        except Exception as e:
            print(f"DEBUG: DetectIconsByLine crashed on page {page_num}: {e}")
            import traceback
            traceback.print_exc()
            continue
        
        # Step 3: Propagate icons to dish lines (REFERENCE METHOD)
        # This moves icons from "garbage" lines (prices, checkboxes) to the main dish line in the row.
        try:
            lines_with_icons = ocr_processor._propagate_icons_to_rows(lines_with_icons)
            lines_with_icons = ocr_processor._filter_icons_to_dish_lines(lines_with_icons)
        except Exception as e:
            print(f"DEBUG: Propagation crashed on page {page_num}: {e}")
            import traceback
            traceback.print_exc()

        # Step 4: Write detected icons back to DocAI lines using original_index
        docai_lines = docai_page.get("lines", [])
        icon_write_count = 0
        
        for ln in lines_with_icons:
            orig_idx = ln.get("original_index")
            if orig_idx is not None and 0 <= orig_idx < len(docai_lines):
                detected_icons = ln.get("icons", [])
                if detected_icons:
                    if "icons" not in docai_lines[orig_idx]:
                        docai_lines[orig_idx]["icons"] = []
                    current = set(docai_lines[orig_idx]["icons"])
                    for icon in detected_icons:
                        if icon not in current:
                            docai_lines[orig_idx]["icons"].append(icon)
                            current.add(icon)
                    icon_write_count += 1
        
        # Re-build line_ys for raw detection assignment (geographic fallback)
        # WE USE ALL LINES. User request: "line by line i want icon to be deteted... whatever icon label is detetced, put that in page raw json with its cooridnates"
        line_ys = []
        for li, dln in enumerate(docai_lines):
            if not isinstance(dln, dict):
                continue
            bb = dln.get("bbox", [0, 0, 0, 0])
            y_center = ((bb[1] + bb[3]) / 2.0) * scale_y
            line_ys.append((y_center, li))
        
        # Assign RAW detections to closest line (geographic) for coordinate visibility
        raw_detection_count = 0
        for det in detections:
            det_bbox = det.get("bbox", [0, 0, 0, 0])
            det_y = (det_bbox[1] + det_bbox[3]) / 2.0
            label = det.get("label", "")
            if not label:
                continue
            
            # Find closest line by Y-center
            best_idx = None
            best_dist = float("inf")
            for y_c, li in line_ys:
                dist = abs(det_y - y_c)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = li
            
            if best_idx is not None and best_idx < len(docai_lines):
                ln = docai_lines[best_idx]
                
                # Add FULL detection details to "icon_detections" (User request: "with its cooridnates")
                if "icon_detections" not in ln or not isinstance(ln.get("icon_detections"), list):
                    ln["icon_detections"] = []
                
                # Normalize bbox to page dimensions (0..1) for consistency with DocAI
                norm_bbox = [
                    det_bbox[0] / img_w if img_w else 0,
                    det_bbox[1] / img_h if img_h else 0,
                    det_bbox[2] / img_w if img_w else 0,
                    det_bbox[3] / img_h if img_h else 0,
                ]
                
                ln["icon_detections"].append({
                    "label": label,
                    "bbox_pixels": det_bbox,
                    "bbox_normalized": norm_bbox,
                    "score": round(det.get("score", 1.0), 4),
                    "source": "icon_detection" 
                })
                raw_detection_count += 1
        
        print(f"DEBUG: Page {page_num} - Propagated icons to {icon_write_count} lines. Assigned {raw_detection_count} raw detections.")
        
        # Store results for debug/reference
        page_lines = []
        for i, ln in enumerate(lines_with_icons):
            page_lines.append({
                "line_index": i,
                "text": ln.get("text", ""),
                "bbox": ln.get("bbox", [0, 0, 0, 0]),
                "icons": ln.get("icons", []),
            })
            
        page_detections = {}
        for det in detections:
            l_idx = det["line_index"]
            if l_idx not in page_detections:
                 page_detections[l_idx] = []
            page_detections[l_idx].append({
                "label": det.get("label", ""),
                "bbox": det.get("bbox", []),
                "score": round(det.get("score", 0.0), 4),
            })
            
        results["pages"][page_num] = {
            "lines": page_lines,
            "detections": page_detections,
            "icon_count": len(detections),
            "source": "docai"
        }
        
    doc.close()
    return results

def _norm_merge_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())

def merge_icons_into_output(merged: Dict[str, Any], icon_results: Dict[str, Any]) -> Dict[str, Any]:
    print("DEBUG: Entering merge_icons_into_output")
    merged = dict(merged)
    merged["legend"] = icon_results.get("legend", {})
    icon_pages = icon_results.get("pages", {})
    print(f"DEBUG: icon_pages keys: {list(icon_pages.keys())}")
    
    for page_data in merged.get("pages", []):
        page_num = page_data.get("page")
        print(f"DEBUG: Processing DocAI page {page_num} (type: {type(page_num)})")
        
        icon_page = icon_pages.get(page_num, {})
        # Fallback for str/int mismatch
        if not icon_page:
            try:
                if isinstance(page_num, str):
                    icon_page = icon_pages.get(int(page_num), {})
                elif isinstance(page_num, int):
                    # Try string
                    icon_page = icon_pages.get(str(page_num), {})
            except: pass

        if not icon_page or not icon_page.get("lines"):
             print(f"DEBUG: No icon data found for page {page_num}")
             continue
             
        icon_lines = icon_page["lines"]
        icon_detections_map = icon_page.get("detections", {})
        print(f"DEBUG: Found {len(icon_lines)} icon_lines for page {page_num}. Merging...")
        
        for i, lp_line in enumerate(page_data.get("lines", [])):
            lp_raw = (lp_line.get("text", "") or "").strip()
            lp_norm = _norm_merge_text(lp_raw)
            if not lp_norm:
                continue
            
            best_match_icons = []
            best_det = []
            
            # 1. Try fuzzy match by normalized string containment
            for idx, il in enumerate(icon_lines):
                il_raw = (il.get("text") or "").strip()
                il_norm = _norm_merge_text(il_raw)
                if not il_norm: continue
                
                # Check for high overlap
                if lp_norm in il_norm or il_norm in lp_norm:
                     if il.get("icons"):
                         best_match_icons = il["icons"]
                         best_det = icon_detections_map.get(idx, [])
                         print(f"DEBUG: MATCH FOUND! '{lp_raw}' matched '{il_raw}' with icons: {best_match_icons}") 
                         break # Found a good match
            
            if not best_match_icons and lp_norm:
                 print(f"DEBUG: No match for '{lp_raw}' (norm: {lp_norm})")

            if best_match_icons:
                lp_line["icons"] = best_match_icons
                lp_line["icon_detections"] = best_det
                
                # Backfill bbox from PyMuPDF match if missing/zero in DocAI line
                # This solves the user complaint about 0.0 bboxes
                current_bbox = lp_line.get("bbox", [0.0, 0.0, 0.0, 0.0])
                if all(v == 0.0 for v in current_bbox):
                    # Find the source line for this match
                    # best_match_icons came from either candidate (direct index) or an item in search loop
                    
                    matched_line_bbox = None
                    if i < len(icon_lines) and icon_lines[i].get("icons") == best_match_icons:
                         matched_line_bbox = icon_lines[i].get("bbox")
                    else:
                        # search found it
                         for il in icon_lines:
                             if il.get("icons") == best_match_icons:
                                 matched_line_bbox = il.get("bbox")
                                 break
                    
                    if matched_line_bbox:
                        lp_line["bbox"] = matched_line_bbox
                        # Also set column_bbox for consistency
                        lp_line["column_bbox"] = [matched_line_bbox[0], matched_line_bbox[1]]

    return merged


def _process_pdf_with_docai_page_by_page(
    pdf_input: Path,
    out_dir: Path,
    processor: FullMenuOCR,
) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    icons_dir = _ensure_legend_icons_for_pdf(pdf_input, out_dir, processor)
    raw_pages: List[Dict[str, Any]] = []
    page_docai_raw: List[Dict[str, Any]] = []
    raw_text_parts: List[str] = []

    with fitz.open(pdf_input) as doc:
        with tempfile.TemporaryDirectory(prefix="docai_pages_", dir=str(out_dir)) as tmp_root:
            tmp_root_path = Path(tmp_root)
            for page_idx in range(len(doc)):
                page_no = page_idx + 1
                one_page_pdf = tmp_root_path / f"page_{page_no:03d}.pdf"
                one_doc = fitz.open()
                try:
                    one_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                    one_doc.save(str(one_page_pdf))
                finally:
                    one_doc.close()

                docai_page = process_pdf_with_docai(one_page_pdf)
                page_docai_raw.append({"page": page_no, "docai": docai_page})

                page_work_dir = tmp_root_path / f"work_{page_no:03d}"
                page_work_dir.mkdir(parents=True, exist_ok=True)
                _clone_legend_icons(icons_dir, page_work_dir / "legend_icons")

                processor.process_docai_document(one_page_pdf, docai_page, page_work_dir, use_openai=False)
                # Keep line-level icon assignment aligned with the legacy attachment path.
                processor.reattach_icons_existing(page_work_dir)
                menu_raw_path = page_work_dir / "menu_raw.json"
                page_menu = (
                    json.loads(menu_raw_path.read_text(encoding="utf-8"))
                    if menu_raw_path.exists()
                    else {"pages": []}
                )
                page_entries = page_menu.get("pages", []) if isinstance(page_menu, dict) else []
                page_data = page_entries[0] if isinstance(page_entries, list) and page_entries else {}
                if not isinstance(page_data, dict):
                    page_data = {}
                page_data["page"] = page_no

                raw_pages.append(page_data)
                (out_dir / f"page_{page_no:02d}_raw.json").write_text(
                    json.dumps(page_data, indent=2),
                    encoding="utf-8",
                )

                page_raw_text = str(processor.docai_raw_text(docai_page) or "").strip()
                if not page_raw_text:
                    page_raw_text = _page_raw_text_from_page_data(page_no, page_data)
                if page_raw_text:
                    if not page_raw_text.lower().startswith("[page"):
                        page_raw_text = f"[PAGE {page_no}]\n{page_raw_text}"
                    raw_text_parts.append(page_raw_text)

    menu_raw = {"pdf": str(pdf_input), "pages": raw_pages}
    docai_agg = {"mode": "page_by_page", "pages": page_docai_raw}
    raw_text = "\n\n".join([p for p in raw_text_parts if p]).strip()
    if not raw_text:
        chunks = []
        for page_data in raw_pages:
            try:
                page_no = int(page_data.get("page"))
            except Exception:
                page_no = 1
            chunk = _page_raw_text_from_page_data(page_no, page_data)
            if chunk:
                chunks.append(chunk)
        raw_text = "\n\n".join(chunks).strip()
    return raw_text, menu_raw, docai_agg


def _build_csv_payload(csv_path: Path) -> tuple[str, Dict[str, Any]]:
    raw = _read_text_with_fallbacks(csv_path)
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(raw), dialect)
    rows = [[_normalize_text(c) for c in row] for row in reader]

    lines: List[Dict[str, Any]] = []
    text_lines: List[str] = ["[PAGE 1]", "[BODY]"]
    line_idx = 0
    for r, row in enumerate(rows, start=1):
        cells = [c for c in row if c]
        if not cells:
            continue
        text_lines.append(f"[ROW {r}] " + " | ".join(f"C{i}: {v}" for i, v in enumerate(row, start=1) if v))
        for c, val in enumerate(row, start=1):
            if not val:
                continue
            lines.append(
                {
                    "line_index": line_idx,
                    "text": val,
                    "bbox": None,
                    "layout_role": "body",
                    "column_index": c,
                    "cell": {"row": r, "column": c},
                    "icons": [],
                }
            )
            line_idx += 1

    raw_text = "\n".join(text_lines).strip()
    payload = {
        "input_type": "csv",
        "raw_text": raw_text,
        "pages": [
            {
                "page": 1,
                "width": None,
                "height": None,
                "lines": lines,
                "icons": [],
                "footer": {"has_icons": False, "lines": [], "icons": []},
            }
        ],
    }
    return raw_text, payload


def _build_ocr_payload(
    menu_raw: Dict[str, Any],
    raw_text: str,
    docai_agg: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    menu_pages = menu_raw.get("pages", []) if isinstance(menu_raw, dict) else []
    menu_page_map: Dict[int, Dict[str, Any]] = {}
    ordered_pages: List[int] = []
    for idx, page in enumerate(menu_pages, start=1):
        if not isinstance(page, dict):
            continue
        page_no = _to_int(page.get("page"), idx)
        menu_page_map[page_no] = page
        if page_no not in ordered_pages:
            ordered_pages.append(page_no)

    docai_page_map: Dict[int, Dict[str, Any]] = {}
    if isinstance(docai_agg, dict):
        docai_pages = docai_agg.get("pages")
        if isinstance(docai_pages, list):
            for entry in docai_pages:
                if not isinstance(entry, dict):
                    continue
                page_raw = _extract_docai_page_raw(entry)
                page_no = _to_int(page_raw.get("page"), 1)
                docai_page_map[page_no] = page_raw
                if page_no not in ordered_pages:
                    ordered_pages.append(page_no)

    if not ordered_pages:
        ordered_pages = [1]

    out_pages: List[Dict[str, Any]] = []
    page_raw_chunks: List[str] = []
    for page_no in ordered_pages:
        menu_page = menu_page_map.get(page_no, {})
        docai_page = docai_page_map.get(page_no, {})
        # Prefer processed page lines because they contain icon labels attached per line.
        raw_lines = menu_page.get("lines") if isinstance(menu_page, dict) else None
        if not isinstance(raw_lines, list) or not raw_lines:
            raw_lines = docai_page.get("lines") if isinstance(docai_page, dict) else None
        if not isinstance(raw_lines, list):
            raw_lines = []

        line_entries: List[Dict[str, Any]] = []
        for idx, ln in enumerate(raw_lines):
            if not isinstance(ln, dict):
                continue
            entry = dict(ln)
            if "line_index" not in entry:
                entry["line_index"] = idx
            line_entries.append(entry)

        page_icons = (menu_page.get("icons") if isinstance(menu_page, dict) else []) or []
        icon_entries: List[Dict[str, Any]] = [dict(icon) for icon in page_icons if isinstance(icon, dict)]

        page_w = docai_page.get("width") if isinstance(docai_page, dict) else None
        page_h = docai_page.get("height") if isinstance(docai_page, dict) else None
        if page_w is None and isinstance(menu_page, dict):
            page_w = menu_page.get("width")
        if page_h is None and isinstance(menu_page, dict):
            page_h = menu_page.get("height")

        page_raw_text = str(docai_page.get("raw_text") or "").strip() if isinstance(docai_page, dict) else ""
        if not page_raw_text and isinstance(menu_page, dict):
            page_raw_text = str(menu_page.get("page_text") or "").strip()
        if not page_raw_text:
            page_raw_text = _page_raw_text_from_page_data(page_no, {"lines": line_entries})
        if page_raw_text:
            page_raw_chunks.append(page_raw_text)

        footer_obj = menu_page.get("footer") if isinstance(menu_page, dict) else None
        if isinstance(footer_obj, dict):
            footer = footer_obj
        else:
            footer = {"has_icons": False, "lines": [], "icons": []}

        out_pages.append(
            {
                "page": page_no,
                "width": page_w,
                "height": page_h,
                "raw_text": page_raw_text,
                "lines": line_entries,
                "icons": icon_entries,
                "footer": footer,
            }
        )

    payload_raw_text = str(raw_text or "").strip()
    if not payload_raw_text:
        payload_raw_text = "\n\n".join([chunk for chunk in page_raw_chunks if chunk]).strip()

    return {
        "input_type": "ocr_document",
        "raw_text": payload_raw_text,
        "pages": out_pages,
    }


def _write_ocr_payload_pages(out_dir: Path, payload: Dict[str, Any]) -> None:
    pages = payload.get("pages", []) if isinstance(payload, dict) else []
    if not isinstance(pages, list) or not pages:
        return
    pages_dir = out_dir / "ocr_payload_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for idx, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        page_no_raw = page.get("page")
        try:
            page_no = int(page_no_raw) if page_no_raw is not None else idx
        except Exception:
            page_no = idx
        page_payload = {
            "input_type": payload.get("input_type") if isinstance(payload, dict) else None,
            "payload_mode": "layout_full",
            "pages": [_prepare_page_for_openai(page, fallback_page_no=page_no)],
        }
        (pages_dir / f"page_{page_no:02d}.json").write_text(
            json.dumps(page_payload, indent=2),
            encoding="utf-8",
        )


def _empty_menu(menu_name: str | None = None) -> Dict[str, Any]:
    return {
        "menu_name": menu_name or None,
        "items": [],
        "other_text": [],
        "footer_text": [],
        "notes": [],
        "page_extras": {},
    }


def _bbox4(value: Any) -> List[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    x0 = _to_float(value[0], 0.0)
    y0 = _to_float(value[1], 0.0)
    x1 = _to_float(value[2], 0.0)
    y1 = _to_float(value[3], 0.0)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _bbox_norm(bbox: List[float] | None, page_w: Any, page_h: Any) -> List[float] | None:
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    w = _to_float(page_w, 0.0)
    h = _to_float(page_h, 0.0)
    if w <= 0.0 or h <= 0.0:
        return None
    x0 = max(0.0, min(1.0, _to_float(bbox[0], 0.0) / w))
    y0 = max(0.0, min(1.0, _to_float(bbox[1], 0.0) / h))
    x1 = max(0.0, min(1.0, _to_float(bbox[2], 0.0) / w))
    y1 = max(0.0, min(1.0, _to_float(bbox[3], 0.0) / h))
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]


def _line_sort_key_for_openai(line: Dict[str, Any]) -> tuple[int, float, float, int]:
    col_raw = line.get("column_index")
    col = _to_int(col_raw, 0) if col_raw is not None else 0
    bbox = _bbox4(line.get("bbox"))
    y = bbox[1] if bbox else float("inf")
    x = bbox[0] if bbox else float("inf")
    line_index = _to_int(line.get("line_index"), 0)
    return (col, y, x, line_index)


def _normalize_icon_label_for_openai(value: Any) -> str:
    label = _normalize_text(value).lower()
    if not label:
        return ""
    return re.sub(r"\s+", "_", label)


def _prepare_icons_for_openai(raw_icons: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_icons, list):
        return out

    for icon in raw_icons:
        entry: Dict[str, Any] = {}
        if isinstance(icon, dict):
            label = _normalize_icon_label_for_openai(
                icon.get("label")
                or icon.get("name")
                or icon.get("icon")
                or icon.get("code")
                or icon.get("type")
            )
            if label:
                entry["label"] = label

            bbox = _bbox4(icon.get("bbox"))
            if bbox is not None:
                entry["bbox"] = bbox

            score = icon.get("score")
            if isinstance(score, (int, float)):
                entry["score"] = round(float(score), 4)

            li_raw = icon.get("line_index")
            if li_raw is not None:
                li = _to_int(li_raw, -1)
                if li >= 0:
                    entry["line_index"] = int(li)
        else:
            label = _normalize_icon_label_for_openai(icon)
            if label:
                entry["label"] = label

        if entry:
            out.append(entry)
    return out


def _compact_icon_for_openai(icon: Dict[str, Any], fallback_line_index: int | None = None) -> Dict[str, Any]:
    if not isinstance(icon, dict):
        return {}
    label = _normalize_icon_label_for_openai(icon.get("label"))
    bbox = _bbox4(icon.get("bbox"))
    li_raw = icon.get("line_index")
    li = _to_int(li_raw, -1) if li_raw is not None else -1
    if li < 0 and fallback_line_index is not None:
        li = _to_int(fallback_line_index, -1)

    out: Dict[str, Any] = {}
    if label:
        out["label"] = label
    if li >= 0:
        out["line_index"] = int(li)
    if bbox is not None:
        out["bbox"] = bbox
    return out


def _icon_identity(icon: Dict[str, Any]) -> str:
    label = _normalize_icon_label_for_openai(icon.get("label"))
    bbox = _bbox4(icon.get("bbox"))
    if bbox is None:
        return f"{label}|"
    return f"{label}|{bbox[0]:.4f}|{bbox[1]:.4f}|{bbox[2]:.4f}|{bbox[3]:.4f}"


def _attach_icon_norm(icon: Dict[str, Any], page_w: Any, page_h: Any) -> Dict[str, Any]:
    out = dict(icon)
    bbox_norm = _bbox_norm(_bbox4(out.get("bbox")), page_w, page_h)
    if bbox_norm is not None:
        out["bbox_norm"] = bbox_norm
    return out


def _line_lookup_for_icons(lines: List[Dict[str, Any]]) -> tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    index_to_line: Dict[int, Dict[str, Any]] = {}
    line_probe: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        li = _to_int(line.get("line_index"), i)
        index_to_line[li] = line
        line_probe.append({"line_index": li, "bbox": line.get("bbox")})
    return index_to_line, line_probe


def _match_icon_to_line_index(
    icon: Dict[str, Any],
    index_to_line: Dict[int, Dict[str, Any]],
    line_probe: List[Dict[str, Any]],
) -> int | None:
    if not isinstance(icon, dict):
        return None
    target_line: int | None = None
    li_raw = icon.get("line_index")
    if li_raw is not None:
        li = _to_int(li_raw, -1)
        if li in index_to_line:
            target_line = li
    if target_line is None:
        match_idx = _line_match_for_icon(_bbox4(icon.get("bbox")), line_probe)
        if match_idx is not None and 0 <= match_idx < len(line_probe):
            target_line = _to_int(line_probe[match_idx].get("line_index"), -1)
    if target_line is None or target_line < 0:
        return None
    return target_line


def _map_page_icons_to_line_index(
    page_icons: List[Dict[str, Any]],
    lines: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    mapped: Dict[int, List[Dict[str, Any]]] = {}
    if not page_icons or not lines:
        return mapped

    index_to_line, line_probe = _line_lookup_for_icons(lines)

    for icon in page_icons:
        if not isinstance(icon, dict):
            continue
        target_line = _match_icon_to_line_index(icon, index_to_line=index_to_line, line_probe=line_probe)
        if target_line is None or target_line < 0:
            continue
        mapped.setdefault(target_line, []).append(dict(icon))
    return mapped


def _prepare_page_for_openai(page: Dict[str, Any], fallback_page_no: int = 1) -> Dict[str, Any]:
    page_no_raw = page.get("page")
    try:
        page_no = int(page_no_raw) if page_no_raw is not None else int(fallback_page_no)
    except Exception:
        page_no = int(fallback_page_no)

    page_width = page.get("width")
    page_height = page.get("height")
    clean_lines: List[Dict[str, Any]] = []
    raw_lines = page.get("lines") if isinstance(page, dict) else None
    if not isinstance(raw_lines, list):
        raw_lines = []

    for idx, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        bbox = _bbox4(line.get("bbox"))
        if bbox is None:
            # Layout Parser often returns no coordinates (documentLayout format).
            # Generate a synthetic sequential bbox so lines are NOT discarded.
            # Use small increments to preserve reading order.
            row_height = 0.02
            y_start = round(idx * row_height, 6)
            y_end = round(y_start + row_height * 0.9, 6)
            bbox = [0.0, y_start, 1.0, y_end]
        line_index_raw = line.get("line_index")
        line_index = _to_int(line_index_raw, idx) if line_index_raw is not None else idx
        entry: Dict[str, Any] = {
            "line_index": int(line_index),
            "text": text,
            "bbox": bbox,
        }
        bbox_norm = _bbox_norm(bbox, page_width, page_height)
        if bbox_norm is not None:
            entry["bbox_norm"] = bbox_norm
        col_raw = line.get("column_index")
        if col_raw is not None:
            entry["column_index"] = int(_to_int(col_raw, 0))
        role = _normalize_text(line.get("layout_role")).lower()
        if role:
            entry["layout_role"] = role
        line_icons = _prepare_icons_for_openai(line.get("icons"))
        if line_icons:
            entry["_line_icons"] = line_icons
        clean_lines.append(entry)

    clean_lines.sort(key=_line_sort_key_for_openai)
    page_icons = _prepare_icons_for_openai(page.get("icons") if isinstance(page, dict) else [])
    page_icons = [_attach_icon_norm(icon, page_width, page_height) for icon in page_icons]
    index_to_line, line_probe = _line_lookup_for_icons(clean_lines)
    for icon in page_icons:
        if not isinstance(icon, dict):
            continue
        target_line = _match_icon_to_line_index(icon, index_to_line=index_to_line, line_probe=line_probe)
        if target_line is None:
            continue
        icon["near_line_index"] = int(target_line)
        if icon.get("line_index") is None:
            icon["line_index"] = int(target_line)
        near_line = index_to_line.get(target_line, {})
        near_text = _normalize_text(near_line.get("text")) if isinstance(near_line, dict) else ""
        if near_text:
            icon["near_text"] = near_text
    line_icon_map = _map_page_icons_to_line_index(page_icons, clean_lines)

    for i, line in enumerate(clean_lines):
        line_idx = _to_int(line.get("line_index"), i)
        line_text = _normalize_text(line.get("text"))
        merged_icons: List[Dict[str, Any]] = []

        for raw_icon in line.pop("_line_icons", []):
            if not isinstance(raw_icon, dict):
                continue
            raw_label = _normalize_icon_label_for_openai(raw_icon.get("label"))
            match: Dict[str, Any] | None = None
            if raw_label:
                for mapped_icon in line_icon_map.get(line_idx, []):
                    if _normalize_icon_label_for_openai(mapped_icon.get("label")) == raw_label:
                        match = mapped_icon
                        break
            prepared_icon = _attach_icon_norm(match if match is not None else raw_icon, page_width, page_height)
            if prepared_icon.get("near_line_index") is None:
                prepared_icon["near_line_index"] = int(line_idx)
            if not _normalize_text(prepared_icon.get("near_text")) and line_text:
                prepared_icon["near_text"] = line_text
            merged_icons.append(prepared_icon)

        for mapped_icon in line_icon_map.get(line_idx, []):
            prepared_icon = _attach_icon_norm(mapped_icon, page_width, page_height)
            if prepared_icon.get("near_line_index") is None:
                prepared_icon["near_line_index"] = int(line_idx)
            if not _normalize_text(prepared_icon.get("near_text")) and line_text:
                prepared_icon["near_text"] = line_text
            merged_icons.append(prepared_icon)

        deduped_icons: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for icon in merged_icons:
            key = _icon_identity(icon)
            if key in seen:
                continue
            seen.add(key)
            deduped_icons.append(icon)
        if deduped_icons:
            line["icons"] = deduped_icons

    page_raw_text = str(page.get("raw_text") or "").strip() if isinstance(page, dict) else ""
    if not page_raw_text:
        page_raw_text = _page_raw_text_from_page_data(page_no, {"lines": clean_lines})
    prepared: Dict[str, Any] = {
        "page": int(page_no),
        "width": page_width,
        "height": page_height,
        "raw_text": page_raw_text,
        "lines": clean_lines,
        "icons": page_icons,
    }
    return prepared


def _prepare_page_raw_text_for_openai(page: Dict[str, Any], fallback_page_no: int = 1) -> Dict[str, Any]:
    page_no_raw = page.get("page")
    try:
        page_no = int(page_no_raw) if page_no_raw is not None else int(fallback_page_no)
    except Exception:
        page_no = int(fallback_page_no)
    page_raw_text = str(page.get("raw_text") or "").strip() if isinstance(page, dict) else ""
    if not page_raw_text:
        page_raw_text = _page_raw_text_from_page_data(page_no, {"lines": page.get("lines") if isinstance(page, dict) else []})
    return {
        "page": int(page_no),
        "raw_text": page_raw_text,
    }


def _prepare_page_line_bbox_for_openai(page: Dict[str, Any], fallback_page_no: int = 1) -> Dict[str, Any]:
    page_no_raw = page.get("page")
    try:
        page_no = int(page_no_raw) if page_no_raw is not None else int(fallback_page_no)
    except Exception:
        page_no = int(fallback_page_no)

    page_width = page.get("width")
    page_height = page.get("height")
    raw_lines = page.get("lines") if isinstance(page, dict) else None
    if not isinstance(raw_lines, list):
        raw_lines = []

    clean_lines: List[Dict[str, Any]] = []
    for idx, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        bbox = _bbox4(line.get("bbox"))
        if bbox is None:
            continue
        line_index_raw = line.get("line_index")
        line_index = _to_int(line_index_raw, idx) if line_index_raw is not None else idx
        entry: Dict[str, Any] = {
            "line_index": int(line_index),
            "text": text,
            "bbox": bbox,
        }
        col_raw = line.get("column_index")
        if col_raw is not None:
            entry["column_index"] = int(_to_int(col_raw, 0))
        line_icons = _prepare_icons_for_openai(line.get("icons"))
        if line_icons:
            compact_line_icons: List[Dict[str, Any]] = []
            seen_line_icons: set[str] = set()
            for icon in line_icons:
                compact_icon = _compact_icon_for_openai(icon, fallback_line_index=int(line_index))
                if not compact_icon:
                    continue
                key = _icon_identity(compact_icon)
                if key in seen_line_icons:
                    continue
                seen_line_icons.add(key)
                compact_line_icons.append(compact_icon)
            if compact_line_icons:
                entry["icons"] = compact_line_icons
        clean_lines.append(entry)

    clean_lines.sort(key=_line_sort_key_for_openai)
    page_icons_raw = _prepare_icons_for_openai(page.get("icons") if isinstance(page, dict) else [])
    index_to_line, line_probe = _line_lookup_for_icons(clean_lines)
    page_icons: List[Dict[str, Any]] = []
    line_icon_map: Dict[int, List[Dict[str, Any]]] = {}
    for icon in page_icons_raw:
        target_line = _match_icon_to_line_index(icon, index_to_line=index_to_line, line_probe=line_probe)
        compact_icon = _compact_icon_for_openai(icon, fallback_line_index=target_line)
        if not compact_icon:
            continue
        page_icons.append(compact_icon)
        li = _to_int(compact_icon.get("line_index"), -1)
        if li >= 0:
            line_icon_map.setdefault(li, []).append(compact_icon)

    if line_icon_map:
        for i, line in enumerate(clean_lines):
            li = _to_int(line.get("line_index"), i)
            combined: List[Dict[str, Any]] = []
            existing_icons = line.get("icons") if isinstance(line.get("icons"), list) else []
            for icon in existing_icons:
                if isinstance(icon, dict):
                    combined.append(icon)
            for icon in line_icon_map.get(li, []):
                if isinstance(icon, dict):
                    combined.append(icon)
            if not combined:
                continue
            deduped: List[Dict[str, Any]] = []
            seen: set[str] = set()
            for icon in combined:
                key = _icon_identity(icon)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(icon)
            if deduped:
                line["icons"] = deduped

    page_raw_text = str(page.get("raw_text") or "").strip() if isinstance(page, dict) else ""
    if not page_raw_text:
        page_raw_text = _page_raw_text_from_page_data(page_no, {"lines": clean_lines})

    out_page = {
        "page": int(page_no),
        "width": page_width,
        "height": page_height,
        "raw_text": page_raw_text,
        "lines": clean_lines,
    }
    if page_icons:
        deduped_page_icons: List[Dict[str, Any]] = []
        seen_page: set[str] = set()
        for icon in page_icons:
            key = _icon_identity(icon)
            if key in seen_page:
                continue
            seen_page.add(key)
            deduped_page_icons.append(icon)
        if deduped_page_icons:
            out_page["icons"] = deduped_page_icons
    return out_page


def _prepare_page_raw_json_for_openai(page: Dict[str, Any], fallback_page_no: int = 1) -> Dict[str, Any]:
    # Keep page payload verbatim so line/icon coordinates are not rewritten before OpenAI.
    prepared = copy.deepcopy(page) if isinstance(page, dict) else {}
    page_no_raw = prepared.get("page")
    try:
        page_no = int(page_no_raw) if page_no_raw is not None else int(fallback_page_no)
    except Exception:
        page_no = int(fallback_page_no)
    prepared["page"] = int(page_no)
    if not _normalize_text(prepared.get("raw_text")):
        lines = prepared.get("lines") if isinstance(prepared.get("lines"), list) else []
        prepared["raw_text"] = _page_raw_text_from_page_data(page_no, {"lines": lines})
    return prepared


def _normalize_openai_input_mode(value: Any) -> str:
    raw = _normalize_text(value).lower()
    if raw in {"raw_page_json", "raw_page", "verbatim", "page_json", "page_raw_json"}:
        return "raw_page_json"
    if raw in {"line_bbox_compact", "line_bbox", "line_box", "bbox_lines", "compact_layout", "layout_compact", "lines_min", "line_info"}:
        return "line_bbox_compact"
    if raw in {"layout", "lines", "coords", "coordinates", "full"}:
        return "layout"
    if raw in {"raw", "raw_text", "text", "plain_text", "raw_text_only"}:
        return "raw_text_only"
    return "raw_page_json"


def _openai_input_mode() -> str:
    return _normalize_openai_input_mode(os.getenv("OPENAI_INPUT_MODE", "raw_page_json"))


def _prepare_payload_for_openai(payload: Dict[str, Any], mode: str | None = None) -> Dict[str, Any]:
    openai_input_mode = _normalize_openai_input_mode(mode if mode is not None else _openai_input_mode())
    pages = payload.get("pages", []) if isinstance(payload, dict) else []
    out_pages: List[Dict[str, Any]] = []
    if isinstance(pages, list):
        for idx, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue
            if openai_input_mode == "raw_page_json":
                out_pages.append(_prepare_page_raw_json_for_openai(page, fallback_page_no=idx))
            elif openai_input_mode == "line_bbox_compact":
                out_pages.append(_prepare_page_line_bbox_for_openai(page, fallback_page_no=idx))
            elif openai_input_mode == "layout":
                out_pages.append(_prepare_page_for_openai(page, fallback_page_no=idx))
            else:
                out_pages.append(_prepare_page_raw_text_for_openai(page, fallback_page_no=idx))
    return {
        "input_type": payload.get("input_type") if isinstance(payload, dict) else None,
        "openai_input_mode": openai_input_mode,
        "pages": out_pages,
    }


def _build_openai_pass1_prompts(payload: Dict[str, Any]) -> tuple[str, str, str]:
    system_text = "You are a menu OCR layout grouper. Return valid JSON only."
    user_instruction = (
        "This is OCR lines with bounding boxes for one menu page, already sorted in reading order. "
        "Group lines into dish_blocks using column_index, y-order, and bbox proximity. "
        "Each dish_block must represent exactly one dish title with its related description/price lines. "
        "Never merge two different dish title lines into one block. "
        "If two candidate title lines appear in sequence, split into separate blocks even if close vertically. "
        "Prefer over-inclusion to avoid missed dishes: if uncertain, keep a candidate title as its own block instead of dropping it. "
        "Keep line_indices in ascending reading order. "
        "Output JSON keys exactly: menu_name, page, dish_blocks, other_text_line_indices, footer_line_indices, notes. "
        "dish_blocks must be a list of objects with: block_id, column_index, bbox, line_indices."
    )
    user_text = f"{user_instruction}\n\n{json.dumps(payload, ensure_ascii=False)}"
    return system_text, user_instruction, user_text


def _build_openai_pass2_prompts(payload: Dict[str, Any], pass1_result: Dict[str, Any]) -> tuple[str, str, str]:
    system_text = (
        "You are a senior menu reconstruction engine. "
        "Understand page context first, then return valid JSON only."
    )
    user_instruction = (
        "This is one page of OCR payload JSON with coordinates. "
        "Treat the given page JSON as authoritative raw OCR evidence and parse it as-is. "
        "Do not pre-filter, remove, or rewrite any OCR lines before reasoning. "
        "Use the full ocr_page payload exactly as provided, including icons and bbox coordinates. "
        "Do not assume missing lines, and do not fabricate coordinates, dish names, prices, calories, or allergens. "
        "First classify page intent: menu_items_page, intro_or_story_page, legend_page, or footer_only_page. "
        "If the page is not a real menu_items_page, return items=[] and move text to other_text/footer_text/page_extras. "
        "Build context before extraction: detect columns, section headers, dish blocks, then prices/descriptions/allergens. "
        "Create an internal line table from all OCR lines using line_index, text, bbox, column_index, and icons. "
        "Extract menu items strictly column-by-column using coordinates: column_index (left-to-right), then y (top-to-bottom). "
        "Inside each column, read from top to bottom and preserve that order when assigning dish context. "
        "Determine dish_type from section/category headers in the same column and assign that dish_type to each dish item. "
        "Hierarchy rule: on menu pages, a section/main heading is typically above its child items; assign following lower lines in that column to that heading until the next peer heading or section boundary. "
        "If a heading changes inside a column, switch dish_type from that point onward in that column. "
        "Do not carry dish_type across unrelated columns unless the heading explicitly spans columns. "
        "Use line_index and bbox only internally for spatial reasoning; do not expose them in output. "
        "Price columns may appear in a narrow adjacent strip; match prices using nearest vertical alignment even when price text sits in an adjacent nearby column. "
        "A valid dish should look like a menu entry (dish title with price and/or clear dish context), not random OCR fragments. "
        "Reject gibberish/noise lines and decorative text as items. "
        "Do not treat isolated symbols, short garbage tokens, legend rows, legal disclaimers, or page branding as dish names. "
        "Do not emit section-combo labels (for example boats, platters, category-only banners) as normal dish items unless they clearly represent a sellable menu item with its own direct description/price context. "
        "Use line.icons and page icons as primary allergen and dietary evidence for that line/item. "
        "If a marker is unclear, keep it in item.extras.unparsed_marks instead of guessing. "
        "Never infer allergens that are not present in line text/icons. "
        "Do not convert uncertain dietary markers into booleans; keep OCR codes in veg/non_veg fields (v, vg, s, gf, sp, etc.) when present. "
        "For prices: first check whether the dish title/description line itself contains a plausible price token; if yes, use that exact local price. "
        "If no inline price exists, then attach standalone numeric/price lines to the nearest valid dish in the same column and local y-range. "
        "Monetary-token rule: if a numeric token appears in expected price position for an item row (same line or nearest aligned price-strip line) and matches money-like formats (for example 9.99, .99, 12, 1,299, $12, INR 450), treat it as a price candidate even without a currency symbol. "
        "US-style dollar prices may appear with or without '$' due to OCR (for example 9, 12, 14.99, $14.99); keep them as item prices when they are the nearest local item-level numeric candidate and no stronger non-price cue is present. "
        "Normalize leading-dot prices to zero-leading format (.99 -> 0.99) and strip currency symbols while preserving numeric value. "
        "Do not treat as price: calorie/nutrition numbers (kcal, cal), quantities/weights/volumes (g, kg, ml, l, oz), piece/serving counts, percentages, dates/times, phone-like numbers, subtract/add instructions, or pure nutrition ranges without monetary context. "
        "When both an immediately-above and immediately-below standalone price exist around a dish line, prefer the above price first; below price may belong to the next dish. "
        "Only use an adjacent-column price when that adjacent line is a numeric-only price strip line; never borrow prices from adjacent columns that contain dish names/descriptions. "
        "Do not assign a price from another section block; section headers create hard boundaries for price propagation. "
        "If both same-column and adjacent-column candidates exist, prefer the same-column candidate. "
        "Shared price rule: if a price appears above a sequence of dishes in the same column, apply that price to all following sibling dishes until the next explicit price appears in that same column. "
        "If one visible price clearly applies to multiple sibling variants in the same group, copy that same explicit price to each variant. "
        "Dual-price rule: if one item row clearly shows two prices for the same item (for example glass/bottle or small/large), keep both prices for that single item and do not collapse to one. "
        "If labels are visible (for example glass and bottle), preserve them in item.extras.price_labels; otherwise keep two prices in reading order as a combined price string (for example 950/4200). "
        "Apply this dual-price rule consistently across all menu sections/categories whenever a single item row contains two explicit prices. "
        "Numbers that are serving counts (for example 4 pc, 6 skewers, 4 or 8 pieces) are not prices unless explicit currency cues exist. "
        "Do not assign prices from far-away sections; prefer nearest local candidates that match column and y-proximity. "
        "If a candidate price is an outlier for the local section and is not present in the item's own OCR lines, reject it and choose the next best local candidate. "
        "For heading-plus-variants layouts, if one group-level price applies to multiple variants below, propagate within that group only and stop at the next heading/section change. "
        "If a local sub-heading includes one explicit add-on/group price for its child list, propagate that price only to immediate child items in that subsection until the next heading boundary. "
        "Do not drop plausible dish titles just because their price is uncertain; keep the item and infer price only from allowed local rules. "
        "Do not omit any dish title candidate from detected dish blocks. If a dish appears as both a short title line and a longer descriptive line, merge them into one final item instead of dropping either mention. "
        "Coverage rule: do not silently drop a local subsection of dish-like child lines under a valid heading; extract child items unless they are clearly footer/legend/legal content. "
        "Do not place dish-like lines in other_text: if a line looks like a sellable item or variant (for example has price/kcal, food noun phrase, add-on/substitute/side wording, or appears in a dish block), keep it in items. "
        "If OCR merges a heading and multiple child dishes into one long line, split into separate child items instead of pushing the merged line to other_text. "
        "If the same dish name appears multiple times due to OCR overlap, keep the occurrence with strongest local description/price evidence and drop duplicates. "
        "Do not emit legend/allergen key rows and legal disclaimers as dish items; place them in footer_text or page_extras. "
        "Do not emit banner/headline text as dish items unless paired with dish-level description/price evidence. "
        "other_text is only for non-menu content such as branding/taglines/decorative text; it must not contain dish names, variant names, or merged dish rows. "
        "Keep OCR text mostly verbatim; only do minimal cleanup for obvious OCR noise. "
        "Before final output, run a consistency check per item: verify each item has supporting OCR line evidence in the same section/column context and that assigned price obeys the local price hierarchy rules above. "
        "Use top-level keys: menu_name, items, other_text, footer_text, notes, page_extras. "
        "Each item must include: name, description, price, kcal, allergens, veg, non_veg, page, dish_type, extras. "
        "Return results in a neat menu style: clear dish names, correct section dish_type labels, and no duplicate items. "
        "Do not return bbox or source_lines in the final output."
    )
    merged_input: Dict[str, Any] = {"ocr_page": payload}
    if isinstance(pass1_result, dict):
        compact_blocks: Dict[str, Any] = {}
        dish_blocks = pass1_result.get("dish_blocks")
        if isinstance(dish_blocks, list) and dish_blocks:
            compact_blocks["dish_blocks"] = dish_blocks
        other_idx = pass1_result.get("other_text_line_indices")
        if isinstance(other_idx, list) and other_idx:
            compact_blocks["other_text_line_indices"] = other_idx
        footer_idx = pass1_result.get("footer_line_indices")
        if isinstance(footer_idx, list) and footer_idx:
            compact_blocks["footer_line_indices"] = footer_idx
        if compact_blocks:
            merged_input["dish_blocking"] = compact_blocks
    user_text = f"{user_instruction}\n\n{json.dumps(merged_input, ensure_ascii=False)}"
    return system_text, user_instruction, user_text


def _build_openai_raw_text_prompts(payload: Dict[str, Any]) -> tuple[str, str, str]:
    system_text = (
        "You are a senior menu reconstruction engine. "
        "Understand context first, then return valid JSON only."
    )
    user_instruction = (
        "This is plain raw OCR text for one menu page. "
        "First understand the full page context and structure before extracting items. "
        "Classify the page as menu_items_page, intro_or_story_page, legend_page, or footer_only_page. "
        "If not a menu_items_page, return items=[] and place content in other_text/footer_text/page_extras. "
        "Parse the menu column-by-column and keep dish groups organized by dish_type/section headings. "
        "Within each column, process strictly top-to-bottom to preserve heading-to-dish relationships. "
        "Assign dish_type to each dish item from the nearest relevant heading context. "
        "Hierarchy rule: a section/main heading generally appears above its child dishes; assign lower lines to that heading until the next peer heading or section boundary. "
        "Handle layouts where prices are in a nearby narrow side column by matching dish rows to the closest vertical price row. "
        "Identify heading/group lines, price lines, and sub-dish lines, then build final items. "
        "Use only this raw text to reconstruct the menu in a clean structured form. "
        "Only output real dish entries as items; reject gibberish/noise/decorative content. "
        "Sometimes one printed price belongs to multiple sub-dishes under one dish type/heading; in that case copy that same explicit price to each related sub-dish. "
        "For this task, every real dish should end up with a price; avoid null price when a nearby explicit group price exists. "
        "If one menu row has two prices for one item (for example glass/bottle), keep both prices for that item; do not drop one. "
        "Apply this two-price rule consistently across all menu groups/categories, not just one section. "
        "If a numeric token appears in expected price position for an item row and matches money-like formats (for example 9.99, .99, 12, 1,299), treat it as a price candidate even without currency symbols. "
        "US-style dollar prices may appear with or without '$' due to OCR (for example 9, 12, 14.99, $14.99); keep them as item prices when they are the nearest local item-level numeric candidate and no stronger non-price cue is present. "
        "Normalize leading-dot prices to zero-leading format (.99 -> 0.99). "
        "Do not treat as price: kcal/calorie values, grams/ml/oz, serving/piece counts, percentages, dates/times, phone-like numbers, subtract/add instructions, or pure nutrition ranges without money context. "
        "If a local subsection heading includes one explicit add-on/group price, apply it only to immediate child items in that subsection until the next heading. "
        "Coverage rule: do not drop dish-like child lines under valid local headings; keep them as items unless they are clearly footer/legend/legal text. "
        "Do not place dish-like rows in other_text: lines with dish names, variants, add-ons/substitutes/sides, kcal, or price-like tokens must be represented in items. "
        "If one long OCR line contains heading + multiple child dishes, split and output the child dishes as items instead of copying the merged line to other_text. "
        "Do not invent dish names, descriptions, prices, calories, allergens, or dietary markers. "
        "Keep dietary markers exactly as written (for example: v, vg, s, gf, sp). "
        "If one price line appears to apply to several nearby items/variants, apply it carefully based on local context. "
        "Shared price rule: when a price line appears above multiple dishes in the same column, apply that same price to dishes below it until the next price line in that column. "
        "Put only true non-item fragments in other_text/footer_text; do not move real dish lines there. "
        "Output keys: menu_name, items, other_text, footer_text, notes, page_extras. "
        "Each item must include: name, description, price, kcal, allergens, veg, non_veg, page, dish_type, extras. "
        "Return results like a clean menu: section-aware, column-aware, and without random OCR junk as items. "
        "Do not return bbox or source_lines in the final output."
    )
    user_text = f"{user_instruction}\n\n{json.dumps(payload, ensure_ascii=False)}"
    return system_text, user_instruction, user_text


def _extract_openai_output_text(response: Any) -> str:
    raw_text = str(getattr(response, "output_text", "") or "").strip()
    if raw_text:
        return raw_text
    parts: List[str] = []
    output_items = getattr(response, "output", None)
    if isinstance(output_items, list):
        for obj in output_items:
            if getattr(obj, "type", None) != "message":
                continue
            content_items = getattr(obj, "content", None)
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if getattr(content, "type", None) != "output_text":
                    continue
                txt = str(getattr(content, "text", "") or "").strip()
                if txt:
                    parts.append(txt)
    return "\n".join(parts).strip()


def _combine_usage(model: str, usage_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged = _empty_usage(model)
    merged["input_tokens"] = 0
    merged["output_tokens"] = 0
    merged["total_tokens"] = 0
    est_cost = 0.0
    has_cost = True
    for entry in usage_entries:
        if not isinstance(entry, dict):
            continue
        merged["input_tokens"] += int(_to_int(entry.get("input_tokens"), 0))
        merged["output_tokens"] += int(_to_int(entry.get("output_tokens"), 0))
        merged["total_tokens"] += int(_to_int(entry.get("total_tokens"), 0))
        part_cost = entry.get("estimated_cost_usd")
        if part_cost is None:
            has_cost = False
        else:
            est_cost += _to_float(part_cost, 0.0)
    if merged["total_tokens"] <= 0:
        merged["total_tokens"] = int(merged["input_tokens"]) + int(merged["output_tokens"])
    merged["estimated_cost_usd"] = round(est_cost, 6) if has_cost else None
    return merged


def _normalize_text_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for entry in value:
        raw = entry
        if isinstance(entry, dict):
            if "text" in entry:
                raw = entry.get("text")
            elif "line_text" in entry:
                raw = entry.get("line_text")
        txt = _normalize_text(raw)
        if txt:
            out.append(txt)
    return out


_ALLOWED_VEG_CODES = {
    "v",
    "vg",
    "veg",
    "vegan",
    "gf",
    "sp",
    "df",
    "nf",
    "sf",
    "jain",
    "eggless",
}

_ALLOWED_NONVEG_CODES = {
    "s",
    "nv",
    "non-veg",
    "non_veg",
    "non veg",
}


def _normalize_veg_flag(value: Any) -> str | None:
    if isinstance(value, bool):
        return "v" if value else None
    val = _normalize_text(value).lower()
    if not val:
        return None
    if val in {"false", "0", "no", "n", "none", "null"}:
        return None
    if val in {"true", "1", "yes", "y"}:
        return "v"
    if val in {"veg", "vegetarian", "v"}:
        return "v"
    if val in {"vegan", "vg"}:
        return "vg"
    if val in _ALLOWED_VEG_CODES:
        return val
    return None


def _normalize_nonveg_flag(value: Any) -> str | None:
    if isinstance(value, bool):
        return "non-veg" if value else None
    val = _normalize_text(value).lower()
    if not val:
        return None
    if val in {"false", "0", "no", "n", "none", "null"}:
        return None
    if val in {"true", "1", "yes", "y"}:
        return "non-veg"
    if val in {"s", "seafood", "fish"}:
        return "s"
    if val in {"non_veg", "non-veg", "non veg", "nv", "meat", "chicken", "egg"}:
        return "non-veg"
    if val in _ALLOWED_NONVEG_CODES:
        return val
    return None


def _normalize_allergens(value: Any) -> List[str]:
    if isinstance(value, list):
        out: List[str] = []
        for a in value:
            txt = _normalize_text(a).lower()
            if txt and txt not in out:
                out.append(txt)
        return out
    txt = _normalize_text(value).lower()
    if not txt:
        return []
    split = re.split(r"[,;/|]+", txt)
    out = []
    for s in split:
        t = _normalize_text(s).lower()
        if t and t not in out:
            out.append(t)
    return out


def _normalize_price(value: Any) -> str | None:
    txt = _normalize_text(value)
    if not txt:
        return None
    money_cue_re = r"(?i)(?:[$â‚¬Â£Â¥â‚¹]|\b(?:rs\.?|inr|usd|aud|cad|eur|gbp|aed|sar|qar|dollar|dollars)\b)"

    def _has_money_cue(s: str) -> bool:
        return bool(re.search(money_cue_re, _normalize_text(s), flags=re.IGNORECASE))

    def _has_decimal_token(s: str) -> bool:
        return bool(re.search(r"\d{1,6}[.,]\d{1,2}\b", _normalize_text(s)))
    # Remove common serving/count notations so they do not get mistaken as prices.
    txt_wo_serving = re.sub(
        r"\(\s*\d{1,3}\s*(?:/\s*\d{1,3}\s*)?(?:pc|pcs|piece|pieces|skewer|skewers|stick|sticks|roll|rolls|ml|cl|l|ltr|litre|liter|g|gm|kg|oz)\s*\)",
        "",
        txt,
        flags=re.IGNORECASE,
    )
    txt_wo_serving = re.sub(
        r"\b\d{1,3}\s*(?:pc|pcs|piece|pieces|skewer|skewers|stick|sticks|roll|rolls|ml|cl|l|ltr|litre|liter|g|gm|kg|oz)\b",
        "",
        txt_wo_serving,
        flags=re.IGNORECASE,
    )
    txt_wo_serving = _normalize_text(txt_wo_serving) or txt

    # Comma-separated price lists usually represent nearby alternative prices merged by OCR
    # (e.g., "350, 380"). In this case, keep the first value as the primary item price.
    if re.fullmatch(
        r"\s*\d{2,6}(?:[.,]\d{1,2})?\s*(?:,\s+\d{2,6}(?:[.,]\d{1,2})?\s*){1,3}",
        txt_wo_serving,
    ):
        first = re.search(r"\d{2,6}(?:[.,]\d{1,2})?", txt_wo_serving)
        if first:
            return first.group(0).replace(",", ".")

    multi_chain = re.search(
        r"(?<!\d)(\d{1,6}(?:[.,]\d{1,2})?(?:\s*/\s*\d{1,6}(?:[.,]\d{1,2})?)+)(?!\d)",
        txt_wo_serving,
    )
    if multi_chain:
        parts = [p.replace(",", ".") for p in re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", multi_chain.group(1))]
        if len(parts) >= 2:
            return "/".join(parts)

    labelled_chunks = []
    for label, num in re.findall(
        r"(?i)\b(veg|vegetarian|non[-\s]?veg|chicken|prawn|prawns|fish|seafood)\b[^0-9]{0,10}(\d{1,6}(?:[.,]\d{1,2})?)",
        txt_wo_serving,
    ):
        labelled_chunks.append((label, num.replace(",", ".")))
    if labelled_chunks:
        parts = []
        for label, num in labelled_chunks:
            label_norm = label.lower()
            if label_norm in {"veg", "vegetarian"}:
                label_out = "Veg"
            elif label_norm in {"nonveg", "non-veg", "non veg"}:
                label_out = "Non-Veg"
            elif label_norm in {"prawn", "prawns"}:
                label_out = "Prawns"
            elif label_norm == "chicken":
                label_out = "Chicken"
            elif label_norm in {"fish", "seafood"}:
                label_out = "Fish"
            else:
                label_out = label.title()
            parts.append(f"{label_out} {num}")
        if parts:
            return " / ".join(parts)

    num_matches = list(re.finditer(r"\d{1,6}(?:[.,]\d{1,2})?", txt_wo_serving))
    if not num_matches:
        return None
    numeric_vals = [_to_float(m.group(0).replace(",", "."), 0.0) for m in num_matches]
    has_large_token = any(v >= 40.0 for v in numeric_vals)

    filtered: List[tuple[int, str]] = []
    unit_re = r"(pc|pcs|piece|pieces|skewer|skewers|stick|sticks|roll|rolls|ml|cl|l|ltr|litre|liter|g|gm|kg|oz)\b"
    for m, val in zip(num_matches, numeric_vals):
        start, end = m.span()
        token = m.group(0)
        left = txt_wo_serving[max(0, start - 14) : start].lower()
        right = txt_wo_serving[end : min(len(txt_wo_serving), end + 20)].lower()
        if re.match(rf"\s*{unit_re}", right):
            continue
        if re.search(r"\(\s*$", left) and re.match(rf"\s*\)?\s*{unit_re}", right):
            continue
        if has_large_token and val <= 30.0:
            continue
        filtered.append((start, token.replace(",", ".")))

    if not filtered:
        return None
    if not has_large_token and len(filtered) > 1:
        # Multiple low numbers without price cues usually represent counts (e.g., 4 or 8 pieces).
        return None
    if len(filtered) == 1 and not has_large_token:
        sole_val = _to_float(filtered[0][1], 0.0)
        if sole_val < 20.0 and not _has_money_cue(txt_wo_serving):
            # Keep low prices when they are explicitly money-like (e.g. 5.99)
            # or when the value itself is a standalone numeric price token.
            if _has_decimal_token(filtered[0][1]):
                pass
            elif re.fullmatch(r"\d{1,3}", txt_wo_serving):
                if sole_val < 3.0:
                    return None
            else:
                return None
    # Prefer the right-most plausible numeric token.
    filtered.sort(key=lambda x: x[0])
    return filtered[-1][1]


def _normalize_food_spelling(text: str) -> str:
    out = str(text or "")
    if not _env_flag("OPENAI_NORMALIZE_FOOD_SPELLING", default=False):
        return out

    def _replace_chilli(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.isupper():
            return "CHILI"
        if token[:1].isupper():
            return "Chili"
        return "chili"

    out = re.sub(r"\bchilli\b", _replace_chilli, out, flags=re.IGNORECASE)
    return out


def _extract_dietary_marker(name: str) -> str | None:
    m = re.search(r"\(\s*([A-Za-z]{1,5})\s*\)\s*$", str(name or ""))
    if not m:
        return None
    return m.group(1).upper()


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _repair_clipped_name_from_description(name: str, description: str | None) -> str:
    nm = str(name or "")
    desc = str(description or "")
    if not nm or not desc:
        return nm
    parts = nm.split()
    if len(parts) < 2:
        return nm

    desc_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", desc)]
    if not desc_words:
        return nm
    first_pos: Dict[str, int] = {}
    for i, w in enumerate(desc_words):
        if w not in first_pos:
            first_pos[w] = i

    changed = False
    for idx, tok in enumerate(parts[:-1]):
        tok_alpha = re.sub(r"[^A-Za-z]", "", tok)
        if not tok_alpha or len(tok_alpha) > 2:
            continue
        next_alpha = re.sub(r"[^A-Za-z]", "", parts[idx + 1]).lower()
        if not next_alpha:
            continue
        candidates = [w for w in desc_words if w.startswith(tok_alpha.lower())]
        if not candidates:
            continue
        # Prefer expansions that are less redundant with the next token.
        uniq_candidates = []
        seen = set()
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            uniq_candidates.append(c)
        candidates = sorted(
            uniq_candidates,
            key=lambda w: (_common_prefix_len(w, next_alpha), first_pos.get(w, 10_000), len(w)),
        )
        chosen = candidates[0] if candidates else ""
        if len(chosen) < 4:
            continue
        replacement = chosen.upper() if tok_alpha.isupper() else chosen.capitalize()
        parts[idx] = re.sub(r"[A-Za-z]+", replacement, tok, count=1)
        changed = True
    if not changed:
        return nm
    return " ".join(parts)


def _normalize_source_lines(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    out: List[int] = []
    for v in value:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(int(v))
            continue
        m = re.search(r"\d+", str(v or ""))
        if m:
            out.append(int(m.group(0)))
    return out


def _normalize_menu_item(item: Dict[str, Any], fallback_page: int | None = None) -> Dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    name = _normalize_text(item.get("name"))
    if not name:
        return None
    description = _normalize_text(item.get("description")) or None
    name = _repair_clipped_name_from_description(name, description)
    name = _normalize_food_spelling(name)
    if description:
        description = _normalize_food_spelling(description)

    veg_flag = _normalize_veg_flag(item.get("veg"))
    nonveg_flag = _normalize_nonveg_flag(item.get("non_veg"))
    marker = _extract_dietary_marker(name)
    if marker in {"V", "VEG", "VEGETARIAN"} and veg_flag is None:
        veg_flag = "v"
    if marker in {"VG", "VEGAN"} and veg_flag is None:
        veg_flag = "vg"
    if marker in {"GF", "SP", "DF", "NF", "SF", "JAIN", "EGGLESS"} and veg_flag is None:
        veg_flag = marker.lower()
    if marker in {"S"} and nonveg_flag is None:
        nonveg_flag = "s"
    if marker in {"NV", "NONVEG", "NON_VEG"} and nonveg_flag is None:
        nonveg_flag = "non-veg"

    out: Dict[str, Any] = {
        "name": name,
        "description": description,
        "price": _normalize_price(item.get("price")),
        "kcal": item.get("kcal"),
        "allergens": _normalize_allergens(item.get("allergens")),
        "veg": veg_flag,
        "non_veg": nonveg_flag,
        "dish_type": _normalize_text(item.get("dish_type")) or None,
    }
    page_no = item.get("page")
    if page_no is None:
        page_no = fallback_page
    out["page"] = _to_int(page_no, 1) if page_no is not None else 1
    bbox = _bbox4(item.get("bbox"))
    source_lines = _normalize_source_lines(item.get("source_lines"))
    if bbox is not None:
        out["_bbox"] = bbox
    if source_lines:
        out["_source_lines"] = source_lines
    extras = item.get("extras")
    if isinstance(extras, dict):
        out["extras"] = extras
    return out


def _normalize_openai_menu(parsed: Dict[str, Any], fallback_page: int | None = None) -> Dict[str, Any]:
    menu_name = _normalize_text(parsed.get("menu_name")) or None
    out: Dict[str, Any] = {
        "menu_name": menu_name,
        "items": [],
        "other_text": _normalize_text_list(parsed.get("other_text")),
        "footer_text": _normalize_text_list(parsed.get("footer_text")),
        "notes": _normalize_text_list(parsed.get("notes")),
        "page_extras": parsed.get("page_extras") if isinstance(parsed.get("page_extras"), dict) else {},
    }
    def _dedupe_norm(value: Any) -> str:
        s = _normalize_text(value).lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    items = parsed.get("items")
    seen_item_keys: set[tuple[str, str, str, int, str]] = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            norm_item = _normalize_menu_item(item, fallback_page=fallback_page)
            if norm_item is None:
                continue
            item_key = (
                _dedupe_norm(norm_item.get("name")),
                _dedupe_norm(norm_item.get("description")),
                _normalize_text(norm_item.get("price")),
                _to_int(norm_item.get("page"), 1),
                _dedupe_norm(norm_item.get("dish_type")),
            )
            if item_key in seen_item_keys:
                continue
            seen_item_keys.add(item_key)
            out["items"].append(norm_item)
    return out


def _augment_menu_from_pass1(menu: Dict[str, Any], payload: Dict[str, Any], pass1_result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(menu, dict):
        return menu
    pages = payload.get("pages") if isinstance(payload, dict) else None
    page0 = pages[0] if isinstance(pages, list) and pages and isinstance(pages[0], dict) else None
    lines = page0.get("lines") if isinstance(page0, dict) else None
    if not isinstance(lines, list):
        return menu

    line_map: Dict[int, str] = {}
    line_detail_map: Dict[int, Dict[str, Any]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        idx = _to_int(line.get("line_index"), -1)
        txt = _normalize_text(line.get("text"))
        bbox = _bbox4(line.get("bbox"))
        col = _to_int(line.get("column_index"), 0)
        if idx >= 0 and txt:
            line_map[idx] = txt
            line_detail_map[idx] = {"text": txt, "bbox": bbox, "column_index": col}

    def _texts_from_indices(indices: Any) -> List[str]:
        out: List[str] = []
        if not isinstance(indices, list):
            return out
        for i in indices:
            idx = _to_int(i, -1)
            if idx < 0:
                continue
            txt = line_map.get(idx)
            if txt and txt not in out:
                out.append(txt)
        return out

    if not menu.get("footer_text"):
        footer_text = _texts_from_indices(pass1_result.get("footer_line_indices"))
        if footer_text:
            menu["footer_text"] = footer_text

    if not menu.get("other_text"):
        other_text = _texts_from_indices(pass1_result.get("other_text_line_indices"))
        if other_text:
            menu["other_text"] = other_text

    def _marker_code(text: str) -> str | None:
        m = re.match(r"^\(\s*([A-Za-z]{1,5})\s*\)$", str(text or "").strip())
        if not m:
            return None
        code = m.group(1).upper()
        if code in {"V", "VG", "S"}:
            return code
        return None

    marker_lines: List[Dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        code = _marker_code(str(line.get("text") or ""))
        bbox = _bbox4(line.get("bbox"))
        if not code or bbox is None:
            continue
        marker_lines.append({"code": code, "bbox": bbox})

    def _item_source_lines(item: Dict[str, Any]) -> List[int]:
        src = _normalize_source_lines(item.get("_source_lines"))
        if src:
            return src
        return _normalize_source_lines(item.get("source_lines"))

    def _item_bbox(item: Dict[str, Any]) -> List[float] | None:
        bb = _bbox4(item.get("_bbox"))
        if bb is not None:
            return bb
        return _bbox4(item.get("bbox"))

    def _set_item_bbox(item: Dict[str, Any], bb: List[float] | None) -> None:
        if bb is None:
            return
        item["_bbox"] = bb

    def _heading_to_dish_type(name: str) -> str | None:
        nm = _normalize_text(name)
        if not nm:
            return None
        if len(nm) > 42:
            return None
        if len(nm.split()) > 5:
            return None
        if any(ch.isdigit() for ch in nm):
            return None
        if "," in nm or "+" in nm:
            return None
        return nm.lower()

    def _norm_match_text(text: Any) -> str:
        s = _normalize_text(text).lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    line_match_index: Dict[int, str] = {
        li: _norm_match_text(det.get("text"))
        for li, det in line_detail_map.items()
        if isinstance(det, dict) and _norm_match_text(det.get("text"))
    }
    line_word_index: Dict[int, set[str]] = {li: set(txt.split()) for li, txt in line_match_index.items() if txt}
    column_alpha_y_centers: Dict[int, List[float]] = {}
    column_x_centers: Dict[int, float] = {}
    _column_x_acc: Dict[int, List[float]] = {}
    for li, det in line_detail_map.items():
        bb = _bbox4(det.get("bbox"))
        col = _to_int(det.get("column_index"), 0)
        txt = _normalize_text(det.get("text"))
        if bb is None:
            continue
        cx = (bb[0] + bb[2]) / 2.0
        _column_x_acc.setdefault(col, []).append(cx)
        if txt and re.search(r"[A-Za-z]", txt):
            cy = (bb[1] + bb[3]) / 2.0
            column_alpha_y_centers.setdefault(col, []).append(cy)
    for c, xs in _column_x_acc.items():
        if xs:
            column_x_centers[c] = sum(xs) / float(len(xs))

    def _infer_item_anchor_from_name(item: Dict[str, Any]) -> tuple[List[int], List[float] | None, float]:
        raw_name = _normalize_text(item.get("name"))
        if " - " in raw_name:
            raw_name = raw_name.split(" - ")[-1].strip()
        if "|" in raw_name:
            raw_name = raw_name.split("|")[-1].strip()
        name_norm = _norm_match_text(raw_name)
        if not name_norm:
            return [], None, 0.0
        best_idx = -1
        best_score = 0.0
        name_tokens = [t for t in name_norm.split() if len(t) >= 3]
        # Prefer lines that contain distinctive item tokens when available.
        token_line_counts: Dict[str, int] = {}
        for tok in name_tokens:
            token_line_counts[tok] = sum(1 for words in line_word_index.values() if tok in words)
        distinctive_tokens = [tok for tok, cnt in token_line_counts.items() if cnt > 0 and cnt <= 2 and len(tok) >= 4]
        for li, line_norm in line_match_index.items():
            if not line_norm:
                continue
            words = line_word_index.get(li) or set()
            if distinctive_tokens and not any(tok in words for tok in distinctive_tokens):
                continue
            overlap = 0
            if name_tokens:
                overlap = sum(1 for tok in name_tokens if tok in words)
            if len(name_tokens) >= 2 and overlap == 0 and name_norm not in line_norm:
                continue
            if len(name_tokens) >= 3 and overlap < 2 and name_norm not in line_norm:
                continue
            ratio = SequenceMatcher(None, name_norm, line_norm).ratio()
            phrase_bonus = 0.35 if name_norm and name_norm in line_norm else 0.0
            short_exact_penalty = 0.08 if line_norm == name_norm and len(line_norm.split()) <= 3 else 0.0
            score = ratio + (0.12 * overlap) + phrase_bonus - short_exact_penalty
            if score > best_score or (abs(score - best_score) <= 1e-6 and (best_idx < 0 or li < best_idx)):
                best_score = score
                best_idx = li

        # If the best match is a short bare title line, prefer a longer continuation line
        # that starts with the same title, as it usually carries the true row context.
        if best_idx >= 0:
            best_line_norm = line_match_index.get(best_idx) or ""
            if best_line_norm == name_norm and len(best_line_norm.split()) <= 6:
                alt_idx = -1
                alt_score = 0.0
                for li, line_norm in line_match_index.items():
                    if li == best_idx or not line_norm:
                        continue
                    if not line_norm.startswith(name_norm + " "):
                        continue
                    if len(line_norm.split()) < (len(name_norm.split()) + 2):
                        continue
                    words = line_word_index.get(li) or set()
                    overlap = sum(1 for tok in name_tokens if tok in words) if name_tokens else 0
                    ratio = SequenceMatcher(None, name_norm, line_norm).ratio()
                    score = ratio + (0.12 * overlap) + 0.2
                    if score > alt_score or (abs(score - alt_score) <= 1e-6 and (alt_idx < 0 or li < alt_idx)):
                        alt_score = score
                        alt_idx = li
                if alt_idx >= 0 and alt_score >= max(0.55, best_score - 0.45):
                    best_idx = alt_idx
                    best_score = alt_score

        if best_idx < 0 or best_score < 0.45:
            return [], None, 0.0
        det = line_detail_map.get(best_idx) or {}
        bb = _bbox4(det.get("bbox"))
        if bb is not None:
            return [best_idx], bb, best_score
        return [best_idx], None, best_score

    items = menu.get("items")
    if isinstance(items, list) and marker_lines:
        for item in items:
            if not isinstance(item, dict):
                continue
            if _normalize_text(item.get("veg")) or _normalize_text(item.get("non_veg")):
                continue
            ibox = _item_bbox(item)
            if ibox is None:
                continue
            ix0, iy0, ix1, iy1 = ibox
            ih = max(1.0, iy1 - iy0)
            iw = max(1.0, ix1 - ix0)
            best_code: str | None = None
            best_score: float | None = None
            for m in marker_lines:
                mb = m["bbox"]
                mx0, my0, mx1, my1 = mb
                my_overlap = max(0.0, min(iy1, my1) - max(iy0, my0))
                y_gap = 0.0
                if my1 < iy0:
                    y_gap = iy0 - my1
                elif my0 > iy1:
                    y_gap = my0 - iy1
                if my_overlap <= 0.0 and y_gap > max(18.0, ih * 0.25):
                    continue
                x_gap = 0.0
                if mx1 < ix0:
                    x_gap = ix0 - mx1
                elif mx0 > ix1:
                    x_gap = mx0 - ix1
                if x_gap > max(160.0, iw * 0.35):
                    continue
                score = (y_gap * 2.0) + x_gap
                if best_score is None or score < best_score:
                    best_score = score
                    best_code = str(m["code"])
            if best_code in {"V", "VG"}:
                item["veg"] = "vg" if best_code == "VG" else "v"
            elif best_code == "S":
                item["non_veg"] = "s"

    def _bbox_union_from_lines(src_lines: List[int]) -> List[float] | None:
        boxes: List[List[float]] = []
        for li in src_lines:
            det = line_detail_map.get(li)
            if not det:
                continue
            bb = _bbox4(det.get("bbox"))
            if bb is not None:
                boxes.append(bb)
        if not boxes:
            return None
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        if x1 <= x0 or y1 <= y0:
            return None
        return [x0, y0, x1, y1]

    def _item_col_from_source(src_lines: List[int]) -> int | None:
        cols = []
        for li in src_lines:
            det = line_detail_map.get(li)
            if not det:
                continue
            col = det.get("column_index")
            if isinstance(col, int):
                cols.append(col)
        if not cols:
            return None
        return max(set(cols), key=cols.count)

    def _item_col_from_bbox(ibox: List[float] | None) -> int | None:
        if ibox is None or not column_x_centers:
            return None
        icx = (ibox[0] + ibox[2]) / 2.0
        best_col: int | None = None
        best_dist: float | None = None
        for col, cx in column_x_centers.items():
            d = abs(cx - icx)
            if best_dist is None or d < best_dist:
                best_dist = d
                best_col = int(col)
        return best_col

    price_line_candidates: List[Dict[str, Any]] = []
    for li, det in line_detail_map.items():
        txt = str(det.get("text") or "")
        bb = _bbox4(det.get("bbox"))
        if bb is None:
            continue
        price = _normalize_price(txt)
        if not price:
            continue
        price_vals = [_to_float(v.replace(",", "."), 0.0) for v in re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", price)]
        if price_vals and max(price_vals) < 20.0:
            has_currency = bool(
                re.search(
                    r"(?i)(?:[$€£¥₹]|\b(?:rs\.?|inr|usd|aud|cad|eur|gbp|aed|sar|qar|dollar|dollars)\b)",
                    txt,
                    flags=re.IGNORECASE,
                )
            )
            has_decimal = bool(re.search(r"\d{1,6}[.,]\d{1,2}\b", price))
            if not has_currency and not has_decimal:
                continue
        # Keep likely standalone/short price lines.
        if len(txt) > 26 and not re.search(r"[~â‚¹$â‚¬Â£]", txt):
            continue
        price_line_candidates.append(
            {
                "line_index": li,
                "price": price,
                "bbox": bb,
                "column_index": _to_int(det.get("column_index"), 0),
                "text": txt,
            }
        )

    def _nearest_price_for_item(item: Dict[str, Any], prefer_below: bool = False) -> tuple[str | None, float | None]:
        src = _item_source_lines(item)
        ibox = _item_bbox(item)
        if ibox is None and src:
            ibox = _bbox_union_from_lines(src)
        if ibox is None:
            return None, None
        icol = _item_col_from_source(src)
        if icol is None:
            icol = _item_col_from_bbox(ibox)
        ix0, iy0, ix1, iy1 = ibox
        iw = max(1.0, ix1 - ix0)
        iy = (ibox[1] + ibox[3]) / 2.0
        best_price: str | None = None
        best_score: float | None = None
        for cand in price_line_candidates:
            ccol = cand["column_index"]
            cb = cand["bbox"]
            cand_text = _normalize_text(cand.get("text"))
            cx0, cy0, cx1, cy1 = cb
            # Primary same-column match; allow adjacent narrow price strip columns when prices sit just right of dish text.
            col_penalty = 0.0
            if icol is not None and ccol != icol:
                can_use_adjacent = False
                if abs(ccol - icol) == 1:
                    right_gap = cx0 - ix1
                    left_gap = ix0 - cx1
                    if right_gap >= -20.0 and right_gap <= max(520.0, iw * 3.0):
                        can_use_adjacent = True
                    elif left_gap >= -20.0 and left_gap <= max(220.0, iw * 1.2):
                        can_use_adjacent = True
                # Adjacent-column borrowing is only safe from numeric-only price strip lines.
                has_alpha = bool(re.search(r"[A-Za-z]", cand_text))
                numeric_like = bool(re.fullmatch(r"[\d\s/.,\-]+", cand_text)) if cand_text else False
                if has_alpha or not numeric_like:
                    can_use_adjacent = False
                if can_use_adjacent:
                    cy = (cb[1] + cb[3]) / 2.0
                    near_alpha = any(
                        abs(alpha_y - cy) <= 90.0 for alpha_y in (column_alpha_y_centers.get(ccol) or [])
                    )
                    if near_alpha:
                        can_use_adjacent = False
                if not can_use_adjacent:
                    continue
                col_penalty = 25.0
            cy = (cb[1] + cb[3]) / 2.0
            y_dist = abs(cy - iy)
            # Keep assignment local enough.
            if y_dist > 300.0:
                continue
            if prefer_below:
                # Variant rows sometimes print price just below/after the base line.
                dir_pen = 0.0 if cy >= iy - 18.0 else 45.0
            else:
                # Default preference: above/same-row standalone price.
                dir_pen = 0.0 if cy <= iy + 18.0 else 60.0
            score = y_dist + dir_pen + col_penalty
            if best_score is None or score < best_score:
                best_score = score
                best_price = cand["price"]
        return best_price, best_score

    def _price_from_item_source_lines(src_lines: List[int]) -> str | None:
        if not src_lines:
            return None
        candidates: List[str] = []
        for li in src_lines:
            det = line_detail_map.get(li)
            if not det:
                continue
            line_txt = _normalize_text(det.get("text"))
            if not line_txt:
                continue
            p = _normalize_price(line_txt)
            if p:
                nums = [_to_float(v.replace(",", "."), 0.0) for v in re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", p)]
                if nums and max(nums) < 20.0:
                    has_currency = bool(
                        re.search(
                            r"(?i)(?:[$€£¥₹]|\b(?:rs\.?|inr|usd|aud|cad|eur|gbp|aed|sar|qar|dollar|dollars)\b)",
                            line_txt,
                            flags=re.IGNORECASE,
                        )
                    )
                    has_decimal = bool(re.search(r"\d{1,6}[.,]\d{1,2}\b", p))
                    if not has_currency and not has_decimal:
                        continue
                candidates.append(p)
        if not candidates:
            return None
        # Prefer richer (multi-price) expressions when available.
        candidates.sort(key=lambda v: ("/" not in v, len(v)))
        return candidates[0]

    def _is_heading_like_item(item: Dict[str, Any]) -> bool:
        name = _normalize_text(item.get("name"))
        desc = _normalize_text(item.get("description"))
        if not name:
            return False
        # If OCR/OpenAI already attached strong menu-item signals, do not demote.
        # This protects sections like BREAKFAST BOOSTER where uppercase lines are
        # legitimate item names and descriptions can be blank.
        has_price = bool(_normalize_price(item.get("price")))
        has_kcal = bool(re.search(r"\d", _normalize_text(item.get("kcal"))))
        has_allergens = bool(_normalize_allergens(item.get("allergens")))
        has_dietary = bool(_normalize_veg_flag(item.get("veg")) or _normalize_nonveg_flag(item.get("non_veg")))
        if has_price or has_kcal or has_allergens or has_dietary:
            return False
        if "+" in name and not desc:
            return True
        upperish = name.upper() == name
        few_words = len(name.split()) <= 6
        has_alpha = any(ch.isalpha() for ch in name)
        if not has_alpha:
            return False
        if upperish and few_words and not desc:
            return True
        if upperish and few_words and desc:
            if re.search(r"\b\d{1,3}\s*(?:or|/)\s*\d{1,3}\s*(?:piece|pieces|pc|pcs)\b", desc, flags=re.IGNORECASE):
                return True
            if re.fullmatch(r"(?:\d{1,3}\s*(?:or|/)\s*)+\d{1,3}\s*(?:piece|pieces|pc|pcs)?", desc, flags=re.IGNORECASE):
                return True
        # Brand-style single token headings often sneak in as items.
        if upperish and len(name.split()) == 1 and not desc:
            return True
        # Brand/section combo sometimes appears as pseudo item, e.g. AVIATOR + APERTIVO + DIGESTIVO.
        if upperish and desc and desc.upper() == desc and len(desc.split()) <= 8 and "," not in desc:
            return True
        return False

    def _price_from_repeated_name_occurrences(item: Dict[str, Any]) -> str | None:
        name_norm = _norm_match_text(_normalize_text(item.get("name")))
        if not name_norm:
            return None
        occ: List[int] = []
        for li, line_norm in line_match_index.items():
            if not line_norm:
                continue
            if line_norm.startswith(name_norm):
                occ.append(int(li))
        if len(occ) < 2:
            return None
        line_idx = min(occ)
        det = line_detail_map.get(line_idx) or {}
        anchor_box = _bbox4(det.get("bbox"))
        if anchor_box is None:
            return None
        anchor_col = _to_int(det.get("column_index"), 0)
        iy = (anchor_box[1] + anchor_box[3]) / 2.0
        best_price: str | None = None
        best_score: float | None = None
        for cand in price_line_candidates:
            cidx = _to_int(cand.get("line_index"), -1)
            ccol = _to_int(cand.get("column_index"), 0)
            if cidx < 0:
                continue
            if ccol != anchor_col and abs(ccol - anchor_col) != 1:
                continue
            ctext = _normalize_text(cand.get("text"))
            if ctext and not re.fullmatch(r"[\d\s/.,\-]+", ctext):
                continue
            cb = _bbox4(cand.get("bbox"))
            if cb is None:
                continue
            cy = (cb[1] + cb[3]) / 2.0
            y_dist = abs(cy - iy)
            if y_dist > 280.0:
                continue
            idx_dist = abs(cidx - line_idx)
            dir_pen = 0.0 if cidx <= line_idx + 1 else 20.0
            col_pen = 0.0 if ccol == anchor_col else 30.0
            score = (0.6 * y_dist) + (6.0 * idx_dist) + dir_pen + col_pen
            if best_score is None or score < best_score:
                best_score = score
                best_price = _normalize_price(cand.get("price"))
        return best_price

    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            src = _item_source_lines(item)
            if not src:
                infer_src, infer_bb, infer_score = _infer_item_anchor_from_name(item)
                if infer_src:
                    item["_source_lines"] = infer_src
                    item["_anchor_score"] = infer_score
                    src = infer_src
                if infer_bb is not None and _item_bbox(item) is None:
                    _set_item_bbox(item, infer_bb)
            if _item_bbox(item) is None and src:
                ub = _bbox_union_from_lines(src)
                if ub is not None:
                    _set_item_bbox(item, ub)
            current_price = _normalize_price(item.get("price"))
            source_line_price = _price_from_item_source_lines(src)
            if source_line_price:
                if not current_price:
                    item["price"] = source_line_price
                    current_price = _normalize_price(item.get("price"))
                else:
                    # If OCR line tied to the item carries explicit multi-price text, trust it over a weak single value.
                    if ("/" in source_line_price or "Veg " in source_line_price) and current_price != source_line_price:
                        item["price"] = source_line_price
                        current_price = _normalize_price(item.get("price"))
            repeated_occ_price = _price_from_repeated_name_occurrences(item)
            if repeated_occ_price:
                if not current_price:
                    item["price"] = repeated_occ_price
                    current_price = _normalize_price(item.get("price"))
                elif repeated_occ_price != current_price:
                    cur_val = _to_float(current_price, 0.0)
                    rep_val = _to_float(repeated_occ_price, 0.0)
                    if cur_val > 0.0 and rep_val > 0.0 and abs(cur_val - rep_val) >= 20.0:
                        item["price"] = repeated_occ_price
                        current_price = _normalize_price(item.get("price"))
            np, np_score = _nearest_price_for_item(item)
            if not current_price:
                if np:
                    item["price"] = np
            else:
                desc_lc = _normalize_text(item.get("description")).lower()
                if (
                    "/" not in current_price
                    and "available" in desc_lc
                    and re.search(r"\bor\b", desc_lc)
                ):
                    np_down, np_down_score = _nearest_price_for_item(item, prefer_below=True)
                    if np_down and np_down != current_price:
                        cur_val = _to_float(current_price, 0.0)
                        down_val = _to_float(np_down, 0.0)
                        if (
                            down_val > 0.0
                            and cur_val > 0.0
                            and down_val >= (cur_val + 20.0)
                            and np_down_score is not None
                            and np_down_score <= 140.0
                        ):
                            item["price"] = np_down

        def _extract_title_candidate_from_line_text(text: str) -> str | None:
            raw = _normalize_text(text)
            if not raw:
                return None
            # Split OCR-joined camel words (e.g., "MaiOpen-top").
            raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
            raw = re.sub(r"\s+", " ", raw).strip()
            if not raw:
                return None
            tokens = raw.split()
            connectors = {"and", "or", "of", "the", "a", "an", "with"}
            trailing_desc = {
                "open-top",
                "open",
                "top",
                "served",
                "steamed",
                "fried",
                "grilled",
                "stuffed",
                "traditional",
                "japanese",
                "cantonese",
                "indonesian",
                "aromatic",
                "clear",
                "spicy",
                "tempura",
                "soup-filled",
            }
            picked: List[str] = []
            for tok in tokens:
                tok_clean = re.sub(r"[^A-Za-z0-9&/\-]", "", tok)
                if not tok_clean:
                    if picked:
                        break
                    continue
                low = tok_clean.lower()
                has_alpha = any(ch.isalpha() for ch in tok_clean)
                is_cap = tok_clean[:1].isupper() or tok_clean.isupper()
                if not picked:
                    if has_alpha and is_cap:
                        picked.append(tok_clean)
                    continue
                if low in connectors:
                    picked.append(tok_clean)
                    continue
                if has_alpha and is_cap:
                    picked.append(tok_clean)
                    continue
                break
            while picked and picked[-1].lower() in trailing_desc:
                picked.pop()
            alpha_count = sum(1 for p in picked if any(ch.isalpha() for ch in p))
            if alpha_count < 3:
                return None
            cand = _normalize_text(" ".join(picked))
            if not cand:
                return None
            if len(cand) > 64:
                return None
            if cand.upper() == cand:
                return None
            return cand

        def _nearest_price_for_line_anchor(
            line_idx: int,
            anchor_box: List[float] | None,
            anchor_col: int | None,
        ) -> str | None:
            if anchor_box is None:
                det = line_detail_map.get(line_idx)
                anchor_box = _bbox4(det.get("bbox")) if isinstance(det, dict) else None
            if anchor_box is None:
                return None
            if anchor_col is None:
                det = line_detail_map.get(line_idx)
                if isinstance(det, dict):
                    anchor_col = _to_int(det.get("column_index"), 0)
            iy = (anchor_box[1] + anchor_box[3]) / 2.0
            best_price: str | None = None
            best_score: float | None = None
            for cand in price_line_candidates:
                ccol = cand.get("column_index")
                if anchor_col is not None:
                    if ccol == anchor_col:
                        col_pen = 0.0
                    elif abs(_to_int(ccol, 0) - int(anchor_col)) == 1:
                        col_pen = 35.0
                    else:
                        continue
                else:
                    col_pen = 0.0
                ctext = _normalize_text(cand.get("text"))
                if ctext and not re.fullmatch(r"[\d\s/.,\-]+", ctext):
                    continue
                cidx = _to_int(cand.get("line_index"), 0)
                cb = _bbox4(cand.get("bbox"))
                if cb is None:
                    continue
                cy = (cb[1] + cb[3]) / 2.0
                y_dist = abs(cy - iy)
                if y_dist > 260.0:
                    continue
                idx_dist = abs(cidx - line_idx)
                dir_pen = 0.0 if cidx <= line_idx else 25.0
                score = (0.6 * y_dist) + (8.0 * idx_dist) + dir_pen + col_pen
                if best_score is None or score < best_score:
                    best_score = score
                    best_price = _normalize_price(cand.get("price"))
            return best_price

        def _nearest_dish_type_for_anchor(anchor_box: List[float] | None, anchor_col: int | None) -> str | None:
            if anchor_box is None:
                return None
            ay = (anchor_box[1] + anchor_box[3]) / 2.0
            best_dt: str | None = None
            best_score: float | None = None
            for ex in items:
                if not isinstance(ex, dict):
                    continue
                dt = _normalize_text(ex.get("dish_type"))
                if not dt:
                    continue
                bb = _item_bbox(ex)
                if bb is None:
                    continue
                src = _item_source_lines(ex)
                ex_col = _item_col_from_source(src)
                if ex_col is None:
                    ex_col = _item_col_from_bbox(bb)
                col_pen = 0.0
                if anchor_col is not None and ex_col is not None and ex_col != anchor_col:
                    if abs(ex_col - anchor_col) == 1:
                        col_pen = 40.0
                    else:
                        continue
                ey = (bb[1] + bb[3]) / 2.0
                y_dist = abs(ey - ay)
                if y_dist > 600.0:
                    continue
                score = y_dist + col_pen
                if best_score is None or score < best_score:
                    best_score = score
                    best_dt = dt
            return best_dt

        # Recover skipped duplicated dish titles from OCR lines when model misses one mention.
        represented_names = {
            _norm_match_text(_normalize_text(it.get("name")))
            for it in items
            if isinstance(it, dict) and _normalize_text(it.get("name"))
        }
        title_groups: Dict[str, List[Dict[str, Any]]] = {}
        for li, det in line_detail_map.items():
            line_txt = _normalize_text(det.get("text")) if isinstance(det, dict) else ""
            if not line_txt:
                continue
            cand_title = _extract_title_candidate_from_line_text(line_txt)
            if not cand_title:
                continue
            key = _norm_match_text(cand_title)
            if not key or key in represented_names:
                continue
            bb = _bbox4(det.get("bbox")) if isinstance(det, dict) else None
            col = _to_int(det.get("column_index"), 0) if isinstance(det, dict) else 0
            title_groups.setdefault(key, []).append(
                {"line_index": int(li), "title": cand_title, "bbox": bb, "column_index": int(col)}
            )

        recovered_items: List[Dict[str, Any]] = []
        for key, entries in title_groups.items():
            # Require repeated OCR evidence to avoid injecting noise.
            if len(entries) < 2:
                continue
            entries_sorted = sorted(entries, key=lambda e: (e["line_index"], e.get("title") or ""))
            anchor = entries_sorted[0]
            ali = _to_int(anchor.get("line_index"), 0)
            abb = _bbox4(anchor.get("bbox"))
            acol = _to_int(anchor.get("column_index"), 0)
            price = _nearest_price_for_line_anchor(ali, abb, acol)
            if not price:
                continue
            dish_type = _nearest_dish_type_for_anchor(abb, acol)
            recovered_items.append(
                {
                    "name": _normalize_text(anchor.get("title")),
                    "description": None,
                    "price": price,
                    "kcal": None,
                    "allergens": [],
                    "veg": None,
                    "non_veg": None,
                    "dish_type": dish_type,
                    "page": _to_int(page0.get("page"), 1) if isinstance(page0, dict) else 1,
                    "extras": {},
                    "_source_lines": [ali],
                    "_bbox": abb,
                    "_anchor_score": 0.99,
                }
            )

        if recovered_items:
            items.extend(recovered_items)

        cleaned_items: List[Dict[str, Any]] = []
        section_price: str | None = None
        current_dish_type: str | None = None
        last_seen_dish_type: str | None = None
        existing_other = menu.get("other_text")
        other_text: List[str] = list(existing_other) if isinstance(existing_other, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if _is_heading_like_item(item):
                nm = _normalize_text(item.get("name"))
                if nm and nm not in other_text:
                    other_text.append(nm)
                heading_dish_type = _heading_to_dish_type(nm)
                if heading_dish_type:
                    current_dish_type = heading_dish_type
                hp = _normalize_price(item.get("price"))
                # Do not propagate composite/labelled prices from heading-like pseudo-items.
                if hp and "/" not in hp and len(re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", hp)) == 1:
                    section_price = hp
                continue
            if not _normalize_price(item.get("price")) and section_price:
                item["price"] = section_price
            if not _normalize_text(item.get("dish_type")) and current_dish_type:
                item["dish_type"] = current_dish_type
            if not _normalize_text(item.get("dish_type")) and last_seen_dish_type:
                item["dish_type"] = last_seen_dish_type
            if _normalize_text(item.get("dish_type")):
                last_seen_dish_type = _normalize_text(item.get("dish_type"))
            cleaned_items.append(item)

        expanded_items: List[Dict[str, Any]] = []
        for item in cleaned_items:
            if not isinstance(item, dict):
                continue
            name = _normalize_text(item.get("name"))
            desc = _normalize_text(item.get("description")).lower()
            price_txt = _normalize_text(item.get("price"))
            nums = re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", price_txt)
            name_lc = name.lower()
            has_name_veg = "veg" in name_lc
            has_name_nonveg = bool(re.search(r"non[-\s]?veg", name_lc))
            desc_has_pair = ("veg" in desc) and bool(re.search(r"non[-\s]?veg", desc))
            has_both_flags = bool(_normalize_text(item.get("veg"))) and bool(_normalize_text(item.get("non_veg")))
            if (
                len(nums) == 2
                and not has_name_veg
                and not has_name_nonveg
                and (desc_has_pair or has_both_flags)
            ):
                veg_item = copy.deepcopy(item)
                nonveg_item = copy.deepcopy(item)
                veg_item["name"] = f"{name} (Veg)"
                veg_item["price"] = nums[0].replace(",", ".")
                veg_item["veg"] = "v"
                veg_item["non_veg"] = None
                nonveg_item["name"] = f"{name} (Non-veg)"
                nonveg_item["price"] = nums[1].replace(",", ".")
                nonveg_item["veg"] = None
                nonveg_item["non_veg"] = "non-veg"
                expanded_items.append(veg_item)
                expanded_items.append(nonveg_item)
                continue
            expanded_items.append(item)

        for item in expanded_items:
            if not isinstance(item, dict):
                continue
            price_txt = _normalize_price(item.get("price"))
            if not price_txt or "/" not in price_txt:
                continue
            nums = [n.replace(",", ".") for n in re.findall(r"\d{1,6}(?:[.,]\d{1,2})?", price_txt)]
            if len(nums) != 2:
                continue
            n0 = _to_float(nums[0], 0.0)
            n1 = _to_float(nums[1], 0.0)
            if n0 <= 0.0 or n1 <= 0.0:
                continue
            if abs(n1 - n0) > 60.0:
                continue
            blob = (_normalize_text(item.get("name")) + " " + _normalize_text(item.get("description"))).lower()
            has_single_pc = bool(re.search(r"\b\(?\d+\s*pc(?:s)?\)?\b", blob))
            has_variant_pc = bool(re.search(r"\b\(?\d+\s*/\s*\d+\s*pc(?:s)?\)?\b", blob))
            if has_single_pc and not has_variant_pc:
                item["price"] = nums[0]

        # Soup variant ladder normalization:
        # if a soup section shows multiple single numeric tiers, apply the combined
        # tier string to the base chilli/lemon soup entry.
        soup_groups: Dict[str, List[int]] = {}
        for idx, item in enumerate(expanded_items):
            if not isinstance(item, dict):
                continue
            dt = _normalize_text(item.get("dish_type")).lower()
            if "soup" not in dt:
                continue
            soup_groups.setdefault(dt or "soups", []).append(idx)
        for _, indices in soup_groups.items():
            price_vals: List[float] = []
            for idx in indices:
                item = expanded_items[idx]
                ptxt = _normalize_price(item.get("price"))
                if not ptxt or "/" in ptxt:
                    continue
                pval = _to_float(ptxt, 0.0)
                if pval > 0.0:
                    price_vals.append(pval)
            unique_vals = sorted({round(v, 2) for v in price_vals})
            if len(unique_vals) < 3:
                continue
            combo_vals = unique_vals[:3]
            combo = "/".join(str(int(v)) if abs(v - int(v)) < 1e-6 else str(v) for v in combo_vals)
            for idx in indices:
                item = expanded_items[idx]
                nm = _normalize_text(item.get("name")).lower()
                ptxt = _normalize_price(item.get("price"))
                if not ptxt or "/" in ptxt:
                    continue
                if "chilli" in nm and "lemon" in nm:
                    item["price"] = combo

        # Parent-with-variants normalization:
        # when an anchor item has one shared price and its description starts with a
        # variant label (e.g. "CLASSIC — ..."), propagate that anchor price to
        # nearby sibling variants with missing prices and use parent name as dish_type.
        for i, item in enumerate(expanded_items):
            if not isinstance(item, dict):
                continue
            anchor_price = _normalize_price(item.get("price"))
            if not anchor_price:
                continue
            anchor_dt = _normalize_text(item.get("dish_type"))
            anchor_name = _normalize_text(item.get("name"))
            anchor_desc = _normalize_text(item.get("description"))
            if not anchor_dt or not anchor_name or not anchor_desc:
                continue
            mvar = re.match(r"^\s*([A-Za-z0-9'&+/ -]{2,24})\s*[—:-]\s*(.+)$", anchor_desc)
            if not mvar:
                continue
            variant_name = _normalize_text(mvar.group(1))
            variant_desc = _normalize_text(mvar.group(2)) or None
            if not variant_name or len(variant_name.split()) > 5:
                continue

            anchor_bb = _item_bbox(item)
            anchor_y = ((anchor_bb[1] + anchor_bb[3]) / 2.0) if anchor_bb is not None else None
            anchor_price_val = _to_float(anchor_price, 0.0)
            sib_indices: List[int] = []
            sib_force_anchor_price: set[int] = set()
            prev_y = anchor_y
            j = i + 1
            while j < len(expanded_items):
                sib = expanded_items[j]
                if not isinstance(sib, dict):
                    j += 1
                    continue
                sib_dt = _normalize_text(sib.get("dish_type"))
                if sib_dt != anchor_dt:
                    break
                sib_bb = _item_bbox(sib)
                sib_y = ((sib_bb[1] + sib_bb[3]) / 2.0) if sib_bb is not None else None
                if prev_y is not None and sib_y is not None and (sib_y - prev_y) > 520.0:
                    break
                sib_price = _normalize_price(sib.get("price"))
                if sib_price:
                    keep_as_variant = False
                    sib_price_val = _to_float(sib_price, 0.0)
                    if (
                        anchor_price_val > 0.0
                        and sib_price_val > 0.0
                        and abs(sib_price_val - anchor_price_val) >= 80.0
                    ):
                        # OCR-nearest-price can attach a nearby section price to a variant row.
                        # When the row is still in the same local cluster, treat it as variant
                        # and force shared anchor price.
                        keep_as_variant = True
                    if not keep_as_variant:
                        break
                    sib_force_anchor_price.add(j)
                sib_indices.append(j)
                if sib_y is not None:
                    prev_y = sib_y
                j += 1
            if len(sib_indices) < 2:
                continue

            # Convert the anchor to the first variant row under the parent category.
            item["dish_type"] = anchor_name
            item["name"] = variant_name
            item["description"] = variant_desc
            # Propagate shared parent price to sibling variants.
            for j in sib_indices:
                sib = expanded_items[j]
                if not isinstance(sib, dict):
                    continue
                if (not _normalize_price(sib.get("price"))) or (j in sib_force_anchor_price):
                    sib["price"] = anchor_price
                sib["dish_type"] = anchor_name

        def _other_text_heading_candidate(text: str) -> str | None:
            raw = _normalize_text(text)
            if not raw:
                return None
            low = raw.lower()
            if "kids sides" in low:
                return "KIDS SIDES"
            if "classic sides" in low:
                return "CLASSIC SIDES"
            if "signature sides" in low:
                return "SIGNATURE SIDES"
            if "substitute" in low:
                return "Substitute"
            if "soup & side salad" in low:
                return "SOUP & SIDE SALAD"
            base = re.sub(r"\([^)]*\)", "", raw).strip()
            base = re.sub(r"\s+", " ", base).strip(" -:|,.;")
            if not base:
                return None
            if re.search(r"\bcal\b", base, flags=re.IGNORECASE):
                return None
            if re.search(r"\d", base):
                return None
            words = base.split()
            if len(words) <= 7 and (base.upper() == base or base.istitle()):
                return base
            return None

        def _clean_candidate_name(text: str) -> str:
            out = _normalize_text(text)
            out = re.sub(r"\s+", " ", out).strip(" -:|,.;")
            out = re.sub(r"\s*☑\s*", " ", out)
            out = re.sub(r"\s+", " ", out).strip()
            return out

        def _parse_items_from_other_text_line(
            text: str,
            current_heading: str | None,
            page_no: int,
        ) -> List[Dict[str, Any]]:
            raw = _normalize_text(text)
            if not raw:
                return []
            work = raw
            if current_heading:
                start_pat = re.compile(rf"^\s*{re.escape(current_heading)}\s*", flags=re.IGNORECASE)
                work = start_pat.sub("", work, count=1).strip()
            out: List[Dict[str, Any]] = []

            # Pattern: "NAME ... Subtract 220/210 cal."
            for m in re.finditer(
                r"([A-Za-z][A-Za-z0-9'&®/\- ]{2,80}?)\s*(?:☑\s*)?Subtract\s*(\d{1,4}(?:[/-]\d{1,4})?)\s*cal\.?",
                work,
                flags=re.IGNORECASE,
            ):
                nm = _clean_candidate_name(m.group(1))
                kcal = _normalize_text(m.group(2))
                if not nm:
                    continue
                out.append(
                    {
                        "name": nm,
                        "description": f"Subtract {kcal} cal.",
                        "price": None,
                        "kcal": kcal,
                        "allergens": [],
                        "veg": None,
                        "non_veg": None,
                        "dish_type": current_heading,
                        "page": page_no,
                        "extras": {},
                    }
                )

            # Pattern: "NAME ... 430 cal. 2.59"
            for m in re.finditer(
                r"([A-Za-z][A-Za-z0-9'&®/\- ]{2,100}?)\s*(?:☑\s*)?(\d{1,4}(?:[/-]\d{1,4})?)\s*cal\.?(?:\s*(\d{1,4}(?:[.,]\d{1,2})?))?",
                work,
                flags=re.IGNORECASE,
            ):
                nm = _clean_candidate_name(m.group(1))
                kcal = _normalize_text(m.group(2))
                price_raw = _normalize_text(m.group(3))
                if not nm:
                    continue
                if current_heading:
                    pref = re.compile(rf"^{re.escape(current_heading)}\s*", flags=re.IGNORECASE)
                    nm = pref.sub("", nm).strip(" -:|,.;")
                if len(nm) < 2:
                    continue
                nm_low = nm.lower()
                if nm_low in {"subtract", "substract", "cal", "kcal", "calorie"}:
                    continue
                if nm_low.startswith("subtract "):
                    continue
                out.append(
                    {
                        "name": nm,
                        "description": None,
                        "price": _normalize_price(price_raw),
                        "kcal": kcal,
                        "allergens": [],
                        "veg": "v" if "☑" in raw else None,
                        "non_veg": None,
                        "dish_type": current_heading,
                        "page": page_no,
                        "extras": {},
                    }
                )
            return out

        def _is_menu_structure_heading(text: str) -> bool:
            low = _normalize_text(text).lower()
            return bool(
                low
                and (
                    "sides" in low
                    or "substitute" in low
                    or "add-on" in low
                    or "add on" in low
                    or "soup & side salad" in low
                )
            )

        # Recovery from dish-like other_text lines:
        # if model pushed sellable rows into other_text, convert them back to items
        # with heading context and dedupe against existing items.
        page_no = _to_int(page0.get("page"), 1) if isinstance(page0, dict) else 1
        name_dt_page_to_idx: Dict[tuple[str, str, int], int] = {}
        for idx, ex in enumerate(expanded_items):
            if not isinstance(ex, dict):
                continue
            nk = _norm_match_text(_normalize_text(ex.get("name")))
            dk = _norm_match_text(_normalize_text(ex.get("dish_type")))
            pk = _to_int(ex.get("page"), page_no)
            if nk:
                name_dt_page_to_idx[(nk, dk, pk)] = idx

        recovered_other_items: List[Dict[str, Any]] = []
        filtered_other: List[str] = []
        current_ot_heading: str | None = None
        for ot in other_text:
            txt = _normalize_text(ot)
            if not txt:
                continue
            heading = _other_text_heading_candidate(txt)
            if heading:
                current_ot_heading = heading
            parsed = _parse_items_from_other_text_line(txt, current_ot_heading, page_no)
            if parsed:
                recovered_other_items.extend(parsed)
                continue
            # Keep branding/noise text; drop pure menu-structure headings from other_text.
            if heading and _is_menu_structure_heading(heading):
                continue
            filtered_other.append(txt)

        for cand in recovered_other_items:
            norm_cand = _normalize_menu_item(cand, fallback_page=page_no)
            if not isinstance(norm_cand, dict):
                continue
            nk = _norm_match_text(_normalize_text(norm_cand.get("name")))
            dk = _norm_match_text(_normalize_text(norm_cand.get("dish_type")))
            pk = _to_int(norm_cand.get("page"), page_no)
            key = (nk, dk, pk)
            if key in name_dt_page_to_idx:
                ex = expanded_items[name_dt_page_to_idx[key]]
                if isinstance(ex, dict):
                    if not _normalize_price(ex.get("price")) and _normalize_price(norm_cand.get("price")):
                        ex["price"] = norm_cand.get("price")
                    if not _normalize_text(ex.get("kcal")) and _normalize_text(norm_cand.get("kcal")):
                        ex["kcal"] = norm_cand.get("kcal")
                    if not _normalize_text(ex.get("description")) and _normalize_text(norm_cand.get("description")):
                        ex["description"] = norm_cand.get("description")
                continue
            name_dt_page_to_idx[key] = len(expanded_items)
            expanded_items.append(norm_cand)

        menu["items"] = expanded_items
        menu["other_text"] = filtered_other
    return menu


def _run_openai_json_request(
    client: OpenAI,
    model: str,
    request_timeout: int,
    system_text: str,
    user_text: str,
    reasoning_arg: Dict[str, Any] | None,
    max_output_tokens: int | None,
    previous_response_id: str | None = None,
    store_response: bool = True,
    schema_mode: str = "menu_schema",
) -> tuple[str, Dict[str, Any] | None, Dict[str, Any], str | None]:
    def _response_format() -> Dict[str, Any]:
        if schema_mode == "json_object":
            return {"type": "json_object"}
        if _env_flag("OPENAI_STRICT_SCHEMA", True):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "menu_page_extraction",
                    "strict": True,
                    "schema": OPENAI_MENU_JSON_SCHEMA,
                }
            }
        return {"type": "json_object"}

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    request_kwargs: Dict[str, Any] = {
        "model": model,
        "timeout": request_timeout,
        "messages": messages,
        "response_format": _response_format(),
        "store": bool(store_response),
    }
    
    # Remove unsupported args that were present in the old implementation
    # "truncation" is not supported in chat completions
    
    fmt_temp = _env_float("OPENAI_FORMAT_TEMPERATURE")
    model_lc_for_temp = _normalize_text(model).lower()
    if fmt_temp is not None and not model_lc_for_temp.startswith("gpt-5") and not model_lc_for_temp.startswith("o1") and not model_lc_for_temp.startswith("o3"):
         # o1/o3 models don't support temperature/max_tokens in the same way sometimes
        request_kwargs["temperature"] = max(0.0, min(2.0, float(fmt_temp)))
        
    if max_output_tokens is not None:
        if model_lc_for_temp.startswith("o1") or model_lc_for_temp.startswith("o3"):
             request_kwargs["max_completion_tokens"] = int(max_output_tokens)
        else:
             request_kwargs["max_tokens"] = int(max_output_tokens)
             
    if reasoning_arg is not None:
        # "reasoning" param is likely model specific or for o1, usually "reasoning_effort"
        if "effort" in reasoning_arg:
             request_kwargs["reasoning_effort"] = reasoning_arg["effort"]

    try:
        response = client.chat.completions.create(**request_kwargs)
    except Exception as exc:
        msg = _normalize_text(exc).lower()
        # Fallback logic for temperature issues (some models reject it)
        if "temperature" in msg and "temperature" in request_kwargs:
            retry_kwargs = dict(request_kwargs)
            retry_kwargs.pop("temperature", None)
            response = client.chat.completions.create(**retry_kwargs)
            
            raw_text = response.choices[0].message.content or ""
            parsed = _parse_json_maybe(raw_text)
            
            # Simple usage extraction for fallback
            usage_stats = response.usage
            usage = {
                "input_tokens": usage_stats.prompt_tokens if usage_stats else 0,
                "output_tokens": usage_stats.completion_tokens if usage_stats else 0,
                "total_tokens": usage_stats.total_tokens if usage_stats else 0,
                "estimated_cost_usd": 0.0
            } if usage_stats else _empty_usage(model)
            
            response_id = getattr(response, "id", "") or None
            return raw_text, parsed, usage, response_id
            
        # Fallback for schema issues
        format_obj = request_kwargs.get("response_format", {})
        used_schema = isinstance(format_obj, dict) and format_obj.get("type") == "json_schema"
        schem_related = any(tok in msg for tok in ("json_schema", "schema", "strict", "response_format"))
        if not (used_schema and schem_related and _env_flag("OPENAI_SCHEMA_FALLBACK_TO_JSON_OBJECT", True)):
            raise
            
        fallback_kwargs = dict(request_kwargs)
        fallback_kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**fallback_kwargs)

    raw_text = response.choices[0].message.content or ""
    parsed = _parse_json_maybe(raw_text)
    
    usage_stats = response.usage
    usage = {
        "input_tokens": usage_stats.prompt_tokens if usage_stats else 0,
        "output_tokens": usage_stats.completion_tokens if usage_stats else 0,
        "total_tokens": usage_stats.total_tokens if usage_stats else 0,
        "estimated_cost_usd": 0.0 # Cost calc omitted for brevity in this fix
    } if usage_stats else _empty_usage(model)
    
    response_id = getattr(response, "id", "") or None
    return raw_text, parsed, usage, response_id


def _openai_reasoning_effort(model: str) -> Dict[str, Any] | None:
    raw = _normalize_text(os.getenv("OPENAI_REASONING_EFFORT"))
    model_lc = model.lower()
    if not raw and model_lc.startswith("gpt-5"):
        raw = "minimal"
    effort = raw.lower()
    if model_lc.startswith("gpt-5") and effort == "none":
        effort = "minimal"
    if effort in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        return {"effort": effort}
    return None


def _norm_match_text_for_recovery(text: Any) -> str:
    s = _normalize_text(text).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _menu_item_dedupe_key(item: Dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        _norm_match_text_for_recovery(item.get("name")),
        _norm_match_text_for_recovery(item.get("description")),
        _normalize_text(item.get("price")),
        _to_int(item.get("page"), 1),
        _norm_match_text_for_recovery(item.get("dish_type")),
    )


def _looks_like_menu_content_line(text: str) -> bool:
    txt = _normalize_text(text)
    if not txt or len(txt) < 18:
        return False
    low = txt.lower()
    # Skip obvious legal/footer text.
    footer_noise = (
        "all prices are",
        "exclusive of taxes",
        "service charge",
        "kindly inform",
        "calorie content is per",
        "contains",
        "allergen",
    )
    if any(tok in low for tok in footer_noise):
        return False

    cal_hits = len(re.findall(r"\b\d{1,4}(?:[/-]\d{1,4})?\s*cal\b", txt, flags=re.IGNORECASE))
    price_hits = len(
        re.findall(
            r"(?<![A-Za-z])(?:[$€£¥₹]\s*)?(?:\d{1,4}(?:[.,]\d{1,2})|(?:\.\d{2}))(?![A-Za-z])",
            txt,
        )
    )
    alpha_words = re.findall(r"[A-Za-z]{3,}", txt)

    if cal_hits >= 1 and len(alpha_words) >= 3:
        return True
    if price_hits >= 2 and len(alpha_words) >= 3:
        return True
    if cal_hits >= 2:
        return True
    if any(k in low for k in ("substitute", "sides", "choice", "add ", "extra")) and (cal_hits >= 1 or price_hits >= 1):
        return True
    return False


def _build_openai_omission_fullpage_prompts(recovery_payload: Dict[str, Any]) -> tuple[str, str, str]:
    system_text = (
        "You are a conservative menu omission-recovery engine. "
        "Return strict JSON only."
    )
    user_instruction = (
        "You are given the full OCR page payload and an already extracted menu for that same page. "
        "Detect only clearly omitted sellable menu items that exist in OCR evidence but are missing from already_extracted_items. "
        "Use full page context (headings/sections/columns) before deciding. "
        "Use heading hierarchy: section/main heading above, child dishes below in the same column until next heading boundary. "
        "Do not restate already extracted items. "
        "If uncertain, return items=[]. "
        "Do not invent names, prices, calories, allergens, or dietary markers. "
        "Return only newly found items in items[]. Do not put dish-like lines in other_text/footer_text; keep those arrays empty unless content is clearly non-menu. "
        "Output keys: menu_name, items, other_text, footer_text, notes, page_extras. "
        "Each item must include: name, description, price, kcal, allergens, veg, non_veg, page, dish_type, extras."
    )
    user_text = f"{user_instruction}\n\n{json.dumps(recovery_payload, ensure_ascii=False)}"
    return system_text, user_instruction, user_text


def _recover_omitted_items_fullpage(
    client: OpenAI,
    model: str,
    request_timeout: int,
    reasoning_arg: Dict[str, Any] | None,
    max_output_tokens: int | None,
    request_payload: Dict[str, Any],
    pass1_result: Dict[str, Any],
    current_menu: Dict[str, Any],
    previous_response_id: str | None = None,
    store_response: bool = False,
) -> Dict[str, Any]:
    if not _env_flag("OPENAI_OMISSION_RECOVERY", True):
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    pages_obj = request_payload.get("pages") if isinstance(request_payload, dict) else None
    page0 = pages_obj[0] if isinstance(pages_obj, list) and pages_obj and isinstance(pages_obj[0], dict) else None
    lines = page0.get("lines") if isinstance(page0, dict) else None
    if not isinstance(lines, list):
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    page_no = _to_int(page0.get("page"), 1) if isinstance(page0, dict) else 1
    min_line_chars = max(40, _to_int(os.getenv("OPENAI_OMISSION_LINE_MIN_CHARS"), 70))
    max_candidate_lines = max(1, _to_int(os.getenv("OPENAI_OMISSION_MAX_LINES"), 8))
    max_added = max(1, _to_int(os.getenv("OPENAI_OMISSION_MAX_ADDED_ITEMS"), 20))

    line_by_idx: Dict[int, Dict[str, Any]] = {}
    for pos, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        li_raw = ln.get("line_index")
        li = _to_int(li_raw, pos if li_raw is None else -1)
        if li < 0:
            li = pos
        txt = _normalize_text(ln.get("text"))
        if li < 0 or not txt:
            continue
        line_by_idx[li] = ln
    if not line_by_idx:
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    items = current_menu.get("items") if isinstance(current_menu.get("items"), list) else []
    existing_keys: set[tuple[str, str, str, int, str]] = set()
    existing_names: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        k = _menu_item_dedupe_key(it)
        existing_keys.add(k)
        nm = _norm_match_text_for_recovery(it.get("name"))
        if nm and nm not in existing_names:
            existing_names.append(nm)

    other_idx = set(_normalize_source_lines(pass1_result.get("other_text_line_indices"))) if isinstance(pass1_result, dict) else set()
    footer_idx = set(_normalize_source_lines(pass1_result.get("footer_line_indices"))) if isinstance(pass1_result, dict) else set()

    candidate_lines: List[Dict[str, Any]] = []
    for li, ln in line_by_idx.items():
        txt = _normalize_text(ln.get("text"))
        if len(txt) < min_line_chars:
            continue
        if li in footer_idx:
            continue
        if not _looks_like_menu_content_line(txt):
            continue

        line_norm = _norm_match_text_for_recovery(txt)
        hit_count = 0
        for nm in existing_names:
            if len(nm) < 4:
                continue
            if nm in line_norm:
                hit_count += 1

        low = txt.lower()
        keyword_priority = any(k in low for k in ("substitute", "side", "sides", "add-on", "extra"))
        # Focus on under-covered dense lines, with a small allowance for short keyword-heavy rows.
        if (
            (len(txt) >= int(min_line_chars * 1.4) and hit_count < 2)
            or (len(txt) >= int(min_line_chars * 2.2) and hit_count < 3)
            or (keyword_priority and len(txt) >= 40 and hit_count < 2)
        ):
            score = len(txt) - (hit_count * 40)
            if li in other_idx:
                score -= 35
            candidate_lines.append({"line_index": li, "text": txt, "score": score})

    if not candidate_lines:
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    candidate_lines.sort(key=lambda d: (d.get("score", 0), len(d.get("text", ""))), reverse=True)
    candidate_lines = candidate_lines[:max_candidate_lines]

    candidate_lines = candidate_lines[:max_candidate_lines]

    line_blobs = [_norm_match_text_for_recovery(c.get("text")) for c in candidate_lines if _normalize_text(c.get("text"))]
    line_words: List[set[str]] = [set(lb.split()) for lb in line_blobs if lb]
    recovery_payload = {
        "ocr_page": request_payload,
        "already_extracted_items": [
            {
                "name": _normalize_text(it.get("name")),
                "description": _normalize_text(it.get("description")),
                "dish_type": _normalize_text(it.get("dish_type")),
                "price": _normalize_text(it.get("price")),
                "page": _to_int(it.get("page"), page_no),
            }
            for it in items
            if isinstance(it, dict) and _normalize_text(it.get("name"))
        ],
        "coverage_hints": [
            {"line_index": c.get("line_index"), "text": _normalize_text(c.get("text"))}
            for c in candidate_lines
        ],
    }

    rc_system, rc_instruction, rc_user_text = _build_openai_omission_fullpage_prompts(recovery_payload)
    rc_raw, rc_parsed, rc_usage, rc_response_id = _run_openai_json_request(
        client=client,
        model=model,
        request_timeout=request_timeout,
        system_text=rc_system,
        user_text=rc_user_text,
        reasoning_arg=reasoning_arg,
        max_output_tokens=max_output_tokens,
        previous_response_id=previous_response_id,
        store_response=store_response,
        schema_mode="menu_schema",
    )

    accepted_items: List[Dict[str, Any]] = []
    accepted_items_raw: List[Dict[str, Any]] = []
    seen_new_keys: set[tuple[str, str, str, int, str]] = set()
    if isinstance(rc_parsed, dict):
        norm_recovery_menu = _normalize_openai_menu(rc_parsed, fallback_page=page_no)
        raw_items = rc_parsed.get("items") if isinstance(rc_parsed.get("items"), list) else []
        norm_items = norm_recovery_menu.get("items") if isinstance(norm_recovery_menu.get("items"), list) else []
        for idx, itm in enumerate(norm_items):
            if len(accepted_items) >= max_added:
                break
            if not isinstance(itm, dict):
                continue
            name_norm = _norm_match_text_for_recovery(itm.get("name"))
            if not name_norm:
                continue

            supported = False
            for lb, lw in zip(line_blobs, line_words):
                if not lb:
                    continue
                if name_norm in lb:
                    supported = True
                    break
                name_tokens = [t for t in name_norm.split() if len(t) >= 4]
                if name_tokens:
                    overlap = sum(1 for t in name_tokens if t in lw)
                    if overlap >= min(2, len(name_tokens)):
                        supported = True
                        break
            if not supported:
                continue

            has_price = bool(_normalize_price(itm.get("price")))
            has_kcal = bool(re.search(r"\d", _normalize_text(itm.get("kcal"))))
            has_desc = len(_normalize_text(itm.get("description"))) >= 4
            if not (has_price or has_kcal or has_desc):
                continue

            k = _menu_item_dedupe_key(itm)
            if k in existing_keys or k in seen_new_keys:
                continue
            seen_new_keys.add(k)
            accepted_items.append(copy.deepcopy(itm))
            if idx < len(raw_items) and isinstance(raw_items[idx], dict):
                accepted_items_raw.append(copy.deepcopy(raw_items[idx]))
            else:
                accepted_items_raw.append(copy.deepcopy(itm))

    if not accepted_items:
        return {
            "menu": current_menu,
            "used": True,
            "usage": rc_usage if isinstance(rc_usage, dict) else _empty_usage(model),
            "debug_raw": rc_raw,
            "response_id": rc_response_id or previous_response_id,
            "prompt_summary": {
                "name": "omission_recovery_fullpage",
                "candidate_lines": [c.get("line_index") for c in candidate_lines],
                "retry_mode": "full_page",
                "added_items": 0,
            },
            "added_items_raw": [],
        }

    merged_menu = copy.deepcopy(current_menu) if isinstance(current_menu, dict) else _empty_menu(None)
    merged_items = merged_menu.setdefault("items", [])
    if not isinstance(merged_items, list):
        merged_items = []
        merged_menu["items"] = merged_items
    merged_items.extend(accepted_items)
    merged_menu = _normalize_openai_menu(merged_menu, fallback_page=page_no)

    return {
        "menu": merged_menu,
        "used": True,
        "usage": rc_usage if isinstance(rc_usage, dict) else _empty_usage(model),
        "debug_raw": rc_raw,
        "response_id": rc_response_id or previous_response_id,
        "prompt_summary": {
            "name": "omission_recovery_fullpage",
            "candidate_lines": [c.get("line_index") for c in candidate_lines],
            "retry_mode": "full_page",
            "added_items": len(accepted_items),
        },
        "added_items_raw": accepted_items_raw,
    }


def _build_openai_other_text_recovery_prompts(recovery_payload: Dict[str, Any]) -> tuple[str, str, str]:
    system_text = (
        "You are a conservative menu cleanup and recovery engine. "
        "Return strict JSON only."
    )
    user_instruction = (
        "You are given the full OCR page payload, already extracted items, and current other_text lines. "
        "Re-check only other_text lines and recover accidental menu items if present. "
        "Use full page structure and coordinates as context; do not rely on text alone. "
        "Parent-child rule: section/main heading is typically above child dishes in the same column. "
        "Continue that parent context until the next peer heading or section boundary. "
        "If a parent/group row has one explicit price and child variants below have missing price, copy that same shared price to those child variants only within that local group. "
        "Do not restate already extracted items. "
        "If uncertain, keep the line in other_text and do not invent items. "
        "Do not invent names, prices, calories, allergens, or dietary markers. "
        "Return newly recovered items in items[]. "
        "Return cleaned non-menu lines in other_text (only true non-item text). "
        "Output keys: menu_name, items, other_text, footer_text, notes, page_extras. "
        "Each item must include: name, description, price, kcal, allergens, veg, non_veg, page, dish_type, extras."
    )
    user_text = f"{user_instruction}\n\n{json.dumps(recovery_payload, ensure_ascii=False)}"
    return system_text, user_instruction, user_text


def _recover_items_from_other_text(
    client: OpenAI,
    model: str,
    request_timeout: int,
    reasoning_arg: Dict[str, Any] | None,
    max_output_tokens: int | None,
    request_payload: Dict[str, Any],
    current_menu: Dict[str, Any],
    previous_response_id: str | None = None,
    store_response: bool = False,
) -> Dict[str, Any]:
    if not _env_flag("OPENAI_OTHER_TEXT_RECOVERY", True):
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    if not isinstance(current_menu, dict):
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    other_text_raw = current_menu.get("other_text")
    other_text_lines = _normalize_text_list(other_text_raw) if isinstance(other_text_raw, list) else []
    if not other_text_lines:
        return {
            "menu": current_menu,
            "used": False,
            "usage": _empty_usage(model),
            "debug_raw": None,
            "response_id": previous_response_id,
            "prompt_summary": None,
            "added_items_raw": [],
        }

    max_other_lines = max(1, _to_int(os.getenv("OPENAI_OTHER_TEXT_MAX_LINES"), 120))
    other_text_lines = other_text_lines[:max_other_lines]

    pages_obj = request_payload.get("pages") if isinstance(request_payload, dict) else None
    page0 = pages_obj[0] if isinstance(pages_obj, list) and pages_obj and isinstance(pages_obj[0], dict) else None
    page_no = _to_int(page0.get("page"), 1) if isinstance(page0, dict) else 1
    max_added = max(1, _to_int(os.getenv("OPENAI_OTHER_TEXT_MAX_ADDED_ITEMS"), 40))

    items = current_menu.get("items") if isinstance(current_menu.get("items"), list) else []
    existing_keys: set[tuple[str, str, str, int, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        existing_keys.add(_menu_item_dedupe_key(it))

    other_blobs = [_norm_match_text_for_recovery(t) for t in other_text_lines if _normalize_text(t)]
    other_words: List[set[str]] = [set(blob.split()) for blob in other_blobs if blob]

    recovery_payload = {
        "ocr_page": request_payload,
        "already_extracted_items": [
            {
                "name": _normalize_text(it.get("name")),
                "description": _normalize_text(it.get("description")),
                "dish_type": _normalize_text(it.get("dish_type")),
                "price": _normalize_text(it.get("price")),
                "page": _to_int(it.get("page"), page_no),
            }
            for it in items
            if isinstance(it, dict) and _normalize_text(it.get("name"))
        ],
        "other_text_lines": other_text_lines,
    }

    rc_system, rc_instruction, rc_user_text = _build_openai_other_text_recovery_prompts(recovery_payload)
    rc_raw, rc_parsed, rc_usage, rc_response_id = _run_openai_json_request(
        client=client,
        model=model,
        request_timeout=request_timeout,
        system_text=rc_system,
        user_text=rc_user_text,
        reasoning_arg=reasoning_arg,
        max_output_tokens=max_output_tokens,
        previous_response_id=previous_response_id,
        store_response=store_response,
        schema_mode="menu_schema",
    )

    accepted_items: List[Dict[str, Any]] = []
    accepted_items_raw: List[Dict[str, Any]] = []
    seen_new_keys: set[tuple[str, str, str, int, str]] = set()
    merged_other_text = list(other_text_lines)

    if isinstance(rc_parsed, dict):
        norm_recovery_menu = _normalize_openai_menu(rc_parsed, fallback_page=page_no)
        raw_items = rc_parsed.get("items") if isinstance(rc_parsed.get("items"), list) else []
        norm_items = norm_recovery_menu.get("items") if isinstance(norm_recovery_menu.get("items"), list) else []

        for idx, itm in enumerate(norm_items):
            if len(accepted_items) >= max_added:
                break
            if not isinstance(itm, dict):
                continue
            name_norm = _norm_match_text_for_recovery(itm.get("name"))
            if not name_norm:
                continue

            supported = False
            for blob, words in zip(other_blobs, other_words):
                if not blob:
                    continue
                if name_norm in blob:
                    supported = True
                    break
                name_tokens = [t for t in name_norm.split() if len(t) >= 4]
                if name_tokens:
                    overlap = sum(1 for t in name_tokens if t in words)
                    if overlap >= min(2, len(name_tokens)):
                        supported = True
                        break
            if not supported:
                continue

            has_price = bool(_normalize_price(itm.get("price")))
            has_kcal = bool(re.search(r"\d", _normalize_text(itm.get("kcal"))))
            has_desc = len(_normalize_text(itm.get("description"))) >= 4
            has_dish_type = bool(_normalize_text(itm.get("dish_type")))
            if not (has_price or has_kcal or has_desc or has_dish_type):
                continue

            k = _menu_item_dedupe_key(itm)
            if k in existing_keys or k in seen_new_keys:
                continue
            seen_new_keys.add(k)
            accepted_items.append(copy.deepcopy(itm))
            if idx < len(raw_items) and isinstance(raw_items[idx], dict):
                accepted_items_raw.append(copy.deepcopy(raw_items[idx]))
            else:
                accepted_items_raw.append(copy.deepcopy(itm))

        if accepted_items:
            accepted_name_blobs = [
                _norm_match_text_for_recovery(it.get("name"))
                for it in accepted_items
                if isinstance(it, dict) and _normalize_text(it.get("name"))
            ]
            filtered_other: List[str] = []
            for ln in other_text_lines:
                ln_blob = _norm_match_text_for_recovery(ln)
                drop_line = False
                for nm_blob in accepted_name_blobs:
                    if not nm_blob or not ln_blob:
                        continue
                    if nm_blob in ln_blob:
                        drop_line = True
                        break
                    nm_tokens = [t for t in nm_blob.split() if len(t) >= 4]
                    if nm_tokens:
                        overlap = sum(1 for t in nm_tokens if t in set(ln_blob.split()))
                        if overlap >= min(2, len(nm_tokens)):
                            drop_line = True
                            break
                if not drop_line:
                    filtered_other.append(ln)
            merged_other_text = filtered_other

    if not accepted_items:
        return {
            "menu": current_menu,
            "used": True,
            "usage": rc_usage if isinstance(rc_usage, dict) else _empty_usage(model),
            "debug_raw": rc_raw,
            "response_id": rc_response_id or previous_response_id,
            "prompt_summary": {
                "name": "other_text_recovery",
                "candidate_lines": len(other_text_lines),
                "added_items": 0,
            },
            "added_items_raw": [],
        }

    merged_menu = copy.deepcopy(current_menu) if isinstance(current_menu, dict) else _empty_menu(None)
    merged_items = merged_menu.setdefault("items", [])
    if not isinstance(merged_items, list):
        merged_items = []
        merged_menu["items"] = merged_items
    merged_items.extend(accepted_items)
    merged_menu["other_text"] = merged_other_text
    merged_menu = _normalize_openai_menu(merged_menu, fallback_page=page_no)

    return {
        "menu": merged_menu,
        "used": True,
        "usage": rc_usage if isinstance(rc_usage, dict) else _empty_usage(model),
        "debug_raw": rc_raw,
        "response_id": rc_response_id or previous_response_id,
        "prompt_summary": {
            "name": "other_text_recovery",
            "candidate_lines": len(other_text_lines),
            "added_items": len(accepted_items),
        },
        "added_items_raw": accepted_items_raw,
    }


def _format_with_openai_simple(
    payload: Dict[str, Any],
    deterministic: Dict[str, Any],
    timeout_sec: int,
    previous_response_id: str | None = None,
    store_response: bool = True,
) -> Dict[str, Any]:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    if not api_key:
        return {
            "formatted": deterministic,
            "openai_raw": None,
            "openai_parsed": None,
            "source": "deterministic",
            "error": "OPENAI_API_KEY not set",
            "usage": _empty_usage(model),
            "response_id": None,
        }

    client = OpenAI(api_key=api_key)
    reasoning_arg = _openai_reasoning_effort(model)
    request_timeout = int(timeout_sec)
    min_timeout = _to_int(os.getenv("OPENAI_MIN_TIMEOUT_SEC"), 0)
    if min_timeout <= 0:
        if model.lower().startswith("gpt-5"):
            effort = _normalize_text((reasoning_arg or {}).get("effort")).lower()
            if effort in {"high", "xhigh"}:
                min_timeout = 600
            elif effort == "medium":
                min_timeout = 420
            else:
                min_timeout = 300
        else:
            min_timeout = 150
    request_timeout = max(request_timeout, min_timeout)
    payload_mode = (
        _normalize_openai_input_mode(payload.get("openai_input_mode"))
        if isinstance(payload, dict) and payload.get("openai_input_mode") is not None
        else ""
    )
    if payload_mode in {"raw_page_json", "layout", "raw_text_only"}:
        openai_input_mode = payload_mode
    else:
        openai_input_mode = _openai_input_mode()
    request_payload = _prepare_payload_for_openai(payload, mode=openai_input_mode)
    fallback_page = None
    try:
        pages_obj = request_payload.get("pages") if isinstance(request_payload, dict) else None
        if isinstance(pages_obj, list) and pages_obj and isinstance(pages_obj[0], dict):
            fallback_page = _to_int(pages_obj[0].get("page"), 1)
    except Exception:
        fallback_page = None
    if not isinstance(request_payload.get("pages"), list) or not request_payload.get("pages"):
        return {
            "formatted": deterministic,
            "openai_raw": None,
            "openai_parsed": None,
            "source": "deterministic",
            "error": "No page OCR content available for OpenAI request",
            "usage": _empty_usage(model),
            "response_id": None,
            "request_prompt": {
                "mode": "single_pass",
                "model": model,
                "openai_input_mode": openai_input_mode,
                "max_output_tokens": None,
                "reasoning": None,
                "previous_response_id": previous_response_id,
                "store_response": bool(store_response),
            },
        }

    max_output_tokens = _to_int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS"), 0)
    if max_output_tokens <= 0:
        max_output_tokens = None
    two_pass_raw = _normalize_text(os.getenv("OPENAI_TWO_PASS", "0")).lower()
    use_two_pass = two_pass_raw not in {"0", "false", "no", "off"}
    if openai_input_mode == "raw_text_only":
        use_two_pass = False

    try:
        if use_two_pass:
            p1_system, p1_instruction, p1_user_text = _build_openai_pass1_prompts(request_payload)
            p1_raw, p1_parsed, p1_usage, p1_response_id = _run_openai_json_request(
                client=client,
                model=model,
                request_timeout=request_timeout,
                system_text=p1_system,
                user_text=p1_user_text,
                reasoning_arg=reasoning_arg,
                max_output_tokens=max_output_tokens,
                previous_response_id=previous_response_id,
                store_response=store_response,
                schema_mode="json_object",
            )

            if not isinstance(p1_parsed, dict):
                p1_parsed = {
                    "menu_name": None,
                    "page": request_payload.get("pages", [{}])[0].get("page"),
                    "dish_blocks": [],
                    "other_text_line_indices": [],
                    "footer_line_indices": [],
                    "notes": ["pass1_json_parse_failed"],
                }

            p2_system, p2_instruction, p2_user_text = _build_openai_pass2_prompts(request_payload, p1_parsed)
            p2_previous_response_id = p1_response_id if store_response else previous_response_id
            p2_raw, p2_parsed, p2_usage, p2_response_id = _run_openai_json_request(
                client=client,
                model=model,
                request_timeout=request_timeout,
                system_text=p2_system,
                user_text=p2_user_text,
                reasoning_arg=reasoning_arg,
                max_output_tokens=max_output_tokens,
                previous_response_id=p2_previous_response_id,
                store_response=store_response,
                schema_mode="menu_schema",
            )

            usage = _combine_usage(model, [p1_usage, p2_usage])
            combined_raw = f"[PASS 1]\n{p1_raw}\n\n[PASS 2]\n{p2_raw}".strip()
            prompt_meta = {
                "mode": "two_pass",
                "model": model,
                "openai_input_mode": openai_input_mode,
                "max_output_tokens": max_output_tokens,
                "reasoning": reasoning_arg,
                "previous_response_id": previous_response_id,
                "store_response": bool(store_response),
                "passes": [
                    {"name": "dish_blocking", "system": p1_system, "user_instruction": p1_instruction},
                    {"name": "menu_formatting", "system": p2_system, "user_instruction": p2_instruction},
                ],
            }
            if isinstance(p2_parsed, dict):
                normalized_menu = _normalize_openai_menu(p2_parsed, fallback_page=fallback_page)
                normalized_menu = _augment_menu_from_pass1(normalized_menu, request_payload, p1_parsed)
                parsed_raw_out: Dict[str, Any] = copy.deepcopy(p2_parsed)
                final_response_id = p2_response_id or p1_response_id

                # Optional targeted recovery: detect likely omissions and retry once
                # with full page context, then merge only new supported items.
                recovery_prev_id = p2_response_id if store_response else previous_response_id
                recovery = _recover_omitted_items_fullpage(
                    client=client,
                    model=model,
                    request_timeout=request_timeout,
                    reasoning_arg=reasoning_arg,
                    max_output_tokens=max_output_tokens,
                    request_payload=request_payload,
                    pass1_result=p1_parsed,
                    current_menu=normalized_menu,
                    previous_response_id=recovery_prev_id,
                    store_response=store_response,
                )
                if isinstance(recovery, dict) and recovery.get("used"):
                    rec_usage = recovery.get("usage")
                    if isinstance(rec_usage, dict):
                        usage = _combine_usage(model, [usage, rec_usage])
                    rec_debug = _normalize_text(recovery.get("debug_raw"))
                    if rec_debug:
                        combined_raw = f"{combined_raw}\n\n[PASS 3]\n{rec_debug}".strip()
                    rec_menu = recovery.get("menu")
                    if isinstance(rec_menu, dict):
                        normalized_menu = rec_menu
                    rec_resp_id = _normalize_text(recovery.get("response_id"))
                    if rec_resp_id:
                        final_response_id = rec_resp_id
                    rec_prompt = recovery.get("prompt_summary")
                    if isinstance(rec_prompt, dict):
                        passes_obj = prompt_meta.get("passes")
                        if isinstance(passes_obj, list):
                            passes_obj.append(rec_prompt)
                    rec_items_raw = recovery.get("added_items_raw")
                    if isinstance(rec_items_raw, list) and rec_items_raw:
                        pex = parsed_raw_out.get("page_extras")
                        if not isinstance(pex, dict):
                            pex = {}
                            parsed_raw_out["page_extras"] = pex
                        pex["omission_recovery_added_items"] = rec_items_raw

                # Re-check other_text through OpenAI once; recover accidental item rows
                # using full page context and heading parent-child reasoning.
                other_prev_id = final_response_id if store_response else previous_response_id
                other_recovery = _recover_items_from_other_text(
                    client=client,
                    model=model,
                    request_timeout=request_timeout,
                    reasoning_arg=reasoning_arg,
                    max_output_tokens=max_output_tokens,
                    request_payload=request_payload,
                    current_menu=normalized_menu,
                    previous_response_id=other_prev_id,
                    store_response=store_response,
                )
                if isinstance(other_recovery, dict) and other_recovery.get("used"):
                    oth_usage = other_recovery.get("usage")
                    if isinstance(oth_usage, dict):
                        usage = _combine_usage(model, [usage, oth_usage])
                    oth_debug = _normalize_text(other_recovery.get("debug_raw"))
                    if oth_debug:
                        combined_raw = f"{combined_raw}\n\n[PASS 4]\n{oth_debug}".strip()
                    oth_menu = other_recovery.get("menu")
                    if isinstance(oth_menu, dict):
                        normalized_menu = oth_menu
                    oth_resp_id = _normalize_text(other_recovery.get("response_id"))
                    if oth_resp_id:
                        final_response_id = oth_resp_id
                    oth_prompt = other_recovery.get("prompt_summary")
                    if isinstance(oth_prompt, dict):
                        passes_obj = prompt_meta.get("passes")
                        if isinstance(passes_obj, list):
                            passes_obj.append(oth_prompt)
                    oth_items_raw = other_recovery.get("added_items_raw")
                    if isinstance(oth_items_raw, list) and oth_items_raw:
                        pex = parsed_raw_out.get("page_extras")
                        if not isinstance(pex, dict):
                            pex = {}
                            parsed_raw_out["page_extras"] = pex
                        pex["other_text_recovery_added_items"] = oth_items_raw

                normalized_menu = _strip_internal_tracking_fields(normalized_menu)
                return {
                    "formatted": normalized_menu,
                    "openai_raw": p2_raw,
                    "openai_debug_raw": combined_raw,
                    "openai_parsed": normalized_menu,
                    "openai_parsed_raw": parsed_raw_out,
                    "source": "openai_two_pass",
                    "error": None,
                    "usage": usage,
                    "response_id": final_response_id,
                    "request_prompt": prompt_meta,
                }
            return {
                "formatted": deterministic,
                "openai_raw": p2_raw,
                "openai_debug_raw": combined_raw,
                "openai_parsed": None,
                "source": "deterministic",
                "error": "OpenAI pass2 response was not valid JSON",
                "usage": usage,
                "response_id": p2_response_id or p1_response_id,
                "request_prompt": prompt_meta,
            }

        if openai_input_mode == "raw_text_only":
            p2_system, p2_instruction, p2_user_text = _build_openai_raw_text_prompts(request_payload)
        else:
            p2_system, p2_instruction, p2_user_text = _build_openai_pass2_prompts(
                request_payload,
                {"dish_blocks": [], "other_text_line_indices": [], "footer_line_indices": [], "notes": []},
            )
        p2_raw, p2_parsed, p2_usage, p2_response_id = _run_openai_json_request(
            client=client,
            model=model,
            request_timeout=request_timeout,
            system_text=p2_system,
            user_text=p2_user_text,
            reasoning_arg=reasoning_arg,
            max_output_tokens=max_output_tokens,
            previous_response_id=previous_response_id,
            store_response=store_response,
            schema_mode="menu_schema",
        )
        prompt_meta = {
            "mode": "single_pass",
            "model": model,
            "openai_input_mode": openai_input_mode,
            "max_output_tokens": max_output_tokens,
            "reasoning": reasoning_arg,
            "previous_response_id": previous_response_id,
            "store_response": bool(store_response),
            "passes": [{"name": "menu_formatting", "system": p2_system, "user_instruction": p2_instruction}],
        }
        if isinstance(p2_parsed, dict):
            normalized_menu = _normalize_openai_menu(p2_parsed, fallback_page=fallback_page)
            if openai_input_mode != "raw_text_only":
                normalized_menu = _augment_menu_from_pass1(
                    normalized_menu,
                    request_payload,
                    {"dish_blocks": [], "other_text_line_indices": [], "footer_line_indices": [], "notes": []},
                )
            usage_single = p2_usage if isinstance(p2_usage, dict) else _empty_usage(model)
            parsed_raw_out = copy.deepcopy(p2_parsed)
            final_response_id = p2_response_id

            other_prev_id = p2_response_id if store_response else previous_response_id
            other_recovery = _recover_items_from_other_text(
                client=client,
                model=model,
                request_timeout=request_timeout,
                reasoning_arg=reasoning_arg,
                max_output_tokens=max_output_tokens,
                request_payload=request_payload,
                current_menu=normalized_menu,
                previous_response_id=other_prev_id,
                store_response=store_response,
            )
            if isinstance(other_recovery, dict) and other_recovery.get("used"):
                oth_usage = other_recovery.get("usage")
                if isinstance(oth_usage, dict):
                    usage_single = _combine_usage(model, [usage_single, oth_usage])
                oth_menu = other_recovery.get("menu")
                if isinstance(oth_menu, dict):
                    normalized_menu = oth_menu
                oth_resp_id = _normalize_text(other_recovery.get("response_id"))
                if oth_resp_id:
                    final_response_id = oth_resp_id
                oth_prompt = other_recovery.get("prompt_summary")
                if isinstance(oth_prompt, dict):
                    passes_obj = prompt_meta.get("passes")
                    if isinstance(passes_obj, list):
                        passes_obj.append(oth_prompt)
                oth_items_raw = other_recovery.get("added_items_raw")
                if isinstance(oth_items_raw, list) and oth_items_raw:
                    pex = parsed_raw_out.get("page_extras")
                    if not isinstance(pex, dict):
                        pex = {}
                        parsed_raw_out["page_extras"] = pex
                    pex["other_text_recovery_added_items"] = oth_items_raw

            normalized_menu = _strip_internal_tracking_fields(normalized_menu)
            return {
                "formatted": normalized_menu,
                "openai_raw": p2_raw,
                "openai_debug_raw": None,
                "openai_parsed": normalized_menu,
                "openai_parsed_raw": parsed_raw_out,
                "source": "openai",
                "error": None,
                "usage": usage_single,
                "response_id": final_response_id,
                "request_prompt": prompt_meta,
            }
        return {
            "formatted": deterministic,
            "openai_raw": p2_raw,
            "openai_debug_raw": None,
            "openai_parsed": None,
            "source": "deterministic",
            "error": "OpenAI response was not valid JSON",
            "usage": p2_usage,
            "response_id": p2_response_id,
            "request_prompt": prompt_meta,
        }
    except (APITimeoutError, APIConnectionError) as exc:
        timeout_like = isinstance(exc, APITimeoutError) or ("timeout" in _normalize_text(exc).lower())
        if timeout_like and _env_flag("OPENAI_TIMEOUT_FALLBACK_ENABLED", True):
            fallback_timeout = max(request_timeout, _to_int(os.getenv("OPENAI_TIMEOUT_FALLBACK_SEC"), 420))
            fallback_reasoning = {"effort": "minimal"} if model.lower().startswith("gpt-5") else reasoning_arg
            try:
                pass1_empty = {
                    "dish_blocks": [],
                    "other_text_line_indices": [],
                    "footer_line_indices": [],
                    "notes": [],
                }
                fb_system, fb_instruction, fb_user_text = _build_openai_pass2_prompts(request_payload, pass1_empty)
                fb_raw, fb_parsed, fb_usage, fb_response_id = _run_openai_json_request(
                    client=client,
                    model=model,
                    request_timeout=fallback_timeout,
                    system_text=fb_system,
                    user_text=fb_user_text,
                    reasoning_arg=fallback_reasoning,
                    max_output_tokens=max_output_tokens,
                    previous_response_id=None,
                    store_response=False,
                    schema_mode="menu_schema",
                )
                if isinstance(fb_parsed, dict):
                    normalized_menu = _normalize_openai_menu(fb_parsed, fallback_page=fallback_page)
                    normalized_menu = _augment_menu_from_pass1(normalized_menu, request_payload, pass1_empty)
                    normalized_menu = _strip_internal_tracking_fields(normalized_menu)
                    return {
                        "formatted": normalized_menu,
                        "openai_raw": fb_raw,
                        "openai_debug_raw": None,
                        "openai_parsed": normalized_menu,
                        "openai_parsed_raw": copy.deepcopy(fb_parsed),
                        "source": "openai_timeout_fallback",
                        "error": None,
                        "usage": fb_usage,
                        "response_id": fb_response_id,
                        "request_prompt": {
                            "mode": "timeout_fallback_same_payload",
                            "model": model,
                            "openai_input_mode": openai_input_mode,
                            "max_output_tokens": max_output_tokens,
                            "reasoning": fallback_reasoning,
                            "previous_response_id": None,
                            "store_response": False,
                            "passes": [{"name": "menu_formatting", "system": fb_system, "user_instruction": fb_instruction}],
                        },
                    }
            except Exception:
                pass

            if _env_flag("OPENAI_TIMEOUT_ALLOW_RAW_TEXT_FALLBACK", False):
                try:
                    raw_payload = _prepare_payload_for_openai(payload, mode="raw_text_only")
                    raw_system, raw_instruction, raw_user_text = _build_openai_raw_text_prompts(raw_payload)
                    raw_raw, raw_parsed, raw_usage, raw_response_id = _run_openai_json_request(
                        client=client,
                        model=model,
                        request_timeout=fallback_timeout,
                        system_text=raw_system,
                        user_text=raw_user_text,
                        reasoning_arg=fallback_reasoning,
                        max_output_tokens=max_output_tokens,
                        previous_response_id=None,
                        store_response=False,
                        schema_mode="menu_schema",
                    )
                    if isinstance(raw_parsed, dict):
                        normalized_menu = _normalize_openai_menu(raw_parsed, fallback_page=fallback_page)
                        normalized_menu = _strip_internal_tracking_fields(normalized_menu)
                        return {
                            "formatted": normalized_menu,
                            "openai_raw": raw_raw,
                            "openai_debug_raw": None,
                            "openai_parsed": normalized_menu,
                            "openai_parsed_raw": copy.deepcopy(raw_parsed),
                            "source": "openai_timeout_fallback_raw_text",
                            "error": None,
                            "usage": raw_usage,
                            "response_id": raw_response_id,
                            "request_prompt": {
                                "mode": "timeout_fallback_raw_text",
                                "model": model,
                                "openai_input_mode": "raw_text_only",
                                "max_output_tokens": max_output_tokens,
                                "reasoning": fallback_reasoning,
                                "previous_response_id": None,
                                "store_response": False,
                                "passes": [{"name": "menu_formatting", "system": raw_system, "user_instruction": raw_instruction}],
                            },
                        }
                except Exception:
                    pass

        return {
            "formatted": deterministic,
            "openai_raw": None,
            "openai_debug_raw": None,
            "openai_parsed": None,
            "source": "deterministic",
            "error": f"OpenAI request failed: {exc}",
            "usage": _empty_usage(model),
            "response_id": None,
            "request_prompt": {
                "mode": "two_pass" if use_two_pass else "single_pass",
                "model": model,
                "openai_input_mode": openai_input_mode,
                "max_output_tokens": max_output_tokens,
                "reasoning": reasoning_arg,
                "previous_response_id": previous_response_id,
                "store_response": bool(store_response),
            },
        }
    except Exception as exc:
        return {
            "formatted": deterministic,
            "openai_raw": None,
            "openai_debug_raw": None,
            "openai_parsed": None,
            "source": "deterministic",
            "error": str(exc),
            "usage": _empty_usage(model),
            "response_id": None,
            "request_prompt": {
                "mode": "two_pass" if use_two_pass else "single_pass",
                "model": model,
                "openai_input_mode": openai_input_mode,
                "max_output_tokens": max_output_tokens,
                "reasoning": reasoning_arg,
                "previous_response_id": previous_response_id,
                "store_response": bool(store_response),
            },
        }


def _merge_menu_outputs(base: Dict[str, Any], update: Dict[str, Any]) -> None:
    if not isinstance(update, dict):
        return
    if not _normalize_text(base.get("menu_name")) and _normalize_text(update.get("menu_name")):
        base["menu_name"] = _normalize_text(update.get("menu_name"))

    for item in (update.get("items") or []):
        if isinstance(item, dict):
            base.setdefault("items", []).append(item)

    for key in ("other_text", "footer_text", "notes"):
        vals = update.get(key) or []
        if not isinstance(vals, list):
            continue
        dst = base.setdefault(key, [])
        for v in vals:
            txt = _normalize_text(v)
            if txt and txt not in dst:
                dst.append(txt)

    update_extras = update.get("page_extras")
    if isinstance(update_extras, dict):
        base_extras = base.setdefault("page_extras", {})
        if isinstance(base_extras, dict):
            for key, val in update_extras.items():
                if key not in base_extras:
                    base_extras[key] = val
                    continue
                existing = base_extras.get(key)
                if existing == val:
                    continue
                if not isinstance(existing, list):
                    existing = [existing]
                if val not in existing:
                    existing.append(val)
                base_extras[key] = existing


def _strip_internal_tracking_fields(menu: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(menu, dict):
        return menu
    items = menu.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("bbox", "source_lines", "_bbox", "_source_lines"):
                if key in item:
                    item.pop(key, None)
    return menu


def _format_with_openai_simple_page_by_page(
    payload: Dict[str, Any],
    deterministic: Dict[str, Any],
    timeout_sec: int,
    out_dir: Path | None = None,
) -> Dict[str, Any]:
    pages = payload.get("pages", []) if isinstance(payload, dict) else []
    if not isinstance(pages, list) or not pages:
        return _format_with_openai_simple(payload, deterministic, timeout_sec)

    openai_input_mode = _openai_input_mode()
    if openai_input_mode == "raw_text_only":
        openai_input_mode = "raw_page_json"
    merged = _empty_menu(_normalize_text(deterministic.get("menu_name")) or None)
    openai_raw_parts: List[str] = []
    openai_debug_raw_parts: List[str] = []
    openai_parsed_raw_pages: List[Dict[str, Any]] = []
    errors: List[str] = []
    has_openai_success = False
    model_name = os.getenv("OPENAI_MODEL", "gpt-4.1")
    usage_total = _empty_usage(model_name)
    usage_pages: List[Dict[str, Any]] = []
    total_cost = 0.0
    has_cost = True
    chain_pages = _env_flag("OPENAI_CHAIN_PAGES", True)
    store_response = _env_flag("OPENAI_STORE_RESPONSES", chain_pages)
    chain_reset_every = _to_int(os.getenv("OPENAI_CHAIN_RESET_EVERY_PAGES"), 0)
    prev_response_id: str | None = None
    debug_dir: Path | None = None
    page_result_dir: Path | None = None
    if out_dir is not None:
        debug_dir = out_dir / "openai_page_payloads"
        debug_dir.mkdir(parents=True, exist_ok=True)
        page_result_dir = out_dir / "openai_page_results"
        page_result_dir.mkdir(parents=True, exist_ok=True)

    # Read page_XX_raw.json files directly from disk and send verbatim to OpenAI.
    raw_page_files: List[Path] = []
    if out_dir is not None:
        raw_page_files = sorted(out_dir.glob("page_*_raw.json"))

    if raw_page_files:
        # Use the actual page_XX_raw.json files from disk
        pages = []
        for rpf in raw_page_files:
            try:
                page_data = json.loads(rpf.read_text(encoding="utf-8"))
                if isinstance(page_data, dict):
                    pages.append(page_data)
            except Exception:
                continue
    # else: pages already set from payload above

    for idx, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            continue
        page_no_raw = page.get("page")
        try:
            page_no = int(page_no_raw) if page_no_raw is not None else idx
        except Exception:
            page_no = idx
        # Send the raw page JSON directly â€” no transformation
        page_payload = {
            "input_type": "ocr_document",
            "openai_input_mode": "raw_page_json",
            "pages": [page],
        }
        if not isinstance(page_payload.get("pages"), list) or not page_payload.get("pages"):
            continue

        if debug_dir is not None:
            (debug_dir / f"page_{page_no:02d}_payload.json").write_text(
                json.dumps(page_payload, indent=2),
                encoding="utf-8",
            )
            sent_page = page_payload.get("pages", [{}])[0] if isinstance(page_payload.get("pages"), list) else {}
            page_raw_text = _normalize_text(sent_page.get("raw_text")) if isinstance(sent_page, dict) else ""
            if page_raw_text:
                (debug_dir / f"page_{page_no:02d}_raw_text_sent_to_openai.txt").write_text(
                    str(sent_page.get("raw_text")),
                    encoding="utf-8",
                )

        page_deterministic = _empty_menu(_normalize_text(deterministic.get("menu_name")) or None)
        sent_previous_response_id = prev_response_id if chain_pages else None
        res = _format_with_openai_simple(
            page_payload,
            page_deterministic,
            timeout_sec,
            previous_response_id=sent_previous_response_id,
            store_response=store_response,
        )
        page_response_id = _normalize_text(res.get("response_id")) if isinstance(res, dict) else ""
        if chain_pages and page_response_id:
            prev_response_id = page_response_id
        if chain_pages and chain_reset_every > 0 and (idx % chain_reset_every == 0):
            prev_response_id = None
        page_usage = res.get("usage") if isinstance(res.get("usage"), dict) else _empty_usage(model_name)
        page_usage_obj = dict(page_usage)
        page_usage_obj["page"] = page_no
        usage_pages.append(page_usage_obj)
        usage_total["input_tokens"] = int(usage_total.get("input_tokens") or 0) + int(page_usage.get("input_tokens") or 0)
        usage_total["output_tokens"] = int(usage_total.get("output_tokens") or 0) + int(
            page_usage.get("output_tokens") or 0
        )
        usage_total["total_tokens"] = int(usage_total.get("total_tokens") or 0) + int(page_usage.get("total_tokens") or 0)
        p_cost = page_usage.get("estimated_cost_usd")
        if p_cost is None:
            has_cost = False
        else:
            total_cost += _to_float(p_cost, 0.0)

        if res.get("openai_raw"):
            page_raw_out = str(res["openai_raw"])
            openai_raw_parts.append(f"[PAGE {page_no}]\n{page_raw_out}")
            if debug_dir is not None:
                (debug_dir / f"page_{page_no:02d}_openai_raw.txt").write_text(
                    page_raw_out,
                    encoding="utf-8",
                )
        page_debug_raw = res.get("openai_debug_raw")
        if page_debug_raw:
            page_debug_out = str(page_debug_raw)
            openai_debug_raw_parts.append(f"[PAGE {page_no}]\n{page_debug_out}")
            if debug_dir is not None:
                (debug_dir / f"page_{page_no:02d}_openai_debug_raw.txt").write_text(
                    page_debug_out,
                    encoding="utf-8",
                )
        page_parsed_raw = res.get("openai_parsed_raw")
        if isinstance(page_parsed_raw, dict):
            openai_parsed_raw_pages.append({"page": page_no, "parsed": copy.deepcopy(page_parsed_raw)})
            if debug_dir is not None:
                (debug_dir / f"page_{page_no:02d}_openai_parsed_raw.json").write_text(
                    json.dumps(page_parsed_raw, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        prompt_obj = res.get("request_prompt")
        if debug_dir is not None and isinstance(prompt_obj, dict):
            (debug_dir / f"page_{page_no:02d}_prompt.json").write_text(
                json.dumps(prompt_obj, indent=2),
                encoding="utf-8",
            )
        if res.get("error"):
            errors.append(f"page {page_no}: {res.get('error')}")
            if debug_dir is not None:
                (debug_dir / f"page_{page_no:02d}_error.txt").write_text(
                    str(res.get("error")),
                    encoding="utf-8",
                )
        page_formatted = res.get("formatted")
        if isinstance(page_formatted, dict):
            # Backfill flags from deterministic items for this page
            det_items = deterministic.get("items") or []
            page_det_items = []
            for it in det_items:
                if not isinstance(it, dict):
                    continue
                if it.get("page") is None:
                    page_det_items.append(it)
                    continue
                try:
                    p = int(it.get("page"))
                except (ValueError, TypeError):
                    p = 0
                if p == page_no:
                    page_det_items.append(it)
            
            if page_formatted.get("items") and page_det_items:
                 _backfill_flags_from_deterministic(page_formatted["items"], page_det_items)

            _merge_menu_outputs(merged, page_formatted)
        if isinstance(res.get("openai_parsed"), dict):
            has_openai_success = True
            if debug_dir is not None:
                (debug_dir / f"page_{page_no:02d}_openai_parsed.json").write_text(
                    json.dumps(res["openai_parsed"], indent=2),
                    encoding="utf-8",
                )
        if page_result_dir is not None:
            page_result_obj = {
                "page": page_no,
                "source": res.get("source"),
                "error": res.get("error"),
                "usage": page_usage_obj,
                "previous_response_id_sent": sent_previous_response_id,
                "response_id": page_response_id or None,
                "openai_raw": res.get("openai_raw"),
                "openai_debug_raw": res.get("openai_debug_raw"),
                "openai_parsed": res.get("openai_parsed"),
                "openai_parsed_raw": res.get("openai_parsed_raw"),
                "prompt": prompt_obj,
            }
            (page_result_dir / f"page_{page_no:02d}.json").write_text(
                json.dumps(page_result_obj, indent=2),
                encoding="utf-8",
            )

    if not has_openai_success:
        usage_total["estimated_cost_usd"] = round(total_cost, 6) if has_cost else None
        usage_total["pages"] = usage_pages
        return {
            "formatted": deterministic,
            "openai_raw": "\n\n".join(openai_raw_parts).strip() if openai_raw_parts else None,
            "openai_debug_raw": "\n\n".join(openai_debug_raw_parts).strip() if openai_debug_raw_parts else None,
            "openai_parsed": None,
            "openai_parsed_raw": {"pages": openai_parsed_raw_pages} if openai_parsed_raw_pages else None,
            "source": "deterministic",
            "error": "; ".join(errors) if errors else "OpenAI page-by-page formatting failed",
            "usage": usage_total,
        }

    if not _normalize_text(merged.get("menu_name")) and _normalize_text(deterministic.get("menu_name")):
        merged["menu_name"] = _normalize_text(deterministic.get("menu_name"))
    merged = _strip_internal_tracking_fields(merged)
    usage_total["estimated_cost_usd"] = round(total_cost, 6) if has_cost else None
    usage_total["pages"] = usage_pages
    return {
        "formatted": merged,
        "openai_raw": "\n\n".join(openai_raw_parts).strip() if openai_raw_parts else None,
        "openai_debug_raw": "\n\n".join(openai_debug_raw_parts).strip() if openai_debug_raw_parts else None,
        "openai_parsed": merged,
        "openai_parsed_raw": {"pages": openai_parsed_raw_pages} if openai_parsed_raw_pages else None,
        "source": "openai_page_by_page",
        "error": "; ".join(errors) if errors else None,
        "usage": usage_total,
    }


def _backfill_flags_from_deterministic(formatted_items: List[Dict[str, Any]], det_items: List[Dict[str, Any]]) -> None:
    if not formatted_items or not det_items:
        return

    # Index deterministic items by normalized name
    det_map: Dict[str, Dict[str, Any]] = {}
    for it in det_items:
        nm = _normalize_text(it.get("name")).lower()
        if nm:
            det_map[nm] = it

    for item in formatted_items:
        nm = _normalize_text(item.get("name")).lower()
        if not nm:
            continue
        
        match = det_map.get(nm)
        # Fallback: try prefix matching if exact match fails
        if not match:
            # Try finding a deterministic item that contains this name or vice versa
            best_score = 0.0
            best_match = None
            for d_nm, d_item in det_map.items():
                if nm in d_nm or d_nm in nm:
                    # Simple length ratio score
                    score = min(len(nm), len(d_nm)) / max(len(nm), len(d_nm))
                    if score > best_score and score > 0.6:
                        best_score = score
                        best_match = d_item
            if best_match:
                match = best_match

        if match:
            # Backfill veg/non_veg if present in deterministic (which uses legacy icon logic)
            det_veg = _normalize_veg_flag(match.get("veg"))
            det_nonveg = _normalize_nonveg_flag(match.get("non_veg"))
            if det_veg and not _normalize_veg_flag(item.get("veg")):
                item["veg"] = det_veg
            if det_nonveg and not _normalize_nonveg_flag(item.get("non_veg")):
                item["non_veg"] = det_nonveg
            
            # Backfill allergens
            det_allergens = match.get("allergens")
            if isinstance(det_allergens, list) and det_allergens:
                curr = set(item.get("allergens") or [])
                curr.update(det_allergens)
                item["allergens"] = sorted(curr)


def run_unified_menu_pipeline(
    input_path: Path,
    output_root: Path,
    dpi: int = 350,
    use_openai: bool = True,
    cleanup: bool = True,
    menu_name_hint: str | None = None,
) -> Dict[str, Any]:
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    out_dir = create_output_dir(input_path, output_root)
    stored_input = out_dir / f"input{input_path.suffix.lower()}"
    stored_input.write_bytes(input_path.read_bytes())

    ext = stored_input.suffix.lower()
    processor = FullMenuOCR(config=FullMenuConfig(dpi=dpi))
    hint = _normalize_text(menu_name_hint or "")

    generated_pdf: Path | None = None
    openai_status: Dict[str, Any]
    formatted: Dict[str, Any]
    menu_raw_path: Path | None = None
    payload: Dict[str, Any]

    if ext == ".csv":
        raw_text, payload = _build_csv_payload(stored_input)
        source_meta = {"source": "csv", "input_type": "csv"}
        deterministic = _empty_menu(Path(hint).stem if hint else None)
        deterministic["other_text"] = [ln for ln in raw_text.splitlines() if ln and not ln.startswith("[")]
        (out_dir / "docai_text_raw.txt").write_text(raw_text, encoding="utf-8")
        (out_dir / "docai_text.txt").write_text(raw_text, encoding="utf-8")
        (out_dir / "docai_text_source.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")
        (out_dir / "ocr_payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _write_ocr_payload_pages(out_dir, payload)
        (out_dir / "docai_text_with_icons.json").write_text(
            json.dumps({"raw_text": raw_text, "payload": payload}, indent=2),
            encoding="utf-8",
        )
        (out_dir / "menu_raw.json").write_text(
            json.dumps({"pdf": None, "pages": payload.get("pages", [])}, indent=2),
            encoding="utf-8",
        )
        menu_raw_path = out_dir / "menu_raw.json"
    else:
        pdf_input = _ensure_pdf_input(stored_input)
        if pdf_input != stored_input:
            generated_pdf = pdf_input
        if pdf_input.name != "input.pdf":
            target_pdf = out_dir / "input.pdf"
            target_pdf.write_bytes(pdf_input.read_bytes())
            pdf_input = target_pdf

        raw_text, menu_raw, docai_agg = _process_pdf_with_docai_page_by_page(
            pdf_input=pdf_input,
            out_dir=out_dir,
            processor=processor,
        )
        (out_dir / "docai_raw.json").write_text(json.dumps(docai_agg, indent=2), encoding="utf-8")
        if not raw_text:
            raw_text = str(processor.pdf_raw_text(pdf_input) or "").strip()
        menu_raw_path = out_dir / "menu_raw.json"
        menu_raw_path.write_text(json.dumps(menu_raw, indent=2), encoding="utf-8")
        ocr_payload = _build_ocr_payload(menu_raw, raw_text, docai_agg=docai_agg)
        source_meta = {"source": "docai", "input_type": "ocr_document", "ocr_mode": "page_by_page"}
        deterministic = _empty_menu()
        if hint:
            deterministic["menu_name"] = Path(hint).stem

        (out_dir / "docai_text_raw.txt").write_text(raw_text, encoding="utf-8")
        (out_dir / "docai_text.txt").write_text(raw_text, encoding="utf-8")
        (out_dir / "docai_text_source.json").write_text(json.dumps(source_meta, indent=2), encoding="utf-8")
        (out_dir / "ocr_payload.json").write_text(json.dumps(ocr_payload, indent=2), encoding="utf-8")
        _write_ocr_payload_pages(out_dir, ocr_payload)
        (out_dir / "docai_text_with_icons.json").write_text(
            json.dumps({"raw_text": raw_text, "payload": ocr_payload}, indent=2),
            encoding="utf-8",
        )

        # Use menu_raw pages directly for OpenAI (same data as page_XX_raw.json).
        # _build_ocr_payload re-processes data and can lose icon details.
        payload = {
            "input_type": "ocr_document",
            "raw_text": raw_text,
            "pages": menu_raw.get("pages", []) if isinstance(menu_raw, dict) else [],
        }

    raw_openai_payload = _prepare_payload_for_openai(payload, mode="raw_text_only")
    raw_openai_chunks: List[str] = []
    raw_pages = raw_openai_payload.get("pages") if isinstance(raw_openai_payload, dict) else None
    if isinstance(raw_pages, list):
        for p in raw_pages:
            if not isinstance(p, dict):
                continue
            txt = str(p.get("raw_text") or "").strip()
            if txt:
                raw_openai_chunks.append(txt)
    (out_dir / "openai_input_raw_text.txt").write_text(
        "\n\n".join(raw_openai_chunks).strip(),
        encoding="utf-8",
    )

    if use_openai:
        requested_timeout = _to_int(os.getenv("OPENAI_REQUEST_TIMEOUT_SEC"), int(processor.config.openai_timeout))
        result = _format_with_openai_simple_page_by_page(
            payload=payload,
            deterministic=deterministic,
            timeout_sec=max(30, requested_timeout),
            out_dir=out_dir,
        )
        formatted = result.get("formatted") or deterministic
        usage_obj = result.get("usage") if isinstance(result.get("usage"), dict) else _empty_usage(os.getenv("OPENAI_MODEL", "gpt-4.1"))
        (out_dir / "openai_usage.json").write_text(json.dumps(usage_obj, indent=2), encoding="utf-8")
        openai_status = {"source": result.get("source"), "error": result.get("error"), "usage": _usage_summary(usage_obj)}
        wrote_openai_raw = False
        if result.get("openai_raw"):
            (out_dir / "openai_raw.txt").write_text(str(result["openai_raw"]), encoding="utf-8")
            wrote_openai_raw = True
        if result.get("openai_debug_raw"):
            (out_dir / "openai_debug_raw.txt").write_text(str(result["openai_debug_raw"]), encoding="utf-8")
        parsed_obj = result.get("openai_parsed")
        if isinstance(parsed_obj, dict):
            (out_dir / "openai_parsed.json").write_text(
                json.dumps(parsed_obj, indent=2),
                encoding="utf-8",
            )
        raw_parsed_obj = result.get("openai_parsed_raw")
        if isinstance(raw_parsed_obj, (dict, list)):
            (out_dir / "openai_parsed_raw.json").write_text(
                json.dumps(raw_parsed_obj, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if not wrote_openai_raw:
            (out_dir / "openai_raw.txt").write_text(json.dumps(formatted, indent=2), encoding="utf-8")
    else:
        formatted = deterministic
        usage_obj = _empty_usage(os.getenv("OPENAI_MODEL", "gpt-4.1"))
        (out_dir / "openai_usage.json").write_text(json.dumps(usage_obj, indent=2), encoding="utf-8")
        openai_status = {"source": "deterministic", "error": "OpenAI disabled", "usage": _usage_summary(usage_obj)}
        (out_dir / "openai_raw.txt").write_text(json.dumps(formatted, indent=2), encoding="utf-8")

    (out_dir / "menu_formatted.json").write_text(json.dumps(formatted, indent=2), encoding="utf-8")
    (out_dir / "openai_status.json").write_text(json.dumps(openai_status, indent=2), encoding="utf-8")
    copy_icon_details(out_dir)

    if cleanup:
        try:
            stored_input.unlink()
        except Exception:
            pass
        try:
            if generated_pdf and generated_pdf.exists():
                generated_pdf.unlink()
        except Exception:
            pass

    return {
        "output_dir": str(out_dir),
        "menu_raw": str(menu_raw_path) if menu_raw_path and menu_raw_path.exists() else None,
        "menu_formatted": str(out_dir / "menu_formatted.json"),
        "openai_usage": str(out_dir / "openai_usage.json"),
    }
