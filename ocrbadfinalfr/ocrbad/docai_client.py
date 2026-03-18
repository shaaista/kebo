from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from PIL import Image

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


@dataclass
class DocAIConfig:
    project_number: str
    location: str
    processor_id: str
    processor_version: Optional[str]
    api_endpoint: str
    credentials_path: str
    enable_native_pdf_parsing: bool = True  # Kept for compatibility, though REST logic might just auto-handle
    low_quality_threshold: float = 1.11


def load_docai_config() -> DocAIConfig:
    load_dotenv()

    project_number = os.getenv("DOC_AI_PROJECT_NUMBER", "").strip() or os.getenv("GCP_PROJECT_ID", "").strip()
    location = os.getenv("DOC_AI_LOCATION", "").strip() or os.getenv("GCP_LOCATION", "").strip()
    processor_id = os.getenv("DOC_AI_PROCESSOR_ID", "").strip() or os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
    processor_version = os.getenv("DOC_AI_PROCESSOR_VERSION", "").strip() or os.getenv("DOCUMENT_AI_PROCESSOR_VERSION_ID", "").strip() or None
    api_endpoint = os.getenv("DOC_AI_API_ENDPOINT", "").strip() or os.getenv("DOCUMENT_AI_API_ENDPOINT", "").strip() or "us-documentai.googleapis.com"
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    if not project_number:
        # Fallback to older env var names if new ones not present
        project_number = os.getenv("GCP_PROJECT_ID", "")

    if not location:
         location = os.getenv("GCP_LOCATION", "")

    if not processor_id and not project_number:
         # Try parsing from full resource path if available
         res = os.getenv("DOCUMENT_AI_PROCESSOR_RESOURCE", "")
         if res:
             parts = res.split("/")
             if len(parts) >= 6:
                 project_number = parts[1]
                 location = parts[3]
                 processor_id = parts[5]

    if not cred_path:
        # Try finding a JSON key file in current directory if not set? 
        # For now, raise if strictly missing as per original logic, 
        # but user might have set it in session.
        pass

    # Resolve relative credentials path
    cred_path_abs = str(Path(cred_path).resolve()) if cred_path else ""

    if location.lower() == "us":
        api_endpoint = "us-documentai.googleapis.com"
    elif location and location not in api_endpoint:
        api_endpoint = f"{location}-documentai.googleapis.com"

    return DocAIConfig(
        project_number=project_number,
        location=location,
        processor_id=processor_id,
        processor_version=processor_version,
        api_endpoint=api_endpoint,
        credentials_path=cred_path_abs,
    )


def processor_resource_name(cfg: DocAIConfig) -> str:
    base = f"projects/{cfg.project_number}/locations/{cfg.location}/processors/{cfg.processor_id}"
    if cfg.processor_version:
        return f"{base}/processorVersions/{cfg.processor_version}"
    return base


