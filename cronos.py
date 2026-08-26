"""Cronos connector — the boundary between this service and the internal EDC.

Everything the mapping stage needs about a CRF (the *form description*) and
everything it sends back (the *form connector*) goes through `Connector`.  The
mock implementation reads the CRF definitions in `cronos_forms/` so the flow can
be exercised end to end today; pointing `CRONOS_BASE_URL` at the real Cronos
swaps in the HTTP implementation without a single change above this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx

log = logging.getLogger("extraction.cronos")

FORMS_DIR = Path(__file__).resolve().parent / "cronos_forms"

# The header block a reviewer confirms before any field mapping happens. These
# three, and only these three, are what `GetVisitCRFData` looks a visit up by, so
# anything else asked here would be a question with nowhere to send the answer.
HEADER_GROUPS: list[dict[str, Any]] = [
    {
        "group_id": "visit",
        "name": "Visit identity",
        "fields": [
            {"field_id": "protocol_no", "label": "Protocol No.", "type": "text",
             "required": True,
             "description": "The protocol number exactly as printed, slashes included."},
            {"field_id": "screening_no", "label": "Screening No.", "type": "text",
             "required": True,
             "description": "The subject's screening number, digits only."},
            {"field_id": "visit_name", "label": "Visit name", "type": "text",
             "required": True,
             "description": "The visit as the EDC names it, e.g. \"Visit 5\"."},
        ],
    },
]

HEADER_FIELD_IDS = [f["field_id"] for g in HEADER_GROUPS for f in g["fields"]]


def header_field(field_id: str) -> Optional[dict[str, Any]]:
    for group in HEADER_GROUPS:
        for field in group["fields"]:
            if field["field_id"] == field_id:
                return field
    return None


# --------------------------------------------------------------------------- helpers


def iter_fields(form: dict[str, Any]):
    """Yield (section, group_or_None, field) for every field in a form definition."""
    for section in form.get("sections") or []:
        for field in section.get("fields") or []:
            yield section, None, field
        group = section.get("group")
        if group:
            for field in group.get("fields") or []:
                yield section, group, field


def find_field(form: dict[str, Any], section_id: str, field_id: str,
               group_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    for section, group, field in iter_fields(form):
        if section["section_id"] != section_id or field["field_id"] != field_id:
            continue
        if (group or {}).get("group_id") != group_id:
            continue
        return field
    return None


def _summary(form: dict[str, Any]) -> dict[str, Any]:
    """The listing shape — enough to choose a form without loading every field."""
    sections = form.get("sections") or []
    return {
        "form_id": form.get("form_id"),
        "form_name": form.get("form_name"),
        "form_version": form.get("form_version"),
        "form_description": form.get("form_description"),
        "visit": form.get("visit"),
        "section_count": len(sections),
        "field_count": sum(1 for _ in iter_fields(form)),
        "sections": [
            {"section_id": s["section_id"], "name": s["name"],
             "repeating": bool(s.get("group"))}
            for s in sections
        ],
    }


def local_forms() -> list[dict[str, Any]]:
    """Every committed CRF definition, whichever connector is in use.

    The EDC returns bare field names, so these are read alongside it for the
    types, options and row labels it has no way to express.
    """
    out: list[dict[str, Any]] = []
    for path in sorted(FORMS_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("skipping unreadable CRF definition %s: %s", path.name, exc)
    return out


class CronosUnavailable(RuntimeError):
    """Cronos could not be reached or answered with an error."""


# --------------------------------------------------------------------------- interface


class Connector(Protocol):
    name: str

    async def list_forms(self, protocol_no: Optional[str]) -> list[dict[str, Any]]: ...
    async def get_form(self, form_id: str) -> Optional[dict[str, Any]]: ...
    async def describe_header(self) -> list[dict[str, Any]]: ...
    async def submit(self, form_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- mock


class MockConnector:
    """Serves the CRF definitions committed under `cronos_forms/`."""

    name = "mock"
    live = False

    def __init__(self, directory: Path = FORMS_DIR) -> None:
        self._dir = directory
        self._cache: Optional[dict[str, dict[str, Any]]] = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            forms: dict[str, dict[str, Any]] = {}
            for path in sorted(self._dir.glob("*.json")):
                try:
                    form = json.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("skipping unreadable CRF definition %s: %s", path.name, exc)
                    continue
                if form.get("form_id"):
                    forms[form["form_id"]] = form
            self._cache = forms
        return self._cache

    async def list_forms(self, protocol_no: Optional[str]) -> list[dict[str, Any]]:
        wanted = (protocol_no or "").strip().lower()
        out = []
        for form in self._load().values():
            protocols = [str(p).strip().lower() for p in (form.get("protocols") or ["*"])]
            if wanted and "*" not in protocols and wanted not in protocols:
                continue
            out.append(_summary(form))
        return out

    async def get_form(self, form_id: str) -> Optional[dict[str, Any]]:
        return self._load().get(form_id)

    async def describe_header(self) -> list[dict[str, Any]]:
        return HEADER_GROUPS

    async def submit(self, form_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Accepts and acknowledges without sending anything anywhere."""
        counted = sum(
            len(s.get("fields") or []) + sum(len(i.get("fields") or [])
                                             for g in (s.get("groups") or [])
                                             for i in (g.get("instances") or []))
            for s in (payload.get("form", {}).get("sections") or [])
        )
        log.info("mock submit: form=%s fields=%s", form_id, counted)
        return {
            "accepted": True,
            "live": False,
            "form_id": form_id,
            "fields_received": counted,
            "message": "Mock connector — nothing was sent to Cronos.",
        }


# --------------------------------------------------------------------------- http


class HttpConnector:
    """Talks to a real Cronos deployment. Endpoints follow the mock's contract."""

    name = "http"
    live = True

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.request(
                    method, f"{self._base}{path}", headers=self._headers(), **kwargs
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            log.warning("cronos %s %s failed: %s", method, path, exc)
            raise CronosUnavailable(str(exc)) from exc

    async def list_forms(self, protocol_no: Optional[str]) -> list[dict[str, Any]]:
        params = {"protocol_no": protocol_no} if protocol_no else None
        data = await self._request("GET", "/api/forms", params=params)
        return data.get("forms", data) if isinstance(data, dict) else data

    async def get_form(self, form_id: str) -> Optional[dict[str, Any]]:
        try:
            return await self._request("GET", f"/api/forms/{form_id}")
        except CronosUnavailable:
            return None

    async def describe_header(self) -> list[dict[str, Any]]:
        try:
            data = await self._request("GET", "/api/forms/header")
            groups = data.get("groups", data) if isinstance(data, dict) else data
            return groups or HEADER_GROUPS
        except CronosUnavailable:
            return HEADER_GROUPS  # a header we can't fetch must not block review

    async def submit(self, form_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", f"/api/forms/{form_id}/submissions", json=payload)
        return {"accepted": True, "live": True, "form_id": form_id, **(data or {})}


# --------------------------------------------------------------------------- factory

_connector: Optional[Connector] = None


def get_connector() -> Connector:
    """HTTP when `CRONOS_BASE_URL` is configured, otherwise the committed mock."""
    global _connector
    if _connector is None:
        base = (os.getenv("CRONOS_BASE_URL") or "").strip()
        if base:
            _connector = HttpConnector(base, (os.getenv("CRONOS_API_KEY") or "").strip())
            log.info("cronos connector: http (%s)", base)
        else:
            _connector = MockConnector()
            log.info("cronos connector: mock (%s)", FORMS_DIR)
    return _connector
