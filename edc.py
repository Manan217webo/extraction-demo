"""Cronos EDC connector — the live Visit CRF endpoints.

Three endpoints matter here:

    GET  /api/EDC/GetVisitCRFData   the CRFs configured for one subject visit
    POST /api/EDC/SaveVisitCRFData  field values, and the source crops behind them
    GET  /api/EDC/GetVisitCRFImage  an image previously saved, by id

A visit is addressed by the triple (protocolNo, screeningNo, visitName) rather
than by any form id, which is why this sits beside `cronos` rather than inside
it: the shapes do not line up.

The deployment answers an unknown visit with a 302 to an HTML error page rather
than a 404, and that page redirects in turn, so redirects are never followed: a
redirect away from the endpoint *is* the "no such visit" answer.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("extraction.edc")

DEFAULT_BASE_URL = "https://cronosedc.dev.cronos.ws"
TIMEOUT = 60.0

# Enough for a page of dense CRF crops; the API's own ceiling is 5 MB per image.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class EdcUnavailable(RuntimeError):
    """The EDC could not be reached, or answered with something we can't use."""


# --------------------------------------------------------------------------- offline

# Point EDC_FIXTURES at a JSON file and the connector serves visits from it
# instead of the network, saving back into the same file. The deployment is not
# always up, and a reviewer working through a form should not be blocked on that.
# It is opt-in rather than a fallback: silently answering from a fixture when the
# real EDC is down would look exactly like the real EDC agreeing with us.


