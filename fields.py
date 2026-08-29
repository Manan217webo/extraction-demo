"""Field extraction — turn parsed markdown into typed values a CRF can hold.

A second pass over the parsed document asks an OpenAI model for values against a
fixed vocabulary of field ids: the header block first, then the fields of the
Cronos form the reviewer chose.  Every value comes back with the wording the model
read it from, which `anchors` uses to put a box on the page.

The result shape is deliberately flat — one row per value, carrying its own
section/group/instance address — so a single JSON schema serves any CRF the
connector hands us, however many repeating groups it has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

import cronos

log = logging.getLogger("extraction.fields")

API_BASE = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
DEFAULT_MODEL = "gpt-4o"
MAX_DOCUMENT_CHARS = 180_000
# A stalled connection used to cost the whole timeout before anything was
# retried — a reviewer sat on "Matching the document header" for minutes over a
# single dropped socket. Connecting is given a few seconds; reading is bounded
# to what a large form actually needs, and the request is retried on top.
REQUEST_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT_SECONDS") or 90)
CONNECT_TIMEOUT = 10.0
TRANSPORT_RETRIES = 2

# Reasoning models (gpt-5*, o1/o3, terra) reject `temperature`. Remembering a
# 400 here means the next call — and every locate batch — skips the wasted retry.
_NO_TEMPERATURE: set[str] = set()
_NO_TEMPERATURE_MARKERS = ("gpt-5", "o1", "o3", "o4", "terra")


class ExtractionUnavailable(RuntimeError):
    """The field-extraction model is not configured or could not be reached."""


def model_name() -> str:
    return (os.getenv("OPENAI_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _api_key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def configured() -> bool:
    key = _api_key()
    return bool(key) and not key.lower().startswith("sk-your") and key.lower() not in {
        "changeme", "your_api_key", "your-api-key",
    }


# --------------------------------------------------------------------------- document


def document_for_model(pages: list[dict[str, Any]], markdown: str) -> str:
    """Page-tagged markdown, so the model can tell us which page it read a value on."""
    if pages:
        chunks = [
            f"<page number=\"{page.get('page_number') or index + 1}\">\n"
            f"{(page.get('content') or '').strip()}\n</page>"
            for index, page in enumerate(pages)
        ]
        document = "\n\n".join(chunks)
    else:
        document = f"<page number=\"1\">\n{(markdown or '').strip()}\n</page>"

    if len(document) > MAX_DOCUMENT_CHARS:
        document = document[:MAX_DOCUMENT_CHARS] + "\n\n<!-- document truncated -->"
        log.warning("document truncated to %s chars for field extraction", MAX_DOCUMENT_CHARS)
    return document


def was_truncated(document: str) -> bool:
    return document.endswith("<!-- document truncated -->")


# --------------------------------------------------------------------------- schema

VALUE_PROPERTIES = {
    "field_id": {"type": "string", "description": "Exactly one of the listed field ids."},
    "value": {
        "type": ["string", "null"],
        "description": "The value as it should be stored, or null if the form leaves it blank.",
    },
    "evidence": {
        "type": ["string", "null"],
        "description": "What you read, in your own words if the value is a tick or a "
                       "mark rather than text. Null if the field is blank.",
    },
    "locator": {
        "type": ["string", "null"],
        "description": "The printed label or row heading that sits next to this value, "
                       "copied EXACTLY as it is printed on the page — for a table, the "
                       "row heading. This must be text that really appears on the page; "
                       "it is used to find the value's position. Null if there is none.",
    },
    "page": {"type": ["integer", "null"], "description": "Page number the value was read from."},
    "confidence": {
        "type": "number",
        "description": "0 to 1. Below 0.6 for handwriting you are unsure of.",
    },
}

HEADER_SCHEMA = {
    "type": "object",
    "properties": {
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": VALUE_PROPERTIES,
                "required": list(VALUE_PROPERTIES),
                "additionalProperties": False,
            },
        }
    },
    "required": ["values"],
    "additionalProperties": False,
}

FORM_VALUE_PROPERTIES = {
    "section_id": {"type": "string"},
    "group_id": {
        "type": ["string", "null"],
        "description": "The repeating group id, or null for a plain section field.",
    },
    "instance": {
        "type": "integer",
        "description": "1 for plain section fields; the row number within a repeating group.",
    },
    **VALUE_PROPERTIES,
}

FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": FORM_VALUE_PROPERTIES,
                "required": list(FORM_VALUE_PROPERTIES),
                "additionalProperties": False,
            },
        }
    },
    "required": ["values"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- prompts

RULES = """You are transcribing a completed clinical trial source document into an
electronic case report form. Accuracy matters more than completeness.

