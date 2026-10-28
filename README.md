# extraction-demo

Read a handwritten CRF and fill the matching Cronos EDC visit.

Upload a scanned CRF as a PDF; the app reads the page, looks the subject's visit
up in Cronos EDC, fills that visit's fields with what it read, and — after a
reviewer checks each value against the page beside it — writes the values back
with a cropped image of the handwriting attached to each one.

Reviewers sign in. Job state lives in Redis so several workers and a restart
keep mid-review visits intact. Each browser keeps multiple visits per user.

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
cp .env.example .env        # Windows: copy .env.example .env
docker compose up -d redis  # login sessions and job meta
uv run scripts/add_user.py alice
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

With `APP_ENV=production`, fixtures, the Cronos dev host, missing Redis/users,
and a missing vision backend are hard failures.

To check a different visit:

```
uv run scripts/doctor.py "ICR/24/001" 03013 "Visit 5"
```

## Run it (development)

```
docker compose up -d redis
uv run uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Then open <http://127.0.0.1:8000> and sign in. Drop `--reload` for anything
but development.

## Deploy on a customer server

1. Copy the project onto the host and create `.env` from `.env.example`.
2. Set at least:
   - `APP_ENV=production`
   - `LLAMA_CLOUD_API_KEY`, `OPENAI_API_KEY`
   - `EDC_BASE_URL` to **their** EDC (never the Cronos dev host)
   - `OPENAI_VISION_MODEL` **or** rely on Tesseract in the image
   - Leave `EDC_FIXTURES` unset
3. Create reviewers (writes `users.json` — do not commit it):

   ```
   uv run scripts/add_user.py alice
   uv run scripts/add_user.py bob
   ```

4. Start:

   ```
   docker compose up -d --build
   ```

   The compose file runs the app (two uvicorn workers by default) and Redis
   with AOF persistence. Optional HTTPS with Caddy:

   ```
   SITE_ADDRESS=extraction.example.com docker compose --profile tls up -d
   ```

5. Gate go-live:

   ```
   docker compose exec app uv run scripts/doctor.py "<protocol>" <screening> "<Visit N>"
   ```

6. Point reviewers at the host. They sign in, upload, and work different visits
   in parallel. A process restart keeps Redis job meta; "Recent reviews" in the
   browser switches between visits for the signed-in user.

Railway: add a Redis plugin, set `REDIS_URL`, `APP_ENV=production`, and the
keys above. `railway.toml` health-checks `/healthz` and starts with workers.

## Working offline

Set `EDC_FIXTURES=edc_fixtures/visits.json` and the whole visit flow runs off a
local file, saves included — they write back into that file. It is opt-in rather
than a fallback, so it can never quietly stand in for a real EDC that happens to
be down.

**Unset it before production.** With it on, "Save to EDC" writes to a text file
and reports success, which is indistinguishable from a real save unless you go
and look in the EDC. `scripts/doctor.py` fails the check when
`APP_ENV=production`.

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
| `app.py` | HTTP routes, auth middleware, job lifecycle |
| `auth.py` | Reviewer passwords and Redis sessions |
| `store.py` | Redis job meta, parse cache, locks, save audit |
| `edc.py` | Cronos EDC connector — the three `/api/EDC/*` endpoints |
| `visit_forms.py` | Turns an EDC visit into a renderable form, and back into a save payload |
| `cronos.py` | Committed CRF definitions and the older form-picker connector |
| `fields.py` | Asks the model for values against a CRF's field vocabulary |
| `mapping.py` | Coerces values to their declared types and assembles the reviewed form |
| `anchors.py` | Ties a value back to a rectangle on the original page |
| `vision.py` | Refines those rectangles (OpenAI vision, or local Tesseract) |
| `pdf_export.py`, `docx_export.py` | Downloadable copies of a reviewed CRF |
| `static/` | The browser app — no build step, no framework |
| `scripts/add_user.py` | Create reviewer accounts |
| `scripts/doctor.py` | Preflight / deploy gate |
| `Dockerfile`, `docker-compose.yml` | Customer deploy (app + Redis, optional Caddy) |
