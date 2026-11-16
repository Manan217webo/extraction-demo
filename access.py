"""Who may see which records: study and site access that follows Cronos.

A person signed in through Cronos carries the study/site assignments Cronos
returned at sign-in (`users.cronos_assignments`). They see:

*   a source folder when one of their assignments matches the folder's `study`
    (Cronos projectId or projectName) and, if the folder names one, its `site`
    (siteId or siteName) — see sources.py;
*   a scanned record when its folder is one they see;
*   an upload when they uploaded it, or its study is one of theirs.

Administrators, local accounts (AUTH_MODE=local has no assignments) and
deployments with CRONOS_SITE_SCOPE=false see everything. Everyone else fails
closed: a folder nobody tagged, or a person with no assignments, shows nothing.

Every read and write path asks here, so a hidden record is also unreachable by
its link. Out-of-scope answers are 404s, not 403s, so ids can't be probed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import and_, false, func, or_, select

import db
import sources

UPLOAD_SOURCE = "upload"


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def enabled() -> bool:
    import cronos_auth
    return cronos_auth.enabled() and \
        (os.getenv("CRONOS_SITE_SCOPE") or "true").strip().lower() != "false"


@dataclass
class Scope:
    """What one person may see. `everything` short-circuits every check."""
    username: str
    everything: bool = False
    # Each assignment as ({study keys}, {site keys}); a key is a lowered id or name.
    assignments: list[tuple[set[str], set[str]]] = field(default_factory=list)

    @property
    def studies(self) -> set[str]:
        return {key for study, _ in self.assignments for key in study}

    def sees_source(self, conn: Any) -> bool:
        if self.everything:
            return True
        study, site = _norm(getattr(conn, "study", None)), _norm(getattr(conn, "site", None))
        if not study:
            return False
        # A folder with no site is the whole study's; an assignment with no site
        # covers every site of its study.
        return any(study in studies and (not site or not sites or site in sites)
                   for studies, sites in self.assignments)

    def source_ids(self, conns: Optional[Iterable[Any]] = None) -> set[str]:
        return {c.id for c in (conns if conns is not None else sources.load().values())
                if self.sees_source(c)}

    def sees_document(self, document: db.Document) -> bool:
        if self.everything:
            return True
        if document.source_id == UPLOAD_SOURCE:
            return document.created_by == self.username or \
                _norm(document.protocol_no) in self.studies
        return document.source_id in self.source_ids()

    def document_clause(self) -> Any:
        """The same rule as `sees_document`, as a WHERE clause on documents."""
        if self.everything:
            return None
        ids = self.source_ids()
        uploads = and_(db.Document.source_id == UPLOAD_SOURCE,
                       or_(db.Document.created_by == self.username,
                           func.lower(db.Document.protocol_no).in_(sorted(self.studies))
                           if self.studies else false()))
        return or_(db.Document.source_id.in_(sorted(ids)), uploads) if ids else uploads

    def visible_document_ids(self) -> Any:
        """A sub-select of the document ids this person sees (for the audit trail)."""
        return select(db.Document.id).where(self.document_clause())


def _keys(*values: Any) -> set[str]:
    return {_norm(v) for v in values if _norm(v)}


def scope_for(user: db.User) -> Scope:
    import auth
    if not enabled() or user.role == "admin" or not auth.is_cronos(user):
        return Scope(username=user.username, everything=True)
    assignments = []
    for item in user.cronos_assignments or []:
        if not isinstance(item, dict):
            continue
        studies = _keys(item.get("projectId"), item.get("projectName"),
                        item.get("studyNo"), item.get("protocolNo"))
        if studies:
            assignments.append((studies, _keys(item.get("siteId"), item.get("siteName"))))
    return Scope(username=user.username, assignments=assignments)
