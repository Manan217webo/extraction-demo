# CRF Extraction Prototype

Throwaway prototype: upload a scanned clinical trial CRF PDF, extract fields with Gemini 2.5 Flash (one call per page), validate in Python, and review/edit side-by-side in a plain HTML UI.

No React, no database, no auth.

## Prerequisites

1. **Python 3.9+** (3.10+ preferred)
2. **Poppler** (required by `pdf2image`)
   - macOS: `brew install poppler`
   - Ubuntu/Debian: `sudo apt-get install poppler-utils`
3. A **Gemini API key** from Google AI Studio

## Setup

```bash
cd VLLNM
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set GEMINI_API_KEY=...
```

## Run

```bash
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Usage

1. Drop or choose a multi-page CRF PDF (prototype schema targets a 3-page Visit 4 form).
2. Wait while each page is converted at 300 DPI and sent to Gemini separately.
3. Review the page image (left) and editable fields (right).
4. Fix values as needed, then **Export JSON**.
5. **Re-run extraction** to call the model again on the same PDF.

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/extract` | Multipart PDF → `{ job_id }` |
| `GET` | `/api/jobs/{job_id}` | Job status, fields, validations |
| `GET` | `/api/jobs/{job_id}/image/{page_no}` | PNG for a page |
| `POST` | `/api/jobs/{job_id}/export` | Edited fields → final JSON |
| `POST` | `/api/jobs/{job_id}/rerun` | Re-run extraction for the job |

Jobs are stored **in memory** only — restarting the server clears them.

## Project layout

```
app.py          FastAPI routes + static mount
schema.py       Per-page field definitions + VLM system prompt
extract.py      PDF → PNG + Gemini calls
validate.py     Post-merge Python rules
static/         index.html, styles.css, app.js
```

## Notes

- Model: `gemini-2.5-flash`, `temperature=0`, JSON structured output.
- Validation (tablet balance, header consistency, vitals ranges, etc.) runs in Python after merge — not in the model prompt.
- This is a prototype: expect imperfect OCR on poor scans; prefer `UNCLEAR` over guessing.