Rules:
- Only report a value you can actually see on the page. Never infer, calculate or
  carry a value over from another field. If a field is blank, struck through or you
  cannot read it, return null.
- Tick boxes reach you as markers, in two notations:
      [x]  or [yes]   the box IS ticked
      [ ]  or [no]    the box is EMPTY
  `[no]` means an empty box. It does NOT mean the answer "No".
  So `[x] Normal [ ] Abnormal` is "Normal", and `[ ] Yes [x] No` is "No", but
  `[ ] CS [ ] NCS` and `[no] ... [no]` are nothing at all — neither option was
  marked, the field was not recorded, and you must return null.
- An empty tick box is never an answer. Never turn "nothing was ticked" into "No",
  "Not done" or any other value. Report only the option that is actually marked.
- `locator` must be text that is genuinely printed on the page, copied character for
  character — normally the row heading or the field label immediately beside the
  value ("Lymph Nodes", "Pulse rate", "Protocol No."). Never put a description or a
  sentence here, and never invent one. It is what places the value on the page.
- `evidence` may describe what you saw when the value is a tick or a handwritten
  mark rather than printed text. When a value comes from a tick box, `evidence`
  MUST contain the marker exactly as it appears in the text, e.g. "[x] Normal" or
  "[ ] CS [ ] NCS", so the tick can be checked.
- Dates: return ISO 8601 `YYYY-MM-DD`. If the source is ambiguous (e.g. 03/04/2025)
  keep the value but drop confidence below 0.6.
- Times: return 24-hour `HH:MM`.
- Numbers: digits only, no units. Record the unit only where a unit field exists.
- Where a field lists allowed options, return one of those options exactly.
- Handwriting, character boxes and tick boxes are common. Where a digit is genuinely
  ambiguous, still give your best reading but set confidence below 0.6.