def fixtures() -> Optional[Path]:
    raw = (os.getenv("EDC_FIXTURES") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def _read_fixtures(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EdcUnavailable(f"The EDC fixture file could not be read: {exc}") from exc


def _matches(visit: dict[str, Any], protocol_no: str, screening_no: str,
             visit_name: str) -> bool:
    return (str(visit.get("protocolNo")) == protocol_no
            and str(visit.get("screeningNo")) == screening_no
            and str(visit.get("visitName")) == visit_name)


def _fixture_visit(path: Path, protocol_no: str, screening_no: str,
                   visit_name: str) -> dict[str, Any]:
    data = _read_fixtures(path)
    for visit in data.get("visits") or []:
        if _matches(visit, protocol_no, screening_no, visit_name):
            log.info("edc fixture: served %s/%s/%s", protocol_no, screening_no, visit_name)
            return visit
    known = ", ".join(
        f"{v.get('protocolNo')}/{v.get('screeningNo')}/{v.get('visitName')}"
        for v in data.get("visits") or []
    ) or "none"
    raise EdcUnavailable(
        "The EDC did not recognise that visit. Check the protocol number, "
        f"screening number and visit name. The fixture holds: {known}."
    )


def _fixture_save(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Write values back into the fixture, the way a working EDC would.

    Fields are matched on `field_id` where the payload carries one and on name
    otherwise — which is what the live deployment was measured doing, and why
    duplicate names collapsed there. Keeping both behaviours means the fixture
    can reproduce either.
    """
    data = _read_fixtures(path)
    target = next(
        (v for v in data.get("visits") or []
         if _matches(v, str(payload.get("protocolNo")), str(payload.get("screeningNo")),
                     str(payload.get("visitName")))),
        None,
    )
    if target is None:
        raise EdcUnavailable("The EDC did not recognise that visit.")

    next_image = max(
        [f.get("imageId") or 0 for crf in target["crfs"] for f in crf["fields"]] + [0]
    )
    report = []
    for incoming in payload.get("crfs") or []:
        crf = next((c for c in target["crfs"] if c.get("crfId") == incoming.get("crfId")), None)
        if crf is None:
            continue
        by_id = {f.get("field_id"): f for f in crf["fields"] if f.get("field_id") is not None}
        for sent in incoming.get("fields") or []:
            found = by_id.get(sent.get("field_id"))
            if found is None:
                found = next((f for f in crf["fields"]
                              if (f.get("field_name") or f.get("fieldName")) == sent.get("fieldName")),
                             None)
            if found is not None:
                found["value"] = sent.get("value", "")

        images = []
        for image in incoming.get("images") or []:
            next_image += 1
            images.append({"fileName": image.get("fileName"), "status": "Saved",
                           "imageId": next_image})
            slot = by_id.get(image.get("field_id"))
            if slot is None:
                slot = next((f for f in crf["fields"]
                             if (f.get("field_name") or f.get("fieldName")) == image.get("fieldName")),
                            None)
            if slot is not None:
                slot["hasImage"] = True
                slot["imageId"] = next_image
        report.append({"crfName": crf.get("crfName"), "crfId": crf.get("crfId"),
                       "status": "Saved", "images": images})

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("edc fixture: saved into %s", path)
    return {"success": True, "protocolNo": payload.get("protocolNo"),
            "screeningNo": payload.get("screeningNo"),
            "visitName": payload.get("visitName"), "crfs": report,
            "message": "CRF data saved successfully.", "fixture": True}


def base_url() -> str:
    return (os.getenv("EDC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def configured() -> bool:
    """True when a base URL is set — the dev host is the default, so always."""
    return bool(base_url())


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = (os.getenv("EDC_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _as_json(response: httpx.Response, what: str) -> Any:
    """The body as JSON, or a readable failure.

    A visit that does not exist comes back as a redirect to an HTML error page
    with a 200, so the content type is the only reliable signal that the request
    actually landed somewhere useful.
    """
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        log.warning("edc %s answered %s with %s", what, response.status_code, content_type or "no content type")
        raise EdcUnavailable(
            "The EDC did not recognise that visit. Check the protocol number, "
            "screening number and visit name."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise EdcUnavailable("The EDC returned a response we couldn't read.") from exc


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as http:
            response = await http.request(method, url, headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        log.warning("edc %s %s failed: %s", method, path, exc)
        raise EdcUnavailable("We couldn't reach the EDC. Please try again.") from exc

    if response.is_redirect:
        log.info("edc %s %s redirected to %s", method, path,
                 response.headers.get("location", "?"))
        raise EdcUnavailable(
            "The EDC did not recognise that visit. Check the protocol number, "
            "screening number and visit name."
        )
    if response.status_code >= 500:
        raise EdcUnavailable("The EDC is currently unavailable. Please try again.")
    if response.status_code >= 400:
        raise EdcUnavailable(
            f"The EDC rejected the request ({response.status_code}). "
            "Check the visit details and try again."
        )
    return _as_json(response, f"{method} {path}")


# --------------------------------------------------------------------------- reads


async def get_visit(protocol_no: str, screening_no: str,
                    visit_name: str) -> dict[str, Any]:
    """The CRFs configured for one subject visit, with any values already saved."""
    offline = fixtures()
    if offline:
        return _fixture_visit(offline, protocol_no, screening_no, visit_name)

    params = {
        "protocolNo": protocol_no,
        "screeningNo": screening_no,
        "visitName": visit_name,
    }
    data = await _request("GET", "/api/EDC/GetVisitCRFData", params=params)
    if not isinstance(data, dict) or not data.get("success"):
        raise EdcUnavailable(
            (data or {}).get("message")
            or "The EDC had no CRF data for that visit."
        )
    return data


def image_url(image_id: int | str) -> str:
    return f"{base_url()}/api/EDC/GetVisitCRFImage?imageId={image_id}"


# --------------------------------------------------------------------------- writes


async def save_visit(payload: dict[str, Any]) -> dict[str, Any]:
    """Send field values and their source crops back to the EDC."""
    offline = fixtures()
    if offline:
        return _fixture_save(offline, payload)

    data = await _request("POST", "/api/EDC/SaveVisitCRFData", json=payload)
    if not isinstance(data, dict):
        raise EdcUnavailable("The EDC returned an unexpected response to the save.")
    if not data.get("success"):
        raise EdcUnavailable(data.get("message") or "The EDC did not accept the save.")
    return data


def check_images(crfs: list[dict[str, Any]]) -> Optional[str]:
    """Why the attached crops would be refused, or None when they are fine."""
    for crf in crfs:
        for image in crf.get("images") or []:
            data = image.get("base64Data") or ""
            # base64 carries three bytes in every four characters.
            size = (len(data) * 3) // 4
            if size > MAX_IMAGE_BYTES:
                return (
                    f"The source image for “{image.get('fieldName')}” is "
                    f"{size // (1024 * 1024)} MB, over the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit."
                )
            if data and not image.get("contentType"):
                return f"The source image for “{image.get('fieldName')}” has no content type."
    return None
