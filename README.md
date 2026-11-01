# Webo eSource

An eSource layer on top of Cronos EDC. It reads scanned paper CRFs (from a file
server or an upload), proposes every value with two independent AI reads, and
lets a reviewer verify each value against the page, save drafts, and submit —
with an electronic signature — only the values that differ from what the EDC
already holds. Everything is versioned and written to an append-only,
hash-chained audit trail that is also delivered to the EDC.

## What it does

| Area | Behaviour |
| --- | --- |
| **Sources** | Admin-configured file-server folders (`sources.json`; SMB/NFS shares mounted on the host). Reviewers browse folders, pick files or whole folders, and scan them. Paths can't leave a configured folder. |
| **Duplicates** | Before a scan, every file is hashed (SHA-256). Files already processed — at that path or anywhere else — are listed and the reviewer chooses, per file, whether to reprocess; reprocessing needs a reason. Declines are audited. |
| **Versions** | A document is a path (or, for uploads, a content hash). Changed bytes at the same path add a version; originals are stored write-once by hash and re-checked on read. Each processing run, draft revision and submission is kept. |
| **Batches** | Scans run server-side in a worker (render → header → EDC visit → two reads), with progress, cancel and retry. Nothing is ever sent to the EDC automatically. Unreadable headers or unknown visits stop at *needs input*. |
| **Two reads** | The bigger model is the primary read (it places the boxes and its value stands on a conflict); the other read's value is shown with one-click *Use this*. Set `OPENAI_PREFERRED_READ` when names don't show which is bigger. |
| **Review** | Every edit needs a reason (quick-pick); drafts autosave as server revisions and never touch the EDC. |
| **Submit** | Pre-flight reads the EDC fresh and shows exactly what will change; unchanged values are not sent (and nothing is sent if nothing changed). Signing re-asks the password and the meaning of the signature (21 CFR 11). Afterwards the visit is read back to confirm the values landed. |
| **Audit** | Every action — sign-ins, scans, AI proposals with model and confidence, edits with old/new/reason, signatures, submissions, exports — is one event, written in the same transaction as the change, chained by HMAC-SHA-256, and refused UPDATE/DELETE by the database. Filter, verify and export (CSV/JSON) from the Audit screen. |
| **To the EDC** | Audit events are queued in an outbox and posted to `EDC_AUDIT_URL` in order (CDISC ODM AuditRecord field names, idempotency keys, exponential back-off). Unset, they are held locally and released when it is set. |
| **Accounts** | Reviewer / monitor (read-only) / admin. Lockout after 5 failures, 12-character passwords, 15-minute idle timeout with a warning, accounts disabled not deleted. |

The API contract is in `docs/API.md`.

## Before go-live (outside the code)

- **Cronos audit endpoint.** The EDC API documents no endpoint for receiving
  audit records. Agree one with Cronos and set `EDC_AUDIT_URL`; until then the
  trail is complete locally and held for delivery.
- **Partial saves.** Values are now sent only when they differ from the EDC.
  Confirm on a test visit that `SaveVisitCRFData` leaves omitted fields alone.
- **Per-user EDC identity.** Saves still use one `EDC_API_KEY`; the acting
  user travels in the audit trail. Ask Cronos whether the API accepts an
  on-behalf-of user.
- **Validation and SOPs.** IQ/OQ/PQ, the FDA e-signature certification letter,
  backup/restore tests of the database and `DATA_DIR`, custody of
  `AUDIT_HMAC_KEY`, and NTP on every host.

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
cp sources.example.json sources.json   # file-server folders to scan from
uv run scripts/add_user.py alice --full-name "Alice Ng" --role admin
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
3. Set `POSTGRES_PASSWORD` and `AUDIT_HMAC_KEY` (a long random string; keep a
   copy outside the server). Mount the file server's share on the host and set
   `SOURCE_SHARE` to it; list the folders reviewers may scan in `sources.json`
   (paths under `/sources`).

4. Start, then create the first admin:

   ```
   docker compose up -d --build
   docker compose exec app uv run scripts/add_user.py alice --full-name "Alice Ng" --role admin
   ```

   The compose file runs the app (two uvicorn workers), a worker for batch
   scans and audit delivery, PostgreSQL (the system of record — back up
   `pg_data` and `originals`), and Redis with AOF persistence. Optional HTTPS with Caddy:

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
| `app.py` | HTTP routes for the review steps, auth middleware, job lifecycle |
| `api.py` | Routes for sources, batches, documents, drafts, signed submission, audit, users |
| `db.py` | Database tables (PostgreSQL / SQLite) — the system of record |
| `audit.py` | Append-only, hash-chained audit trail |
| `edc_audit.py` | Outbox delivery of the audit trail to the EDC |
| `documents.py` | Ingest, versions, duplicate detection, extraction runs |
| `esource.py` | Ties the review steps to their permanent record; reasons, signatures |
| `pipeline.py`, `worker.py` | Server-side batch processing |
| `sources.py` | File-server sources and path safety |
| `blobstore.py` | Write-once storage of originals by SHA-256 |
| `auth.py` | Accounts, roles, lockout, sessions |
| `store.py` | Redis working copies of jobs, parse cache, locks |
| `edc.py` | Cronos EDC connector — the three `/api/EDC/*` endpoints |
| `visit_forms.py` | Turns an EDC visit into a renderable form, and back into a save payload |
| `cronos.py` | Committed CRF definitions and the older form-picker connector |
| `fields.py` | Asks the model for values against a CRF's field vocabulary |
| `mapping.py` | Coerces values to their declared types and assembles the reviewed form |
| `anchors.py` | Ties a value back to a rectangle on the original page |
| `vision.py` | Refines those rectangles (OpenAI vision, or local Tesseract) |
| `pdf_export.py`, `docx_export.py` | Downloadable copies of a reviewed CRF |
| `static/` | The browser app — no build step, no framework |
| `scripts/add_user.py` | Create accounts / reset passwords |
| `tests/` | `uv run pytest` |
| `scripts/doctor.py` | Preflight / deploy gate |
| `Dockerfile`, `docker-compose.yml` | Customer deploy (app + Redis, optional Caddy) |
