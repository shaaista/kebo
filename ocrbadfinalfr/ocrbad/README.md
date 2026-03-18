# Menu OCR Project

## What This Project Does
This service takes a menu file upload, runs OCR + layout parsing + icon detection, then formats output into structured menu JSON.

The app entrypoint is:

`uvicorn app:app --reload`

## Runtime Files Kept
- `app.py` (FastAPI UI + upload endpoint)
- `unified_menu_pipeline.py` (main pipeline orchestration)
- `docai_client.py` (Google Document AI calls and preprocessing)
- `full_menu_ocr.py` (OCR line/icon processing helpers)
- `legend_extractor.py` (legend/icon extraction)
- `menu_pipeline.py` (shared output helpers used by unified pipeline)
- `config.py` (legend/icon label config)
- `requirements.txt`
- `.env` and `service_account.json` (credentials/config)

## System Requirements
- Python 3.11+ (3.13 is also fine if your installed wheels support it)
- LibreOffice installed (`soffice`) for DOC/DOCX/XLS/XLSX/PPT/etc to PDF conversion
- Tesseract OCR installed (recommended for robust icon/legend fallback)

## Python Dependencies
Install all required packages:

```bash
pip install -r requirements.txt
```

## Environment Variables
Set these in `.env` (or shell env):

Required:
- `OPENAI_API_KEY`
- `DOC_AI_PROJECT_NUMBER` (or `GCP_PROJECT_ID`)
- `DOC_AI_PROCESSOR_ID` (layout parser processor)
- `GOOGLE_APPLICATION_CREDENTIALS` (path to service account JSON)

Common optional:
- `DOCUMENT_AI_PROCESSOR_ID` (secondary OCR processor id)
- `DOC_AI_PROCESSOR_VERSION` or `DOCUMENT_AI_PROCESSOR_VERSION_ID`
- `OPENAI_MODEL` (current default in your setup is `gpt-5`)
- `OPENAI_TWO_PASS` (`1`/`0`)
- `OPENAI_INPUT_MODE` (recommended: `raw_page_json`)

## How To Run
1. Open terminal in project root.
2. Ensure `.env` is configured and service account file exists.
3. Start server:

```bash
uvicorn app:app --reload
```

4. Open browser:
- `http://127.0.0.1:8000`

5. Upload a menu file.

## Supported Upload Types
- PDF
- Images: PNG/JPG/JPEG/BMP/TIFF/WEBP
- CSV
- Office docs: DOC/DOCX/ODT/RTF/PPT/PPTX/ODP/XLS/XLSX/ODS (converted via LibreOffice)

## Output
Each run writes a new folder under `output/` with:
- OCR payloads (`ocr_payload.json`, per-page raw files)
- OpenAI raw/parsed outputs
- Final structured output: `menu_formatted.json`

## Pipeline Flow (Simple)
1. Normalize input to PDF if needed.
2. Run Document AI page-by-page (layout + OCR context).
3. Extract legend icons/templates and attach icon signals to lines.
4. Build page payloads and send to OpenAI (page-by-page).
5. Normalize + recover missed items (including `other_text` re-check).
6. Save final structured menu JSON.

## Pipeline Flow (Technical)
1. `app.py` saves upload to `output/_uploads`.
2. `run_unified_menu_pipeline()` in `unified_menu_pipeline.py` orchestrates:
- input conversion (`_ensure_pdf_input`)
- docai extraction (`_process_pdf_with_docai_page_by_page`)
- icon template handling (`LegendExtractor`, `FullMenuOCR`)
- OpenAI formatting (`_format_with_openai_simple_page_by_page`)
3. OpenAI stage uses structured prompts with:
- heading/column hierarchy
- price assignment rules
- omission recovery pass
- `other_text` recovery pass
4. Final output persisted to `menu_formatted.json`.

## Notes
- If DOC/DOCX/XLS/XLSX conversion fails, check LibreOffice installation/path.
- If Google calls fail, verify credentials and processor IDs.
- If uploads fail in FastAPI forms, ensure `python-multipart` is installed.