def get_bearer_token(cfg: DocAIConfig) -> str:
    if not cfg.credentials_path or not os.path.exists(cfg.credentials_path):
         # Try default credentials if path not specific
         import google.auth
         creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
         creds.refresh(Request())
         return creds.token
         
    creds = service_account.Credentials.from_service_account_file(
        cfg.credentials_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds.refresh(Request())
    return creds.token


def resolve_processor_version(cfg: DocAIConfig, token: str) -> str:
    alias = (cfg.processor_version or "").strip().lower()
    base = f"projects/{cfg.project_number}/locations/{cfg.location}/processors/{cfg.processor_id}"

    if not alias:
        return base

    _VERSION_ALIAS_TAGS = {"rc", "stable"}
    if alias not in _VERSION_ALIAS_TAGS:
        return f"{base}/processorVersions/{cfg.processor_version}"

    list_url = f"https://{cfg.api_endpoint}/v1/{base}/processorVersions"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-goog-user-project": cfg.project_number,
    }
    try:
        resp = requests.get(list_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            versions = resp.json().get("processorVersions", [])
            for v in versions:
                aliases = [a.lower() for a in v.get("googleManaged", {}).get("aliases", [])]
                aliases += [a.lower() for a in v.get("aliases", [])]
                if alias in aliases:
                    return v.get("name", "")
            print(f"Warning: Alias '{alias}' not found. Falling back to base processor.")
            return base
        else:
            print(f"Warning: Failed to list versions (HTTP {resp.status_code}). Base processor.")
            return base
    except Exception as e:
        print(f"Warning: Error resolving version alias: {e}")
        return base


def docai_process_raw(cfg: DocAIConfig, content_bytes: bytes, mime_type: str) -> Dict[str, Any]:
    token = get_bearer_token(cfg)
    name = resolve_processor_version(cfg, token)
    url = f"https://{cfg.api_endpoint}/v1/{name}:process"

    # Debug: show exact URL being called
    print(f"**REST URL:** `{url}`")
    print(f"**Resolved processor name:** `{name}`")
    print(f"**MIME type sent:** `{mime_type}` | **Payload size:** {len(content_bytes)} bytes")

    process_options = {}
    # Check env var. Default to True if not set (matches previous behavior), 
    # but we want False to force OCR if user set it to false.
    enable_native = os.getenv("DOCUMENT_AI_ENABLE_NATIVE_PDF_PARSING", "true").lower() == "true"
    
    # Only relevant for PDF
    if mime_type == "application/pdf":
        process_options = {
            "ocrConfig": {
                "enableNativePdfParsing": enable_native
            }
        }

    payload = {
        "rawDocument": {
            "content": base64.b64encode(content_bytes).decode("utf-8"),
            "mimeType": mime_type,
        }
    }
    if process_options:
        payload["processOptions"] = process_options

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-goog-user-project": cfg.project_number,
    }

    _max_attempts = 3
    _retry_statuses = {429, 500, 503}
    resp = None
    for _attempt in range(_max_attempts):
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=300)
        if resp.status_code == 200:
            break
        if resp.status_code in _retry_statuses and _attempt < _max_attempts - 1:
            _wait = 5 * (_attempt + 1)
            print(f"WARNING: DocAI returned {resp.status_code} (attempt {_attempt + 1}/{_max_attempts}), retrying in {_wait}s...")
            import time as _time
            _time.sleep(_wait)
            continue
        raise RuntimeError(f"DocAI error {resp.status_code}: {resp.text}")
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"DocAI error: no successful response after {_max_attempts} attempts")

    result = resp.json()
    
    # Debug: show what keys are in the response and document structure
    doc = result.get("document", {}) or {}
    layout = doc.get("documentLayout", {}) or {}
    top_blocks = layout.get("blocks", []) or []
    print(
        f"**Response top-level keys:** {list(result.keys())}  \n"
        f"**document keys:** {list(doc.keys()) if doc else '(empty)'}  \n"
        f"**document.pages count:** {len(doc.get('pages', []) or [])}  \n"
        f"**document.text length:** {len(doc.get('text', '') or '')}  \n"
        f"**document.mimeType:** {doc.get('mimeType', '(missing)')} \n"
        f"**documentLayout keys:** {list(layout.keys())} \n"
        f"**documentLayout.blocks count:** {len(top_blocks)}"
    )
    
    # Debug: dump first block structure to see if layout.boundingPoly exists
    if top_blocks:
        first_block = top_blocks[0]
        print(f"\n**FIRST TOP-LEVEL BLOCK STRUCTURE:**")
        print(json.dumps(first_block, indent=2, default=str)[:2000])
    
    # Debug: save raw response (minus base64 content) to output dir for inspection
    try:
        debug_response = json.loads(json.dumps(result, default=str))
        # Remove base64 content to keep file small
        if "rawDocument" in debug_response:
            debug_response["rawDocument"]["content"] = "<BASE64_REMOVED>"
        if "document" in debug_response and "rawDocument" in debug_response.get("document", {}):
            debug_response["document"]["rawDocument"]["content"] = "<BASE64_REMOVED>"
        # Write to output dir
        from pathlib import Path as _DebugPath
        debug_out = _DebugPath("output") / "_docai_raw_response_debug.json"
        debug_out.parent.mkdir(parents=True, exist_ok=True)
        debug_out.write_text(json.dumps(debug_response, indent=2, default=str), encoding="utf-8")
        print(f"**DEBUG: Raw response saved to {debug_out}**")
    except Exception as debug_err:
        print(f"**DEBUG: Failed to save raw response: {debug_err}**")

    return result


# ----------------------------
# Input Normalization & Chunking
# ----------------------------

SUPPORTED_MIMES = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
TEXT_EXTS = {".txt", ".csv"}


def sniff_extension(filename: str) -> str:
    _, ext = os.path.splitext(filename.lower())
    return ext


def images_to_pdf_bytes(img_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PDF")
    return out.getvalue()


def text_to_pdf_bytes(text: str, title: str = "Document") -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    left = 36
    top = height - 36
    line_h = 12
    y = top
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, title)
    y -= 2 * line_h
    c.setFont("Helvetica", 10)
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        while len(line) > 110:
            c.drawString(left, y, line[:110])
            line = line[110:]
            y -= line_h
            if y < 36:
                c.showPage()
                y = top
                c.setFont("Helvetica", 10)
        c.drawString(left, y, line)
        y -= line_h
        if y < 36:
            c.showPage()
            y = top
            c.setFont("Helvetica", 10)
    c.save()
    return out.getvalue()


