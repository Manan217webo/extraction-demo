# extraction-demo

Read a handwritten CRF and fill the matching Cronos EDC visit.

Upload a scanned CRF as a PDF; the app reads the page, looks the subject's visit
up in Cronos EDC, fills that visit's fields with what it read, and — after a
reviewer checks each value against the page beside it — writes the values back
with a cropped image of the handwriting attached to each one.

## Setup

Requires [uv](https://docs.astral.sh/uv/). It fetches its own Python 3.12, so
nothing else needs installing first.

```
# Windows
winget install --id=astral-sh.uv -e

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, in the project folder:

```
uv sync                     # creates .venv from uv.lock
copy .env.example .env      # Windows;  cp .env.example .env  elsewhere
```

Fill in `LLAMA_CLOUD_API_KEY` and `OPENAI_API_KEY`, and point `EDC_BASE_URL` at
the EDC deployment this instance should talk to. See `.env.example` for the
full list of settings and what each one does.

## Check the machine is ready

```
uv run scripts/doctor.py
```

Every check runs through the application's own modules, so a pass means the code
paths the app uses work here — not that a parallel reimplementation agrees with
itself. It reports the configured EDC, reads the demo visit, prints the shape of
each CRF it finds, and warns about anything that would degrade a demo (fixtures
left on, a control the EDC sent with no option list, box placement unavailable).
Exits non-zero on a hard failure, so it can gate a deploy step.

To check a different visit:

```
uv run scripts/doctor.py "ICR/24/001" 03013 "Visit 5"
```

## Run it

```
uv run uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Then open <http://127.0.0.1:8000>. Drop `--reload` for anything but development.

## Working offline

Set `EDC_FIXTURES=edc_fixtures/visits.json` and the whole visit flow runs off a
local file, saves included — they write back into that file. It is opt-in rather
than a fallback, so it can never quietly stand in for a real EDC that happens to
be down.

**Unset it before a demo.** With it on, "Save to EDC" writes to a text file and
reports success, which is indistinguishable from a real save unless you go and
look in the EDC. `scripts/doctor.py` warns when it is set.

## Dependencies

`pyproject.toml` and `uv.lock` are the source of truth. `requirements.txt` is
generated from the lock and committed so the Railway build works whether or not
it detects uv — regenerate it after any dependency change:

```
uv export --no-hashes --no-emit-project --output-file requirements.txt
```

## Layout

| File | Does |
| --- | --- |
| `app.py` | HTTP routes and the job lifecycle |
| `edc.py` | Cronos EDC connector — the three `/api/EDC/*` endpoints |
| `visit_forms.py` | Turns an EDC visit into a renderable form, and back into a save payload |
| `cronos.py` | Committed CRF definitions and the older form-picker connector |
| `fields.py` | Asks the model for values against a CRF's field vocabulary |
| `mapping.py` | Coerces values to their declared types and assembles the reviewed form |
| `anchors.py` | Ties a value back to a rectangle on the original page |
| `vision.py` | Refines those rectangles (OpenAI vision, or local Tesseract) |
| `pdf_export.py`, `docx_export.py` | Downloadable copies of a reviewed CRF |
| `static/` | The browser app — no build step, no framework |
