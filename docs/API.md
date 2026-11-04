# eSource API contract

All routes are JSON under `/api` unless noted. Errors are `{"detail": "<sentence for a person>"}`
with a meaningful status. Timestamps are ISO-8601 UTC with `Z`. Roles:
`admin`, `reviewer`, `monitor` (read-only). A 401 means "sign in again"; the
session expires after `SESSION_IDLE_MINUTES` (default 15) of inactivity.

## Session

| Method | Path | Body → Response |
|---|---|---|
| POST | `/login` | `{username, password}` → `{username, full_name, role, idle_seconds}`. 401 wrong, 423 locked (`detail` says until when). |
| POST | `/logout` | → `{ok}` |
| GET | `/me` | → `{username, full_name, role, idle_seconds, expires_in}` |
| POST | `/session/ping` | Keeps the session alive. → `{expires_in}` |
| GET | `/session` | (existing) + `user`, `features: {sources, audit_delivery}` |
| GET | `/meta` | → `{reasons:[{code,label}], meanings:[{code,label}], user:{…}}` |

Reason codes: `TRANSCRIPTION_ERROR`, `ILLEGIBLE_SOURCE`, `CORRECTED_PER_SOURCE`,
`ALTERNATE_AI_READ`, `AI_MISREAD`, `UNIT_CONVERSION`, `QUERY_RESPONSE`,
`VALUE_REMOVED`, `OTHER` (free text required).
Meanings: `REVIEWED_ACCURATE` ("I confirm these values were verified against the
source document"), `AUTHOR` ("I am the author of these entries").

### Cronos sign-in (`AUTH_MODE=cronos`)

`POST /login` takes the same body but checks it with Cronos. New answers: 403 (password
must be changed in Cronos, account inactive or expired, no eSource role), 423 (locked in
Cronos), 429 (too many attempts), 503 (Cronos unreachable). `GET /me` adds `auth_source`
and, for Cronos accounts, `cronos:{user_id, roles, assignments}`. Tokens stay on the server.
Local admin accounts still sign in locally. Draft save, preflight and submit answer 403
without the Cronos duty and 503 if Cronos can't confirm it; `duty.denied` is audited, and
a delegated right adds `duty`, `duty_source`, `delegation_id` and `on_behalf_of` to the
`draft.saved`, `signature.applied` and `edc.*` event details.

## Sources (file server)

| Method | Path | Body → Response |
|---|---|---|
| GET | `/sources` | → `{sources:[{id, name, kind, location}]}` |
| GET | `/sources/{id}/browse?path=` | → `{source:{id,name}, path, parent, breadcrumbs:[{name,path}], entries:[Entry], truncated}` |

`Entry = {name, path, type:"dir"|"pdf", size, modified, status, document_id}` where
`status` is `null` (dirs), `"new"`, `"processed"`, `"changed"` (known path, file
differs), `"processing"`, `"failed"`.

## Batches

| Method | Path | Body → Response |
|---|---|---|
| POST | `/batches/preview` | `{source_id, paths:[...]}` (files or folders, folders are recursive) → `{files:[PreviewFile], counts:{new, processed, changed, processing}}` |
| POST | `/batches` | `{source_id, files:[{path, sha256, reprocess:bool}], reason}` → `{batch: Batch}`. `reason` required when any `reprocess` is true. Files that were processed and have `reprocess:false` are skipped (and audited as declined). |
| GET | `/batches` | → `{batches:[Batch]}` (newest first) |
| GET | `/batches/{id}` | → `{batch: Batch, items:[Item]}` |
| POST | `/batches/{id}/cancel` | → `{batch}` |
| POST | `/extractions/{id}/retry` | → `{item}` |

`PreviewFile = {path, name, size, sha256, status:"new"|"processed"|"changed"|"processing", prior:{document_id, processed_at, processed_by, submitted}|null}`
`Batch = {id, number, label, created_at, created_by, total, skipped, counts:{queued, running, ready, failed, cancelled}, done:bool, cancelled:bool}`
`Item = {extraction_id, document_id, filename, path, status:"queued"|"running"|"ready"|"failed"|"cancelled"|"needs_input", stage:"render"|"header"|"visit"|"read"|"done"|null, error, flags, conflicts, confidence, started_at, finished_at}`

## Documents (inbox, review, versions)

| Method | Path | Body → Response |
|---|---|---|
| GET | `/docs?status=&q=&protocol=&subject=&assignee=&flagged=&limit=&offset=` | → `{documents:[Doc], total, counts:{processing, ready, in_review, submitted, failed}}` |
| GET | `/docs/{id}` | → `{document: Doc}` |
| GET | `/docs/{id}/review` | → Review snapshot (below). Opening is audited. |
| GET | `/docs/{id}/file?version=` | PDF bytes (`application/pdf`) |
| POST | `/docs/{id}/assign` | `{username|null}` → `{document}` |
| GET | `/docs/{id}/history` | → `{versions:[{version, sha256, created_at, created_by, reason}], extractions:[{id, version, run, status, created_at, created_by, models, reason}], drafts:[{revision, saved_at, saved_by, autosave, edits:int}], submissions:[{id, submitted_at, submitted_by, status, changes:[…], skipped, signature:{full_name, meaning, signed_at}}]}` |
| GET | `/docs/{id}/diff?a=&b=` | `a`,`b` are draft revisions; `0` is the AI result of the current extraction. → `{a, b, changes:[{key, label, crf, a, b}]}` |
| POST | `/docs/{id}/reprocess` | `{reason}` → `{extraction_id}` |
| POST | `/docs/{id}/draft` | `{payload, base_revision, edits:[Edit], autosave}` → `{revision, saved_at}`. 409 when `base_revision` is stale. Never contacts the EDC. |
| POST | `/docs/{id}/preflight` | `{payload}` → `{changes:[{crfName, fieldName, label, old, new}], skipped:[{crfName, label, value}], warnings:[str], blocked:[{key,label,reason}], diff_sha}` — reads the EDC fresh. |
| POST | `/docs/{id}/submit` | `{payload, crops, password, meaning, diff_sha, edits:[Edit]}` → `{status:"sent"|"unchanged", counts, delta, warnings, signature:{full_name, meaning, signed_at}}`. 401 wrong password (no sign-out), 409 the EDC changed since preflight. |

`Doc = {id, filename, path, source_id, status:"processing"|"ready"|"in_review"|"submitted"|"failed"|"needs_input", protocol_no, screening_no, visit_name, flags, conflicts, confidence, assignee, updated_at, current_version, extraction:{id, status, stage, error}, draft_revision}`

`Edit = {key, label, old, new, reason_code, reason_text, kind:"edit"|"alternate"|"verify"}`

Review snapshot (the browser's restore shape, so the review screen opens it like a
saved session):
```
{ document: Doc, job_id, extraction_id, readonly:bool,
  result: {job_id, status:"COMPLETED", filename, page_count, mode:"vision", document_id, ...},
  header: <POST /documents/{job}/header response> | null,
  visit: <POST /documents/{job}/visit response> | null,
  payload: <map payload or latest draft payload> | null,
  draft: {revision, saved_at, saved_by} | null,
  submitted: {submitted_at, submitted_by} | null,
  reader: {model, second_read, preferred} }
```

The interactive upload (`POST /documents`) still works and now also returns
`document_id`; the old `/documents/{job_id}/…` step routes are unchanged.
`POST /documents/{job_id}/visit/save` without `dry_run` answers 410: submitting
needs a signature, so it goes through `/docs/{id}/submit`.

## Audit

| Method | Path | Body → Response |
|---|---|---|
| GET | `/audit?user=&document_id=&subject=&action=&q=&from=&to=&before=&limit=` | → `{events:[Event], next_before}` (newest first, `before` is a seq) |
| GET | `/audit/actions` | → `{actions:[str]}` |
| GET | `/audit/export?format=csv|json&…same filters` | file download |
| GET | `/audit/verify` | → `{ok, checked, head, broken_at, verified_at}` |
| GET | `/audit/delivery` | → `{mode:"live"|"held", endpoint, counts:{pending, sent, failed, held}, oldest_pending}` |
| POST | `/audit/delivery/retry` | → `{requeued}` |

`Event = {seq, id, ts, action, username, full_name, role, document_id, protocol_no, screening_no, visit_name, field, old_value, new_value, reason, details, client_ip, app_version, prev_hash, hash, delivery:{status, attempts, last_error, sent_at}}`

### EDC pull (system-to-system)

For the EDC to fetch the trail itself instead of (or as well as) receiving the push
to `EDC_AUDIT_URL`. No sign-in: the caller sends an API key from `EDC_PULL_API_KEY`.
The endpoint answers 404 until that is set.

| Method | Path | Body → Response |
|---|---|---|
| GET | `/edc/audit?after=0&limit=500` | `Authorization: Bearer <key>` → `{source, sourceVersion, records:[AuditRecord], next_after, has_more, head_seq}` |

- Records come oldest first, with `Sequence > after`. `limit` is 1–1000 (default 500).
- Keep `next_after` and pass it as `after` next time; while `has_more` is true, ask again
  straight away. Polling every minute or so is plenty.
- A record never appears behind one with a higher `Sequence`, so following `next_after`
  never skips one. Sequences may have gaps; a record's `PrevHash` equals the previous
  record's `Hash`, which shows nothing real is missing.
- `AuditRecord` is the same object the push sends: `AuditRecordID, Sequence, DateTimeStamp,
  Action, UserRef{LoginName, FullName, Role}, LocationRef{IP}, ReasonForChange, StudyOID,
  SubjectKey, StudyEventOID, FormOID, ItemOID, ItemRef, OldValue, NewValue,
  SourceDocumentID, Details, SourceSystemVersion, PrevHash, Hash`.
- 401 for a missing or wrong key. Pulling does not change push delivery status.

## Admin

| Method | Path | Body → Response |
|---|---|---|
| GET | `/admin/users` | → `{users:[{username, full_name, role, active, locked_until, last_login_at}]}` |
| POST | `/admin/users` | `{username, full_name, role, password}` → `{user}` |
| PATCH | `/admin/users/{username}` | `{full_name?, role?, active?, unlock?, password?}` → `{user}` |