def normalize_for_docai(filename: str, file_bytes: bytes) -> Tuple[str, str, bytes]:
    """Returns (docai_filename, mime_type, docai_bytes)"""
    ext = sniff_extension(filename)
    if ext in SUPPORTED_MIMES:
        return filename, SUPPORTED_MIMES[ext], file_bytes
    if ext in IMAGE_EXTS:
        return os.path.splitext(filename)[0] + ".pdf", "application/pdf", images_to_pdf_bytes(file_bytes)
    if ext in TEXT_EXTS:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")
        return os.path.splitext(filename)[0] + ".pdf", "application/pdf", text_to_pdf_bytes(text, title=filename)
    
    # Fallback to PDF if unknown, assuming it might work or let DocAI fail
    return filename, "application/pdf", file_bytes


def split_pdf_into_chunks(pdf_bytes: bytes, max_pages_per_chunk: int = 15) -> List[bytes]:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return [pdf_bytes]
        total = len(reader.pages)
        if total <= max_pages_per_chunk:
            return [pdf_bytes]
        chunks = []
        for start in range(0, total, max_pages_per_chunk):
            writer = PdfWriter()
            end = min(start + max_pages_per_chunk, total)
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            out = io.BytesIO()
            writer.write(out)
            chunks.append(out.getvalue())
        return chunks
    except Exception:
        # If PDF parsing fails (encryption etc), return simple bytes as one chunk
        return [pdf_bytes]


# ----------------------------
# Result Parsing
# ----------------------------

def norm_poly_to_bbox_abs(norm_vertices: List[Dict[str, float]], w: float, h: float) -> List[float]:
    xs = [v.get("x", 0.0) * w for v in norm_vertices]
    ys = [v.get("y", 0.0) * h for v in norm_vertices]
    if not xs or not ys:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(xs), min(ys), max(xs), max(ys)]


def extract_text_anchor(doc: Dict[str, Any], anchor: Dict[str, Any]) -> str:
    full_text = doc.get("text", "") or ""
    pieces = []
    for seg in anchor.get("textSegments", []) or []:
        start = int(seg.get("startIndex", 0) or 0)
        end = int(seg.get("endIndex", 0) or 0)
        if 0 <= start < end <= len(full_text):
            pieces.append(full_text[start:end])
    return "".join(pieces).strip()


def assign_columns(bboxes: List[List[float]], page_width: float) -> Tuple[List[int], List[Tuple[float, float]]]:
    if not bboxes:
        return [], []
    centers = sorted([( (b[0]+b[2])/2.0, idx) for idx, b in enumerate(bboxes)])
    xs = [c[0] for c in centers]
    gaps = [(xs[i+1]-xs[i], i) for i in range(len(xs)-1)]
    if not gaps:
        return [0]*len(bboxes), [(0.0, page_width)]
    
    max_gap, split_i = max(gaps, key=lambda t: t[0])
    if max_gap > 0.18 * page_width:
        left_ids = set(idx for _, idx in centers[:split_i+1])
        col_idx = []
        for i in range(len(bboxes)):
            col_idx.append(0 if i in left_ids else 1)
        
        left_x0 = min((bboxes[i][0] for i in range(len(bboxes)) if col_idx[i] == 0), default=0.0)
        left_x1 = max((bboxes[i][2] for i in range(len(bboxes)) if col_idx[i] == 0), default=page_width/2)
        right_x0 = min((bboxes[i][0] for i in range(len(bboxes)) if col_idx[i] == 1), default=page_width/2)
        right_x1 = max((bboxes[i][2] for i in range(len(bboxes)) if col_idx[i] == 1), default=page_width)
        return col_idx, [(left_x0, left_x1), (right_x0, right_x1)]
    
    return [0]*len(bboxes), [(0.0, page_width)]