- Return at most one entry per field address. Omit fields entirely rather than
  guessing."""


def header_prompt(document: str) -> list[dict[str, str]]:
    lines = []
    for group in cronos.HEADER_GROUPS:
        lines.append(f"\n{group['name']}:")
        for field in group["fields"]:
            bits = [f"  - {field['field_id']}: {field['label']} ({field['type']}"]
            if field.get("unit"):
                bits.append(f", unit {field['unit']}")
            if field.get("options"):
                bits.append(f", one of: {', '.join(field['options'])}")
            bits.append(")")
            if field.get("description"):
                bits.append(f" — {field['description']}")
            lines.append("".join(bits))
    return [
        {"role": "system", "content": RULES},
        {
            "role": "user",
            "content": (
                "Read the header block of this source document and return the "
                "identifying fields below.\n"
                f"{chr(10).join(lines)}\n\n"
                "Document:\n\n" + document
            ),
        },
    ]


def form_prompt(document: str, form: dict[str, Any], header: dict[str, Any]) -> list[dict[str, str]]:
    lines = [f"Form: {form.get('form_name')} ({form.get('form_id')}, "
             f"version {form.get('form_version')})",
             f"Description: {form.get('form_description') or '—'}", ""]

    def describe(field: dict[str, Any]) -> str:
        bits = [f"      - {field['field_id']}: {field['label']} [{field['type']}"]
        if field.get("unit"):
            bits.append(f", unit {field['unit']}")
        if field.get("options"):
            bits.append(f", one of: {' | '.join(field['options'])}")
        if field.get("min") is not None and field.get("max") is not None:
            bits.append(f", expected {field['min']}–{field['max']}")
        if field.get("derived"):
            bits.append(", only if printed on the page — never calculate it")
        bits.append("]")
        return "".join(bits)

    for section in form.get("sections") or []:
        lines.append(f"  Section {section['section_id']} — {section['name']}")
        if section.get("description"):
            lines.append(f"    {section['description']}")
        if section.get("fields"):
            lines.append(f"    Fields (group_id null, instance 1):")
            lines.extend(describe(f) for f in section["fields"])
        group = section.get("group")
        if group:
            if group.get("positional_rows"):
                # The EDC stores these rows by position and gives them no labels,
                # so an instance number is the printed row, not a running count.
                # Skipping a row must leave a hole rather than shift the rest up.
                counting = (
                    f"one instance per {group['row_label'].lower()} row printed on "
                    f"the page, numbered by its position in that table — row 1 is "
                    f"instance 1, row 2 is instance 2, and so on for all "
                    f"{group['max_instances']} rows. If a row is blank, leave that "
                    f"instance out entirely; never renumber the rows that follow it"
                )
            else:
                counting = (
                    f"one instance per {group['row_label'].lower()} actually "
                    f"recorded, numbered from 1, up to {group['max_instances']}"
                )
            lines.append(
                f"    Repeating group {group['label']} — {counting}. "
                f"Address every field below as "
                f"section_id \"{section['section_id']}\", group_id "
                f"\"{group['group_id']}\", instance 1, 2, 3 … "
                f"(section_id stays \"{section['section_id']}\" — it is never "
                f"\"{group['group_id']}\")."
            )
            lines.extend(describe(f) for f in group["fields"])
        lines.append("")

    context = ", ".join(
        f"{cronos.header_field(k)['label'] if cronos.header_field(k) else k}: {v}"
        for k, v in (header or {}).items() if v
    )

    return [
        {"role": "system", "content": RULES},
        {
            "role": "user",
            "content": (
                "Transcribe this source document into the CRF below. Use the exact "
                "section_id, group_id and field_id given. For a repeating group, create one "
                "instance per row of data present on the page — do not pad out empty rows.\n\n"
                "Each section below is a titled block on the page, and the sheet stacks "
                "many of them. Read a section's fields ONLY from inside its own block. "
                "Several blocks ask the same question — more than one carries a date of "
                "assessment — so a value taken from the wrong block is wrong even when it "
                "happens to read the same. If a section's block does not answer a field, "
                "leave it out rather than borrowing from a neighbouring block.\n\n"
                + "\n".join(lines)
                + (f"\nConfirmed header context: {context}\n" if context else "")
                + "\nDocument:\n\n" + document
            ),
        },
    ]


# --------------------------------------------------------------------------- transport


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    """One request, retried across transport failures only.

    A dropped socket, a stalled read or a refused connect is retried straight
    away with a short pause; an HTTP status is never retried here, because the
    caller decides what a 400 means. Retrying blindly on status would double
    the cost of every rejected request.
    """
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
    last: Optional[BaseException] = None
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                return_value = await http.post(
                    f"{API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {_api_key()}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            break
        except httpx.TransportError as exc:
            last = exc
            if attempt >= TRANSPORT_RETRIES:
                raise
            log.warning("field extraction transport failure (%s), retrying: %s",
                        type(exc).__name__, exc or "connection dropped")
            await asyncio.sleep(1.5 * (attempt + 1))
    if return_value.status_code >= 400:
        raise httpx.HTTPStatusError(
            return_value.text, request=return_value.request, response=return_value
        )
    return return_value.json()


def _unsupported(detail: str, *needles: str) -> bool:
    lowered = detail.lower()
    return any(needle in lowered for needle in needles)


async def complete(messages: list[Any], schema: dict[str, Any], schema_name: str,
                   model: Optional[str] = None) -> dict[str, Any]:
    """A structured completion against any model on the configured account."""
    return await _complete(messages, schema, schema_name, model=model)


def _uses_temperature(model: str) -> bool:
    if model in _NO_TEMPERATURE:
        return False
    lowered = model.lower()
    return not any(marker in lowered for marker in _NO_TEMPERATURE_MARKERS)


async def _complete(messages: list[Any], schema: dict[str, Any],
                    schema_name: str, model: Optional[str] = None) -> dict[str, Any]:
    """One completion, degrading gracefully across model families."""
    if not configured():
        raise ExtractionUnavailable(
            "Field extraction isn't configured yet. Please contact your administrator."
        )

    chosen = model or model_name()
    payload: dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    if _uses_temperature(chosen):
        payload["temperature"] = 0

    for attempt in range(4):
        try:
            data = await _post(payload)
            break
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text or ""
            # Reasoning models reject `temperature`; older models reject json_schema.
            if exc.response.status_code == 400 and "temperature" in payload \
                    and _unsupported(detail, "temperature"):
                payload.pop("temperature")
                _NO_TEMPERATURE.add(chosen)
                continue
            if exc.response.status_code == 400 and _unsupported(
                detail, "response_format", "json_schema"
            ):
                payload["response_format"] = {"type": "json_object"}
                payload["messages"] = messages + [{
                    "role": "system",
                    "content": "Reply with JSON matching this schema exactly:\n"
                               + json.dumps(schema),
                }]
                continue
            log.error("field extraction rejected (%s): %s", exc.response.status_code, detail[:400])
            if exc.response.status_code in (401, 403):
                raise ExtractionUnavailable(
                    "The field-extraction service rejected our credentials. "
                    "Please contact your administrator."
                ) from exc
            if exc.response.status_code == 429:
                raise ExtractionUnavailable(
                    "The field-extraction service is busy. Please try again in a moment."
                ) from exc
            raise ExtractionUnavailable("We couldn't read the fields from this document.") from exc
        except httpx.HTTPError as exc:
            # str(exc) is empty for a plain dropped connection; the class name is
            # the only thing that says what happened.
            log.error("field extraction transport error (%s): %s",
                      type(exc).__name__, exc or "connection dropped")
            raise ExtractionUnavailable(
                "We couldn't reach the field-extraction service. Please try again."
            ) from exc
    else:
        raise ExtractionUnavailable("We couldn't read the fields from this document.")

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"] or "{}"
    except (KeyError, IndexError) as exc:
        raise ExtractionUnavailable("We couldn't read the fields from this document.") from exc

    if choice.get("finish_reason") == "length":
        # The JSON is cut mid-object, so nothing can be salvaged from it. Say why,
        # because the fix is a different model rather than a retry.
        log.error("field extraction hit the model output limit (%s)", model_name())
        raise ExtractionUnavailable(
            "This document has more fields than the current reading model can return "
            f"in one go ({model_name()}). Try a model with a larger output limit, or "
            "split the document into fewer pages."
        )

    usage = data.get("usage") or {}
    parsed = _loads(content)
    parsed["_usage"] = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "model": data.get("model") or model_name(),
    }
    return parsed


def _loads(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"\{.*\}", content, re.S)  # a stray code fence or preamble
    if fenced:
        try:
            return json.loads(fenced.group(0))
        except json.JSONDecodeError:
            pass
    log.error("field extraction returned unparseable content: %s", content[:300])
    raise ExtractionUnavailable("We couldn't read the fields from this document.")


# --------------------------------------------------------------------------- passes


def _clean(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    field_id = (row.get("field_id") or "").strip()
    if not field_id:
        return None
    value = row.get("value")
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in {"", "null", "none", "n/a", "na", "not recorded", "-", "—"}:
            value = None
    elif value is not None:
        value = str(value)
    evidence = (row.get("evidence") or "").strip() or None
    locator = (row.get("locator") or "").strip() or None
    try:
        confidence = float(row.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    page = row.get("page")
    return {
        "field_id": field_id,
        "value": value,
        "evidence": evidence,
        "locator": locator,
        "page": int(page) if isinstance(page, (int, float)) and page else None,
        "confidence": max(0.0, min(1.0, confidence)),
    }


async def extract_header(document: str) -> dict[str, Any]:
    result = await _complete(header_prompt(document), HEADER_SCHEMA, "crf_header")
    allowed = set(cronos.HEADER_FIELD_IDS)
    rows: dict[str, dict[str, Any]] = {}
    for raw in result.get("values") or []:
        row = _clean(raw)
        if row and row["field_id"] in allowed and row["field_id"] not in rows:
            rows[row["field_id"]] = row
    return {"values": list(rows.values()), "usage": result.get("_usage", {})}


def _merge_usage(parts: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = completion = 0
    models: list[str] = []
    for part in parts:
        usage = part.get("_usage") or {}
        prompt += usage.get("prompt_tokens") or 0
        completion += usage.get("completion_tokens") or 0
        if usage.get("model"):
            models.append(usage["model"])
    return {
        "prompt_tokens": prompt or None,
        "completion_tokens": completion or None,
        "model": models[0] if models else model_name(),
    }


async def _extract_sections(document: str, form: dict[str, Any],
                            header: dict[str, Any]) -> dict[str, Any]:
    """One completion per CRF, in parallel — wall clock is the slowest section.

    A visit with two CRFs was a single 70s call. Splitting keeps each prompt to
    one block (which is how the page is printed) so two ~20s calls overlap.
    """
    sections = form.get("sections") or []
    if len(sections) <= 1:
        return await _complete(form_prompt(document, form, header), FORM_SCHEMA, "crf_form")

    async def one(section: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        mini = {**form, "sections": [section]}
        result = await _complete(form_prompt(document, mini, header), FORM_SCHEMA, "crf_form")
        log.info("section %s (%s) read in %.1fs (%s value(s))",
                 section.get("section_id"), section.get("name"),
                 time.monotonic() - started, len(result.get("values") or []))
        return result

    gathered = await asyncio.gather(
        *[one(section) for section in sections], return_exceptions=True
    )
    merged: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    failed = 0
    for section, item in zip(sections, gathered):
        if isinstance(item, BaseException):
            failed += 1
            log.warning("section %s (%s) failed: %s",
                        section.get("section_id"), section.get("name"), item)
            continue
        merged.extend(item.get("values") or [])
        usages.append(item)
    if not merged and failed:
        raise ExtractionUnavailable("We couldn't read the fields from this document.")
    return {"values": merged, "_usage": _merge_usage(usages)}


async def extract_form(document: str, form: dict[str, Any],
                       header: dict[str, Any]) -> dict[str, Any]:
    result = await _extract_sections(document, form, header)

    valid = {
        (section["section_id"], (group or {}).get("group_id"), field["field_id"])
        for section, group, field in cronos.iter_fields(form)
    }
    # Models routinely put the group id in section_id, or omit one of the two.
    # A field id is unique enough within a CRF to recover the full address, so a
    # value is only discarded when nothing in the form could have produced it.
    by_field: dict[str, list[tuple]] = {}
    for address in valid:
        by_field.setdefault(address[2], []).append(address)

    def resolve(section_id: str, group_id: Optional[str],
                field_id: str) -> Optional[tuple]:
        candidates = by_field.get(field_id) or []
        if not candidates:
            return None
        if (section_id, group_id, field_id) in valid:
            return (section_id, group_id, field_id)
        for match in candidates:  # trust the group, correct the section
            if group_id and match[1] == group_id:
                return match
        for match in candidates:  # the group id arrived as the section id
            if section_id and match[1] == section_id:
                return match
        for match in candidates:  # right section, group left blank
            if section_id and match[0] == section_id:
                return match
        return candidates[0] if len(candidates) == 1 else None

    seen: set[tuple] = set()
    rows: list[dict[str, Any]] = []
    dropped = 0
    repaired = 0
    for raw in result.get("values") or []:
        row = _clean(raw)
        if not row:
            continue
        section_id = (raw.get("section_id") or "").strip()
        group_id = (raw.get("group_id") or None) or None
        if isinstance(group_id, str):
            group_id = group_id.strip() or None

        address = resolve(section_id, group_id, row["field_id"])
        if address is None:
            dropped += 1
            continue
        if address != (section_id, group_id, row["field_id"]):
            repaired += 1
        section_id, group_id = address[0], address[1]
        try:
            instance = max(1, int(raw.get("instance") or 1))
        except (TypeError, ValueError):
            instance = 1
        if group_id is None:
            instance = 1
        key = address + (instance,)
        if key in seen:
            continue
        seen.add(key)
        rows.append({**row, "section_id": section_id, "group_id": group_id,
                     "instance": instance})

    if repaired:
        log.info("repaired the address of %s value(s) for %s", repaired, form.get("form_id"))
    if dropped:
        log.info("dropped %s value(s) matching no field in %s", dropped, form.get("form_id"))
    return {"values": rows, "usage": result.get("_usage", {}),
            "dropped": dropped, "repaired": repaired}
