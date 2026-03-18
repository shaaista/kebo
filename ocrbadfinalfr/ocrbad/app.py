from __future__ import annotations

import json
import os
from pathlib import Path

import html
import re
import shutil
import subprocess
import time
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

import fitz  # type: ignore

from unified_menu_pipeline import run_unified_menu_pipeline

app = FastAPI()

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "output"
SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_TEXT_INPUT_EXTS = {".csv"}
SUPPORTED_DOC_EXTS = {
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".ppt",
    ".pptx",
    ".odp",
    ".xls",
    ".xlsx",
    ".ods",
}
SUPPORTED_UPLOAD_EXTS = {".pdf", *SUPPORTED_IMAGE_EXTS, *SUPPORTED_DOC_EXTS, *SUPPORTED_TEXT_INPUT_EXTS}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _html_page(body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Menu OCR</title>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap');
      :root {{
        --bg: #f1f6ff;
        --panel: #ffffff;
        --ink: #1f2a37;
        --muted: #5b6b7f;
        --accent: #9bbcff;
        --accent-dark: #7ea4f5;
        --border: #dbe7ff;
        --shadow: 0 10px 24px rgba(35, 60, 120, 0.08);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        font-family: "Inter", sans-serif;
        background: var(--bg);
        color: var(--ink);
        padding: 32px 18px 48px;
        margin: 0;
      }}
      .wrap {{
        max-width: 860px;
        margin: 0 auto;
      }}
      .page {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 22px;
        box-shadow: var(--shadow);
      }}
      h1 {{
        margin: 0 0 8px 0;
        font-size: 24px;
      }}
      h2, h3 {{
        margin: 0 0 8px 0;
      }}
      p {{ color: var(--muted); }}
      .card {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px;
        margin-top: 14px;
        box-shadow: var(--shadow);
      }}
      .row {{
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
      }}
      .btn {{
        display: inline-block;
        padding: 9px 14px;
        border-radius: 10px;
        border: 1px solid var(--accent);
        background: var(--accent);
        color: #0f1f3a;
        text-decoration: none;
        font-weight: 600;
        transition: 0.15s ease;
      }}
      .btn:disabled {{
        opacity: 0.6;
        cursor: not-allowed;
      }}
      .btn:hover {{ background: var(--accent-dark); border-color: var(--accent-dark); }}
      .btn.secondary {{
        background: transparent;
        color: var(--ink);
        border-color: var(--border);
      }}
      .dropzone {{
        border: 2px dashed var(--border);
        border-radius: 12px;
        padding: 22px 16px;
        background: #f8fbff;
        text-align: center;
      }}
      input[type=file] {{
        display: block;
        margin: 10px auto 0;
      }}
      .pill {{
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid var(--border);
        background: #f4f8ff;
        font-size: 12px;
        color: var(--muted);
      }}
      .label {{
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
        margin-top: 10px;
      }}
      .item {{
        background: #fff;
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px 14px;
      }}
      .item h4 {{
        margin: 0 0 6px 0;
        font-size: 16px;
      }}
      .muted {{
        color: var(--muted);
        font-size: 13px;
      }}
      .meta {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 8px;
      }}
      .section-title {{
        margin-top: 8px;
        font-weight: 600;
      }}
      .small {{
        font-size: 12px;
        color: var(--muted);
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="page">
        {body}
      </div>
    </div>
    <script>
      const fileInput = document.querySelector('input[type="file"]');
      const submitBtn = document.querySelector('button[type="submit"]');
      if (fileInput && submitBtn) {{
        submitBtn.disabled = true;
        submitBtn.textContent = 'Select File';
        fileInput.addEventListener('change', () => {{
          const hasFile = fileInput.files && fileInput.files.length > 0;
          submitBtn.disabled = !hasFile;
          submitBtn.textContent = hasFile ? 'Apply OCR' : 'Select File';
        }});
      }}
    </script>
  </body>
</html>"""
    )


def _parse_json_maybe(text: str):
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
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
        return json.loads(cleaned)
    except Exception:
        return None


def _to_number_maybe(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"\d{1,6}(?:[.,]\d{1,2})?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None


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


def _convert_office_to_pdf(doc_path: Path) -> Path:
    soffice = _find_soffice_executable()
    if not soffice:
        raise RuntimeError(
            "Document conversion requires LibreOffice (soffice). Install LibreOffice or upload PDF/image."
        )
    out_dir = doc_path.parent
    before = {p.resolve() for p in out_dir.glob("*.pdf")}
    cmd = [
        soffice,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(doc_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    expected = doc_path.with_suffix(".pdf")
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


def _ensure_pdf_input(upload_path: Path) -> Path:
    ext = upload_path.suffix.lower()
    if ext == ".pdf":
        return upload_path
    if ext in SUPPORTED_IMAGE_EXTS:
        return _convert_image_to_pdf(upload_path)
    if ext in SUPPORTED_DOC_EXTS:
        return _convert_office_to_pdf(upload_path)
    raise ValueError(f"Unsupported file type: {ext or '(no extension)'}")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    body = """
    <h1>Upload your menu</h1>
    <p>Upload a menu file and generate OCR payload JSON files for manual page-by-page review.</p>
    <div class="card">
      <form action="/process" method="post" enctype="multipart/form-data">
        <div class="dropzone">
          <div class="label">Drag & drop your file here</div>
          <div class="small">or click below to browse</div>
          <input type="file" name="file" accept=".pdf,image/*,.csv,.doc,.docx,.odt,.rtf,.ppt,.pptx,.odp,.xls,.xlsx,.ods" required />
          <div style="margin-top:10px;">
            <button class="btn" type="submit">Select File</button>
          </div>
        </div>
      </form>
    </div>
    """
    return _html_page(body)


@app.post("/process", response_class=HTMLResponse)
async def process(file: UploadFile = File(...)) -> HTMLResponse:
    original_name = Path(file.filename or "upload").name
    ext = Path(original_name).suffix.lower()
    if ext not in SUPPORTED_UPLOAD_EXTS:
        return _html_page(
            "<p>Unsupported file type. Please upload PDF, image (PNG/JPG/JPEG/BMP/TIFF/WEBP), "
            "CSV/Excel (CSV/XLS/XLSX), or document (DOC/DOCX/ODT/RTF/PPT/PPTX/ODP/ODS).</p>"
        )

    tmp_dir = OUTPUT_ROOT / "_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    tmp_path = tmp_dir / f"{ts}_{original_name}"
    content = await file.read()
    tmp_path.write_bytes(content)

    start = time.perf_counter()
    # User requested to force OpenAI enabled in code
    use_openai = True  # _env_flag("APP_USE_OPENAI", False)
    try:
        result = run_unified_menu_pipeline(
            tmp_path,
            OUTPUT_ROOT,
            dpi=350,
            use_openai=use_openai,
            cleanup=True,
            menu_name_hint=original_name,
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"\n*** PIPELINE ERROR: {exc} ***\n")
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return _html_page(f"<p>Could not process uploaded file: {html.escape(str(exc))}</p>")
    elapsed = time.perf_counter() - start
    try:
        tmp_path.unlink()
    except Exception:
        pass
    out_dir = result.get("output_dir")
    out_name = Path(out_dir).name if out_dir else ""
    process_meta = f"Processed in {elapsed:.1f}s"
    mode_note = (
        "OpenAI page-by-page formatting enabled (raw page JSON sent one-by-one)."
        if use_openai
        else "No Gemini/OpenAI request was sent. Payload files are stored for manual review."
    )
    filename = html.escape(original_name)
    output_rel = html.escape(f"output/{out_name}")
    page_payload_rel = html.escape(f"output/{out_name}/ocr_payload_pages")
    body = f"""
    <h1>Done</h1>
    <p class="small">{html.escape(process_meta)}</p>
    <p>{html.escape(mode_note)}</p>
    <div class="card">
      <div class="row">
        <span class="pill">{filename}</span>
        <a class="btn" href="/download/{out_name}/ocr_payload.json">Download OCR Payload</a>
        <a class="btn secondary" href="/">Home</a>
      </div>
      <p class="small" style="margin-top:10px;">Output folder: <code>{output_rel}</code></p>
      <p class="small">Page-by-page payload folder: <code>{page_payload_rel}</code></p>
    </div>
    """
    return _html_page(body)


@app.get("/download/{job}/{filename}")
def download(job: str, filename: str) -> FileResponse:
    path = OUTPUT_ROOT / job / filename
    return FileResponse(path)


def _render_human_formatted(text: str) -> str:
    try:
        data = json.loads(text)
    except Exception:
        return f"<p class='muted'>Could not parse formatted output. Raw response:</p><pre>{html.escape(text)}</pre>"

    if not isinstance(data, dict):
        return f"<pre>{html.escape(text)}</pre>"

    if data.get("error"):
        return f"<p class='muted'>OpenAI error:</p><pre>{html.escape(str(data.get('error')))}</pre>"

    menu_name = html.escape(str(data.get("menu_name") or "Menu"))
    items = data.get("items") or []
    other_text = data.get("other_text") or []
    footer_text = data.get("footer_text") or []
    notes = data.get("notes") or []

    parts = [f"<h2>{menu_name}</h2>"]
    if items:
        parts.append("<div class='section-title'>Menu Items</div>")
        parts.append("<div class='grid'>")
        for item in items:
            if not isinstance(item, dict):
                continue
            name = html.escape(str(item.get("name") or ""))
            price = html.escape(str(item.get("price") or ""))
            kcal = html.escape(str(item.get("kcal") or ""))
            desc = html.escape(str(item.get("description") or ""))
            allergens = item.get("allergens") or []
            veg = bool(item.get("veg"))
            non_veg = bool(item.get("non_veg"))
            flags = []
            if veg:
                flags.append("Veg")
            if non_veg:
                flags.append("Non-veg")
            allergens_text = ", ".join(str(a) for a in allergens) if allergens else "None"
            parts.append(
                "<div class='item'>"
                f"<h4>{name}</h4>"
                f"<div class='muted'>{desc}</div>"
                "<div class='meta'>"
                f"<span class='pill'>Price: {price or '--'}</span>"
                f"<span class='pill'>Kcal: {kcal or '--'}</span>"
                f"<span class='pill'>Allergens: {html.escape(allergens_text)}</span>"
                f"<span class='pill'>{' / '.join(flags) if flags else '--'}</span>"
                "</div>"
                "</div>"
            )
        parts.append("</div>")
    else:
        parts.append("<p class='muted'>No menu items detected.</p>")

    def render_text_block(title: str, content) -> str:
        if not content:
            return ""
        if isinstance(content, list):
            lines = "<br>".join(html.escape(str(c)) for c in content if c)
        else:
            lines = html.escape(str(content))
        return f"<div class='section-title'>{title}</div><div class='muted'>{lines}</div>"

    parts.append(render_text_block("Other Text", other_text))
    parts.append(render_text_block("Footer Text", footer_text))
    parts.append(render_text_block("Notes", notes))

    return "".join(parts)