def _collect_layout_texts(
    blocks, 
    page_texts: Dict[int, List[Dict[str, Any]]],
    parent_bbox: List[Dict[str, Any]] | None = None
):
    """
    Recursively walk Document AI blocks and collect text entries
    (headings, paragraphs, etc.) grouped by 1-indexed page number.
    
    Args:
        blocks: List of block objects from Document AI.
        page_texts: Dict to collect text entries into.
        parent_bbox: Optional list of normalized vertices from the parent block,
                     used as a fallback if the current block lacks geometry.
    """
    for block in (blocks or []):
        page_span = block.get("pageSpan", {}) or {}
        page_start = int(page_span.get("pageStart", 0) or 0)
        page_end = int(page_span.get("pageEnd", page_start) or page_start)
        
        # Extract normalized vertices if present
        # layout -> boundingPoly -> normalizedVertices
        layout = block.get("layout", {}) or {}
        poly = (layout.get("boundingPoly", {}) or {}).get("normalizedVertices", []) or []
        
        # If no poly for this block, inherit from parent (common for sub-blocks)
        effective_poly = poly if poly else parent_bbox

        if "textBlock" in block:
            tb = block["textBlock"]
            text = (tb.get("text", "") or "").strip()
            children = tb.get("blocks", []) or []
            block_type = (tb.get("type", "") or "").lower()

            # Collect this block's own text (headings, titles, paragraphs)
            if text:
                for pg in range(page_start, page_end + 1):
                    pnum = pg  # Store raw index (will normalize later)
                    page_texts.setdefault(pnum, []).append({
                        "text": text,
                        "type": block_type,
                        "block_id": block.get("blockId", ""),
                        "bbox_normalized": effective_poly or [],
                    })

            # Also recurse into children for nested content
            if children:
                _collect_layout_texts(children, page_texts, parent_bbox=effective_poly)

        elif "tableBlock" in block:
            tb = block["tableBlock"]
            for row_key in ("headerRows", "bodyRows"):
                for row in (tb.get(row_key, []) or []):
                    for cell in (row.get("cells", []) or []):
                        # Cells often have their own bbox, but if not, use table's? 
                        # Ideally cells should have their own. We pass effective_poly just in case.
                        cell_layout = cell.get("layout", {}) or {}
                        cell_poly = (cell_layout.get("boundingPoly", {}) or {}).get("normalizedVertices", [])
                        # For cells, we prefer cell_poly, else fallback to table's effective_poly
                        pass_down_poly = cell_poly if cell_poly else effective_poly
                        
                        _collect_layout_texts(cell.get("blocks", []) or [], page_texts, parent_bbox=pass_down_poly)

        elif "listBlock" in block:
            lb = block["listBlock"]
            for entry in (lb.get("listEntries", []) or []):
                # List entries sometimes have geometry, sometimes not.
                # Inspect entry structure if needed, but usually just recurse to blocks.
                _collect_layout_texts(entry.get("blocks", []) or [], page_texts, parent_bbox=effective_poly)


