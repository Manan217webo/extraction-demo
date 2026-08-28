"""Preflight check — is this machine ready to run a demo?

Run it before trusting the UI:

    uv run scripts/doctor.py

Every check goes through the application's own modules rather than
reimplementing them, so a pass here means the code paths the app uses actually
work on this machine. A parallel reimplementation could agree with itself while
the app fails.

Checks run in dependency order and each says what to do about a failure. Exits
non-zero if anything is a hard failure, so it can gate a deploy step.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import cronos  # noqa: E402
import edc  # noqa: E402
import fields  # noqa: E402
import vision  # noqa: E402
import visit_forms  # noqa: E402

# The visit the demo runs on. Override from the command line:
#   uv run scripts/doctor.py "ICR/24/001" 03013 "Visit 5"
DEMO_VISIT = ("ICR/24/001", "03013", "Visit 5")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_MARK = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}

results: list[tuple[str, str, str]] = []


def record(status: str, title: str, detail: str) -> str:
    results.append((status, title, detail))
    print(f"[{_MARK[status]}] {title}\n           {detail}")
    return status


# --------------------------------------------------------------------------- checks


def check_python() -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] == (3, 12):
        record(PASS, "Python version", f"{version}")
    else:
        record(FAIL, "Python version",
               f"{version} — the project pins 3.12. Run through `uv run`, which "
               "reads .python-version and fetches the right toolchain.")


def check_env_file() -> None:
    env = ROOT / ".env"
    if env.exists():
        record(PASS, ".env file", f"found at {env}")
        return
    stray = ROOT / ".env.txt"
    if stray.exists():
        record(FAIL, ".env file",
               "found .env.txt instead of .env — Notepad appended the extension. "
               "Rename it: `ren .env.txt .env`")
    else:
        record(FAIL, ".env file",
               f"not found at {env}. Copy the template: `copy .env.example .env`")


def check_document_reader() -> None:
    key = (os.getenv("LLAMA_CLOUD_API_KEY") or "").strip()
    if not key:
        record(FAIL, "Document reader (LlamaCloud)",
               "LLAMA_CLOUD_API_KEY is not set — every upload will fail.")
    elif key.lower().startswith("llx-your"):
        record(FAIL, "Document reader (LlamaCloud)",
               "LLAMA_CLOUD_API_KEY is still the placeholder from .env.example.")
    else:
        record(PASS, "Document reader (LlamaCloud)",
               f"key present ({key[:7]}…). Credit balance shows on the app's own page.")


def check_field_extraction() -> None:
    if fields.configured():
        record(PASS, "Field extraction (OpenAI)", f"model {fields.model_name()}")
    else:
        record(FAIL, "Field extraction (OpenAI)",
               "OPENAI_API_KEY is missing or still a placeholder — the header and "
               "field reads will both fail.")


def check_box_placement() -> None:
    backend = vision.backend()
    if backend == "openai":
        record(PASS, "Highlight boxes", f"OpenAI vision, model {vision.model_name()}")
    elif backend == "tesseract":
        record(PASS, "Highlight boxes", "local Tesseract")
    else:
        record(WARN, "Highlight boxes",
               "no backend available — boxes fall back to interpolating inside the "
               "parser's table rectangle and will drift. Set OPENAI_VISION_MODEL.")


def check_fixture_mode() -> None:
    offline = edc.fixtures()
    if offline is None:
        record(PASS, "EDC target", "live EDC (fixtures off)")
        return
    record(WARN, "EDC target",
           f"EDC_FIXTURES is set to {offline} — saves write to that file and report "
           "success without reaching the EDC. Unset it before the demo.")


def check_local_definitions() -> None:
    forms = cronos.local_forms()
    connector = cronos.get_connector()
    record(PASS, "Local CRF definitions",
           f"{len(forms)} committed definition(s); Cronos connector = {connector.name}")


async def check_edc_visit() -> None:
    protocol, screening, visit_name = (
        tuple(sys.argv[1:4]) if len(sys.argv) >= 4 else DEMO_VISIT
    )
    target = edc.fixtures() or edc.base_url()
    try:
        visit = await edc.get_visit(protocol, screening, visit_name)
    except edc.EdcUnavailable as exc:
        record(FAIL, "EDC visit read",
               f"{target} — {exc} (check EDC_BASE_URL has no trailing slash and "
               "names the IIS application, e.g. http://localhost/Cronos_Sun_EDC)")
        return

    crfs = visit.get("crfs") or []
    record(PASS, "EDC visit read",
           f"{protocol} / {screening} / {visit_name} → {len(crfs)} CRF(s) from {target}")

    definition = visit_forms.build_definition(visit, cronos.local_forms())
    for crf in definition["edc"]["crfs"]:
        section = next(s for s in definition["sections"]
                       if s["section_id"] == crf["section_id"])
        group = section.get("group")
        shape = (f"{len(group['fields'])} columns × {group['max_instances']} rows"
                 if group else "no table")
        record(PASS, f"  CRF {crf['crfId']} {crf['crfName']}",
               f"{len(crf['fields'])} fields, {shape}")
        if group:
            blank = [f["label"] for f in group["fields"]
                     if f["type"] in ("select", "radio") and not f.get("options")]
            if blank:
                record(WARN, f"  CRF {crf['crfId']} option lists",
                       f"no choices offered for: {', '.join(blank)} — the EDC sent no "
                       "data-csv for these, so they cannot be read or picked.")

    unsaveable = visit_forms.unsaveable(definition)
    if unsaveable:
        record(WARN, "Ambiguous field names",
               f"{len(unsaveable)} CRF(s) have repeated field names with no field_id "
               "to tell them apart; some values would collide on save.")
    else:
        record(PASS, "Field addressing", "every field carries a field_id — no collisions")


# --------------------------------------------------------------------------- main


async def main() -> int:
    print(f"\nextraction-demo preflight — {ROOT}\n")
    check_python()
    check_env_file()
    check_document_reader()
    check_field_extraction()
    check_box_placement()
    check_local_definitions()
    check_fixture_mode()
    await check_edc_visit()

    failures = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]
    print("\n" + "-" * 68)
    if failures:
        print(f"NOT READY — {len(failures)} failure(s), {len(warnings)} warning(s)")
        for _, title, _detail in failures:
            print(f"  · {title}")
        return 1
    if warnings:
        print(f"READY, with {len(warnings)} warning(s) to look at:")
        for _, title, _detail in warnings:
            print(f"  · {title}")
        return 0
    print("READY — all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