def docai_layout_to_page_json(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse the newer documentLayout format returned by Layout Parser RC / Gemini.
    Returns the same structure as the traditional pages-based parser so the
    rest of the app (merge, UI) works unchanged.
    """
    layout = doc.get("documentLayout", {}) or {}
    top_blocks = layout.get("blocks", []) or []

    page_texts_raw: Dict[int, List[Dict[str, Any]]] = {}
    _collect_layout_texts(top_blocks, page_texts_raw)

    # 2026-02-16 IMPROVED FIX: Dynamic Page Normalization
    # Some DocAI models use 0-based indexing (0, 1, 2...)
    # Others use 1-based indexing (1, 2, 3...)
    # Instead of hardcoded shift, we normalize the lowest found index to 1.
    if page_texts_raw:
        min_pg = min(page_texts_raw.keys())
        # We want min_pg to become 1.
        # So offset = 1 - min_pg.
        # e.g. if min=0 -> offset=1 -> 0 becomes 1
        # e.g. if min=1 -> offset=0 -> 1 becomes 1
        offset = 1 - min_pg
        
        page_texts = {}
        for raw_pg, entries in page_texts_raw.items():
            page_texts[raw_pg + offset] = entries
            
        if offset != 0:
            print(f"DEBUG: Normalized page indices by offset {offset} (Raw min: {min_pg}).")
    else:
        page_texts = {}

    all_text_parts: List[str] = []
    out_pages: List[Dict[str, Any]] = []

    for page_num in sorted(page_texts.keys()):
        entries = page_texts[page_num]
        page_text = "\n".join(e["text"] for e in entries)
        all_text_parts.append(page_text)

        line_rows = []
        for entry in entries:
            # If we don't have page dimensions, we can't calculate absolute bbox easily.
            # But we can at least provide the normalized one.
            # And for 'bbox' (absolute), we can just use normalized values (0..1) 
            # if we have no width/height, effectively assuming 1x1 page.
            poly = entry.get("bbox_normalized", [])
            bbox = [0.0, 0.0, 0.0, 0.0]
            if poly:
                xs = [v.get("x", 0.0) for v in poly]
                ys = [v.get("y", 0.0) for v in poly]
                if xs and ys:
                   bbox = [min(xs), min(ys), max(xs), max(ys)]

            line_rows.append({
                "text": entry["text"],
                "bbox": bbox,
                "bbox_normalized": poly,
                "layout_role": entry.get("type", "line") or "line",
                "column_index": 0,
                "column_bbox": [0.0, 0.0],
            })

        out_pages.append({
            "page": page_num,
            "page_dimension": {"width": 1, "height": 1, "unit": "NORMALIZED"},
            "text_length_total": len(page_text),
            "lines": line_rows,
            "blocks": line_rows,
            "raw_page": {"documentLayout_entries": entries},
        })

    full_text = "\n".join(t for t in all_text_parts if t.strip())

    return {
        "doc": {
            "mime_type": doc.get("mimeType"),
            "text_length": len(full_text),
            "page_count": len(out_pages),
        },
        "pages": out_pages,
        "text": full_text,
    }


def docai_to_page_json(docai_resp: Dict[str, Any]) -> Dict[str, Any]:
    doc = docai_resp.get("document", {}) or {}
    pages = doc.get("pages", []) or []
    full_text = doc.get("text", "") or ""

    # ---- Handle documentLayout format (Layout Parser) ----
    # Reverting to appparser.py logic: only use Layout if pages key is missing/empty.
    if not pages and "documentLayout" in doc:
        return docai_layout_to_page_json(doc)
    full_text = doc.get("text", "") or ""
    out_pages = []

    for p_i, page in enumerate(pages, start=1):
        dim = page.get("dimension", {}) or {}
        w = float(dim.get("width", 1.0) or 1.0)
        h = float(dim.get("height", 1.0) or 1.0)
        
        lines = page.get("lines", []) or []
        line_rows = []
        bboxes = []
        for ln in lines:
            layout = ln.get("layout", {}) or {}
            anchor = layout.get("textAnchor", {}) or {}
            text = extract_text_anchor(doc, anchor)
            poly = (layout.get("boundingPoly", {}) or {}).get("normalizedVertices", []) or []
            bbox = norm_poly_to_bbox_abs(poly, w, h) if poly else [0.0, 0.0, 0.0, 0.0]
            bboxes.append(bbox)
            line_rows.append({
                "text": text,
                "bbox": bbox,
                "bbox_normalized": poly,
                "layout_role": "line",
            })
        
        col_idx, col_ranges = assign_columns(bboxes, w)
        for i, row in enumerate(line_rows):
            row["column_index"] = int(col_idx[i]) if i < len(col_idx) else 0
            row["column_bbox"] = list(col_ranges[row["column_index"]]) if col_ranges else [0.0, w]
        
        # Blocks (optional)
        blocks = page.get("blocks", []) or []
        out_blocks = []
        for b in blocks:
            layout = b.get("layout", {}) or {}
            anchor = layout.get("textAnchor", {}) or {}
            text = extract_text_anchor(doc, anchor)
            poly = (layout.get("boundingPoly", {}) or {}).get("normalizedVertices", []) or []
            bbox = norm_poly_to_bbox_abs(poly, w, h) if poly else [0.0, 0.0, 0.0, 0.0]
            out_blocks.append({"text": text, "bbox": bbox, "bbox_normalized": poly})
            
        out_pages.append({
            "page": p_i,
            "page_dimension": {"width": w, "height": h, "unit": dim.get("unit", "UNIT_UNSPECIFIED")},
            "text_length_total": len(full_text),
            "lines": line_rows,
            "blocks": out_blocks,
            "raw_page": page,
        })
        
    return {
        "doc": {
            "mime_type": doc.get("mimeType"),
            "text_length": len(full_text),
            "page_count": len(pages),
        },
        "pages": out_pages,
        "text": full_text,
    }


def merge_chunked_results(page_json_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged_pages = []
    merged_text_parts = []
    page_offset = 0

    for item in page_json_list:
        merged_text_parts.append(item.get("text", "") or "")
        for p in item.get("pages", []):
            p2 = dict(p)
            p2["page"] = int(p2["page"]) + page_offset
            merged_pages.append(p2)
        page_offset += len(item.get("pages", []))

    merged_text = "\n".join([t for t in merged_text_parts if t.strip()])
    return {
        "doc": {
            "mime_type": page_json_list[0].get("doc", {}).get("mime_type") if page_json_list else None,
            "text_length": len(merged_text),
            "page_count": len(merged_pages),
        },
        "pages": merged_pages,
        "text": merged_text,
    }


def _load_ocr_config() -> Optional[DocAIConfig]:
    """
    Load config for the secondary OCR processor (DOCUMENT_AI_PROCESSOR_ID)
    which returns document.pages with real bounding boxes.
    Returns None if not configured separately from the Layout Parser.
    """
    load_dotenv()
    ocr_processor_id = os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
    layout_processor_id = os.getenv("DOC_AI_PROCESSOR_ID", "").strip()
    
    # Only useful if there IS a separate OCR processor
    if not ocr_processor_id or ocr_processor_id == layout_processor_id:
        return None
    
    # Try to get the OCR processor's location from its resource path
    ocr_resource = os.getenv("DOCUMENT_AI_PROCESSOR_RESOURCE", "").strip()
    ocr_location = ""
    ocr_project = ""
    if ocr_resource:
        parts = ocr_resource.split("/")
        if len(parts) >= 6:
            ocr_project = parts[1]
            ocr_location = parts[3]
    
    if not ocr_project:
        ocr_project = os.getenv("DOC_AI_PROJECT_NUMBER", "").strip() or os.getenv("GCP_PROJECT_ID", "").strip()
    if not ocr_location:
        ocr_location = "asia-south1"  # Fallback from .env
    
    api_endpoint = os.getenv("DOCUMENT_AI_API_ENDPOINT", "").strip()
    if not api_endpoint:
        api_endpoint = f"{ocr_location}-documentai.googleapis.com"
    
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    cred_path_abs = str(Path(cred_path).resolve()) if cred_path else ""
    
    return DocAIConfig(
        project_number=ocr_project,
        location=ocr_location,
        processor_id=ocr_processor_id,
        processor_version=None,
        api_endpoint=api_endpoint,
        credentials_path=cred_path_abs,
    )


def _extract_ocr_line_bboxes(ocr_resp: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """
    From a traditional DocAI OCR response (document.pages), extract line-level
    bounding boxes grouped by 1-indexed page number.
    Returns: {page_num: [{text, bbox: [x0,y0,x1,y1], poly: [...]}]}
    """
    doc = ocr_resp.get("document", {}) or {}
    pages = doc.get("pages", []) or []
    full_text = doc.get("text", "") or ""
    result: Dict[int, List[Dict[str, Any]]] = {}
    
    for p_i, page in enumerate(pages, start=1):
        dim = page.get("dimension", {}) or {}
        w = float(dim.get("width", 1.0) or 1.0)
        h = float(dim.get("height", 1.0) or 1.0)
        
        lines_out = []
        for ln in (page.get("lines", []) or []):
            layout = ln.get("layout", {}) or {}
            anchor = layout.get("textAnchor", {}) or {}
            text = extract_text_anchor(doc, anchor).strip()
            if not text:
                continue
            poly = (layout.get("boundingPoly", {}) or {}).get("normalizedVertices", []) or []
            bbox = norm_poly_to_bbox_abs(poly, w, h) if poly else [0.0, 0.0, 0.0, 0.0]
            lines_out.append({
                "text": text,
                "bbox": bbox,
                "poly": poly,
                "page_width": w,
                "page_height": h,
            })
        result[p_i] = lines_out
    return result


def _enrich_layout_with_ocr_coords(
    layout_result: Dict[str, Any],
    ocr_line_bboxes: Dict[int, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    Merge OCR bounding boxes into Layout Parser results.
    Uses word-overlap (Jaccard) scoring for robust matching,
    multi-line bbox merging, and positional interpolation for unmatched lines.
    """
    import re

    def _words(text: str) -> set:
        """Extract lowercase word set for Jaccard comparison."""
        return set(re.findall(r'[a-z0-9]+', text.lower()))

    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _merge_bboxes(bboxes: list) -> list:
        """Merge multiple bboxes into one enclosing bbox."""
        if not bboxes:
            return [0.0, 0.0, 0.0, 0.0]
        x0 = min(b[0] for b in bboxes)
        y0 = min(b[1] for b in bboxes)
        x1 = max(b[2] for b in bboxes)
        y1 = max(b[3] for b in bboxes)
        return [x0, y0, x1, y1]

    def _merge_polys(polys: list) -> list:
        """Merge multiple normalized polys into one enclosing poly."""
        if not polys:
            return []
        all_x = []
        all_y = []
        for poly in polys:
            for v in poly:
                all_x.append(v.get("x", 0.0))
                all_y.append(v.get("y", 0.0))
        if not all_x:
            return []
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        return [
            {"x": min_x, "y": min_y},
            {"x": max_x, "y": min_y},
            {"x": max_x, "y": max_y},
            {"x": min_x, "y": max_y},
        ]

    for page in layout_result.get("pages", []):
        page_num = int(page.get("page", 1))
        ocr_lines = ocr_line_bboxes.get(page_num, [])

        if not ocr_lines:
            continue

        # Update page dimensions from OCR (real pixel dimensions)
        first = ocr_lines[0]
        pw = first.get("page_width", 1)
        ph = first.get("page_height", 1)
        if pw > 1 or ph > 1:
            page["page_dimension"] = {"width": pw, "height": ph, "unit": "POINT"}
            page["width"] = pw
            page["height"] = ph

        # Preserve dense OCR line rows for downstream icon detection.
        # Layout Parser may merge multiple text lines into a single block/line.
        ocr_line_rows: List[Dict[str, Any]] = []
        ocr_boxes: List[List[float]] = []
        for ol in ocr_lines:
            text = str(ol.get("text") or "").strip()
            bbox = ol.get("bbox")
            poly = ol.get("poly")
            if not text or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            try:
                x0, y0, x1, y1 = [float(v) for v in bbox]
            except Exception:
                continue
            if x1 <= x0 or y1 <= y0:
                continue
            row = {
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "bbox_normalized": poly if isinstance(poly, list) else [],
                "layout_role": "line",
            }
            ocr_line_rows.append(row)
            ocr_boxes.append([x0, y0, x1, y1])

        if ocr_line_rows:
            col_idx, col_ranges = assign_columns(ocr_boxes, float(pw or 1.0))
            for i, row in enumerate(ocr_line_rows):
                col = int(col_idx[i]) if i < len(col_idx) else 0
                row["column_index"] = col
                if col_ranges and 0 <= col < len(col_ranges):
                    row["column_bbox"] = list(col_ranges[col])
                else:
                    row["column_bbox"] = [0.0, float(pw or 0.0)]
            page["ocr_lines"] = ocr_line_rows

        # Pre-compute word sets for each OCR line
        ocr_word_sets = [_words(ol["text"]) for ol in ocr_lines]
        used_ocr: set = set()
        layout_lines = page.get("lines", [])
        matched_indices: set = set()  # Layout line indices that got matched

        for li, line in enumerate(layout_lines):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            layout_words = _words(text)
            if not layout_words:
                continue

            # 1) Exact text match (case-insensitive)
            exact_key = text.lower()
            exact_match = None
            for oi, ol in enumerate(ocr_lines):
                if oi in used_ocr:
                    continue
                if ol["text"].strip().lower() == exact_key:
                    exact_match = (oi, ol)
                    break
            if exact_match:
                oi, ol = exact_match
                used_ocr.add(oi)
                line["bbox"] = ol["bbox"]
                line["bbox_normalized"] = ol["poly"]
                matched_indices.add(li)
                continue

            # 2) Word Jaccard matching — find ALL OCR lines with significant overlap
            #    then merge their bboxes (handles cases where Layout groups text
            #    differently from OCR, e.g. dish name spans 2 OCR lines)
            scored = []
            for oi, ol in enumerate(ocr_lines):
                if oi in used_ocr:
                    continue
                score = _jaccard(layout_words, ocr_word_sets[oi])
                if score >= 0.4:
                    scored.append((score, oi, ol))

            if scored:
                scored.sort(key=lambda x: -x[0])
                # Take the best match; if it's very high, also grab any
                # secondary matches that share many words (multi-line merging)
                best_score = scored[0][0]
                to_merge = []
                for score, oi, ol in scored:
                    if score >= 0.4 and (score >= best_score * 0.7):
                        to_merge.append((oi, ol))
                    else:
                        break  # Already sorted desc

                bboxes = [ol["bbox"] for _, ol in to_merge if ol["bbox"] != [0.0, 0.0, 0.0, 0.0]]
                polys = [ol["poly"] for _, ol in to_merge if ol["poly"]]

                if bboxes:
                    for oi, _ in to_merge:
                        used_ocr.add(oi)
                    line["bbox"] = _merge_bboxes(bboxes)
                    line["bbox_normalized"] = _merge_polys(polys)
                    matched_indices.add(li)
                    continue

        # 3) Positional interpolation for unmatched lines
        #    For lines with [0,0,0,0] bbox, estimate position from nearest
        #    matched neighbors above and below
        for li, line in enumerate(layout_lines):
            if li in matched_indices:
                continue
            bbox = line.get("bbox", [0, 0, 0, 0])
            if bbox != [0.0, 0.0, 0.0, 0.0] and bbox != [0, 0, 0, 0]:
                continue

            # Find nearest matched line above
            above_bbox = None
            for ai in range(li - 1, -1, -1):
                if ai in matched_indices:
                    ab = layout_lines[ai].get("bbox", [0, 0, 0, 0])
                    if ab != [0.0, 0.0, 0.0, 0.0] and ab != [0, 0, 0, 0]:
                        above_bbox = ab
                        break

            # Find nearest matched line below
            below_bbox = None
            for bi in range(li + 1, len(layout_lines)):
                if bi in matched_indices:
                    bb = layout_lines[bi].get("bbox", [0, 0, 0, 0])
                    if bb != [0.0, 0.0, 0.0, 0.0] and bb != [0, 0, 0, 0]:
                        below_bbox = bb
                        break

            if above_bbox and below_bbox:
                # Interpolate: place between above and below
                y_top = above_bbox[3]  # bottom of above
                y_bottom = below_bbox[1]  # top of below
                line["bbox"] = [above_bbox[0], y_top, above_bbox[2], y_bottom]
                # Build normalized poly
                if pw > 1 and ph > 1:
                    line["bbox_normalized"] = [
                        {"x": above_bbox[0] / pw, "y": y_top / ph},
                        {"x": above_bbox[2] / pw, "y": y_top / ph},
                        {"x": above_bbox[2] / pw, "y": y_bottom / ph},
                        {"x": above_bbox[0] / pw, "y": y_bottom / ph},
                    ]
            elif above_bbox:
                # Place just below the above line
                row_h = above_bbox[3] - above_bbox[1]
                y_top = above_bbox[3] + 2
                y_bottom = y_top + row_h
                line["bbox"] = [above_bbox[0], y_top, above_bbox[2], y_bottom]
                if pw > 1 and ph > 1:
                    line["bbox_normalized"] = [
                        {"x": above_bbox[0] / pw, "y": y_top / ph},
                        {"x": above_bbox[2] / pw, "y": y_top / ph},
                        {"x": above_bbox[2] / pw, "y": y_bottom / ph},
                        {"x": above_bbox[0] / pw, "y": y_bottom / ph},
                    ]
            elif below_bbox:
                # Place just above the below line
                row_h = below_bbox[3] - below_bbox[1]
                y_bottom = below_bbox[1] - 2
                y_top = y_bottom - row_h
                line["bbox"] = [below_bbox[0], y_top, below_bbox[2], y_bottom]
                if pw > 1 and ph > 1:
                    line["bbox_normalized"] = [
                        {"x": below_bbox[0] / pw, "y": y_top / ph},
                        {"x": below_bbox[2] / pw, "y": y_top / ph},
                        {"x": below_bbox[2] / pw, "y": y_bottom / ph},
                        {"x": below_bbox[0] / pw, "y": y_bottom / ph},
                    ]

        # Sync blocks with lines
        page["blocks"] = list(layout_lines)

    return layout_result


def process_pdf_with_docai(pdf_path: Path, config: DocAIConfig | None = None) -> Dict[str, Any]:
    """
    Main entry point: Read PDF -> normalize -> chunk -> process -> merge -> return Standard JSON.
    
    Uses dual-processor approach:
    1. Layout Parser (DOC_AI_PROCESSOR_ID) for text + semantic structure
    2. OCR Processor (DOCUMENT_AI_PROCESSOR_ID) for bounding box coordinates
    """
    cfg = config or load_docai_config()
    pdf_bytes = Path(pdf_path).read_bytes()
    filename, mime, docai_bytes = normalize_for_docai(Path(pdf_path).name, pdf_bytes)
    
    chunks = [docai_bytes]
    if mime == "application/pdf":
         chunks = split_pdf_into_chunks(docai_bytes)
    
    # Step 1: Layout Parser for text + structure
    results = []
    for chunk in chunks:
        raw_resp = docai_process_raw(cfg, chunk, mime)
        page_json = docai_to_page_json(raw_resp)
        results.append(page_json)
    
    layout_result = merge_chunked_results(results)
    
    # Step 2: OCR Processor for coordinates (if configured separately)
    ocr_cfg = _load_ocr_config()
    if ocr_cfg:
        try:
            print("DEBUG: Making supplementary OCR call for bounding box coordinates...")
            ocr_chunks = [docai_bytes]
            if mime == "application/pdf":
                ocr_chunks = split_pdf_into_chunks(docai_bytes)
            
            ocr_results: Dict[int, List[Dict[str, Any]]] = {}
            page_offset = 0
            for chunk in ocr_chunks:
                ocr_resp = docai_process_raw(ocr_cfg, chunk, mime)
                chunk_bboxes = _extract_ocr_line_bboxes(ocr_resp)
                # Offset page numbers for multi-chunk
                for pg, lines in chunk_bboxes.items():
                    ocr_results[pg + page_offset] = lines
                page_offset += len(chunk_bboxes)
            
            layout_result = _enrich_layout_with_ocr_coords(layout_result, ocr_results)
            print(f"DEBUG: OCR enrichment complete. {sum(len(v) for v in ocr_results.values())} OCR lines matched.")
        except Exception as e:
            print(f"WARNING: OCR coordinate enrichment failed (non-fatal): {e}")
            # Continue with Layout Parser results even without coordinates
    
    return layout_result
