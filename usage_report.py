"""Token use, and what it costs, across the AI reads — for the cost review.

Every run already keeps what its model calls used:

- the form read: `Extraction.usage` (split by model when two models read);
- the header read: `Extraction.snapshot["meta"]["header_usage"]`;
- the split of a multi-visit file: `Split.usage`.

This module adds them up over a date range, by model, by stage, by day and per
document and page, so the cost can be judged from what was actually spent
rather than estimated. Prices are not in the code: set TOKEN_PRICES to a JSON
object of US dollars per million tokens, for example

    TOKEN_PRICES='{"gpt-4.1": {"input": 2.0, "output": 8.0},
                   "gpt-4.1-mini": {"input": 0.4, "output": 1.6}}'

A model with no price is still counted; its cost is reported as unknown.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict
from typing import Any, Iterable, Optional

from sqlalchemy import select

import db

STAGES = ("split", "header", "form")


def prices() -> dict[str, dict[str, float]]:
    """{model: {"input": $/M, "output": $/M}} from TOKEN_PRICES; {} when unset or broken."""
    raw = (os.getenv("TOKEN_PRICES") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for model, price in (data or {}).items():
        try:
            out[str(model)] = {"input": float(price["input"]), "output": float(price["output"])}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def entries(usage: Optional[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """(model, prompt tokens, completion tokens) for each model a usage record names."""
    if not isinstance(usage, dict):
        return []
    nested = usage.get("by_model")
    if isinstance(nested, dict) and nested:
        return [(str(name), int(c.get("prompt_tokens") or 0), int(c.get("completion_tokens") or 0))
                for name, c in nested.items() if isinstance(c, dict)]
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if not prompt and not completion:
        return []
    return [(str(usage.get("model") or "unknown"), prompt, completion)]


def cost(model: str, prompt: int, completion: int,
         table: dict[str, dict[str, float]]) -> Optional[float]:
    price = table.get(model)
    if price is None:
        return None
    return (prompt * price["input"] + completion * price["output"]) / 1_000_000


class _Tally:
    __slots__ = ("calls", "prompt", "completion", "cost", "unpriced")

    def __init__(self) -> None:
        self.calls = self.prompt = self.completion = self.unpriced = 0
        self.cost = 0.0

    def add(self, prompt: int, completion: int, spent: Optional[float]) -> None:
        self.calls += 1
        self.prompt += prompt
        self.completion += completion
        if spent is None:
            self.unpriced += 1
        else:
            self.cost += spent

    def out(self) -> dict[str, Any]:
        return {"records": self.calls, "prompt_tokens": self.prompt,
                "completion_tokens": self.completion,
                "total_tokens": self.prompt + self.completion,
                "cost_usd": round(self.cost, 4),
                "cost_complete": self.unpriced == 0}


def summarise(rows: Iterable[dict[str, Any]], table: dict[str, dict[str, float]]
              ) -> dict[str, Any]:
    """Aggregate usage rows: {stage, model, prompt, completion, day, document_id,
    filename, extraction_id, pages, status}."""
    total, by_model, by_stage, by_day = _Tally(), defaultdict(_Tally), defaultdict(_Tally), \
        defaultdict(_Tally)
    runs: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        spent = cost(row["model"], row["prompt"], row["completion"], table)
        for tally in (total, by_model[row["model"]], by_stage[row["stage"]], by_day[row["day"]]):
            tally.add(row["prompt"], row["completion"], spent)
        doc = documents.setdefault(row["document_id"], {
            "document_id": row["document_id"], "filename": row.get("filename"),
            "runs": set(), "tokens": 0, "cost_usd": 0.0})
        doc["tokens"] += row["prompt"] + row["completion"]
        doc["cost_usd"] += spent or 0.0
        if row.get("extraction_id"):
            doc["runs"].add(row["extraction_id"])
            run = runs.setdefault(row["extraction_id"], {
                "extraction_id": row["extraction_id"], "document_id": row["document_id"],
                "filename": row.get("filename"), "status": row.get("status"),
                "pages": row.get("pages"), "tokens": 0, "cost_usd": 0.0})
            run["tokens"] += row["prompt"] + row["completion"]
            run["cost_usd"] += spent or 0.0
            if row.get("pages"):
                run["pages"] = row["pages"]

    paged = [r for r in runs.values() if r.get("pages")]
    pages = sum(r["pages"] for r in paged)
    failed = [r for r in runs.values() if r.get("status") in ("failed", "cancelled")]
    n_docs = len(documents)
    reprocessed = [d for d in documents.values() if len(d["runs"]) > 1]
    for d in documents.values():
        d["runs"] = len(d["runs"])
        d["cost_usd"] = round(d["cost_usd"], 4)
    for r in runs.values():
        r["cost_usd"] = round(r["cost_usd"], 4)
    totals = total.out()
    return {
        "totals": totals,
        "by_model": {k: v.out() for k, v in sorted(by_model.items())},
        "by_stage": {k: by_stage[k].out() for k in STAGES if k in by_stage},
        "by_day": {k: v.out() for k, v in sorted(by_day.items())},
        "averages": {
            "documents": n_docs,
            "runs": len(runs),
            "pages": pages,
            "tokens_per_document": round(totals["total_tokens"] / n_docs) if n_docs else None,
            "cost_per_document_usd": round(totals["cost_usd"] / n_docs, 4) if n_docs else None,
            "tokens_per_page": (round(sum(r["tokens"] for r in paged) / pages) if pages else None),
            "cost_per_page_usd": (round(sum(r["cost_usd"] for r in paged) / pages, 5)
                                  if pages else None),
        },
        # Spend that produced nothing a reviewer could use, and re-reads of the
        # same document: the two places savings are most likely.
        "waste": {
            "failed_runs": len(failed),
            "failed_tokens": sum(r["tokens"] for r in failed),
            "failed_cost_usd": round(sum(r["cost_usd"] for r in failed), 4),
            "documents_read_more_than_once": len(reprocessed),
            "extra_runs": sum(d["runs"] - 1 for d in reprocessed),
        },
        "top_runs": sorted(runs.values(), key=lambda r: r["tokens"], reverse=True)[:10],
        "unpriced_models": sorted(m for m, t in by_model.items() if t.unpriced),
    }


async def build(since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    """The usage report for runs and splits created in [since, until)."""
    rows: list[dict[str, Any]] = []
    async with db.session() as session:
        names = {}
        stmt = (select(db.Extraction, db.Document.filename)
                .join(db.Document, db.Document.id == db.Extraction.document_id)
                .where(db.Extraction.created_at >= since, db.Extraction.created_at < until)
                .execution_options(yield_per=100))
        async for extraction, filename in await session.stream(stmt):
            names[extraction.document_id] = filename
            meta = ((extraction.snapshot or {}).get("meta") or {})
            pages = meta.get("pages") if isinstance(meta.get("pages"), int) else None
            day = extraction.created_at.date().isoformat()
            base = {"day": day, "document_id": extraction.document_id, "filename": filename,
                    "extraction_id": extraction.id, "pages": pages, "status": extraction.status}
            for stage, usage in (("header", meta.get("header_usage")), ("form", extraction.usage)):
                for model, prompt, completion in entries(usage):
                    rows.append({**base, "stage": stage, "model": model, "prompt": prompt,
                                 "completion": completion})
            session.expunge(extraction)  # keep the big snapshots from piling up

        splits = (await session.execute(
            select(db.Split, db.Document.filename)
            .join(db.Document, db.Document.id == db.Split.document_id)
            .where(db.Split.created_at >= since, db.Split.created_at < until))).all()
        for split, filename in splits:
            for model, prompt, completion in entries(split.usage):
                rows.append({"day": split.created_at.date().isoformat(),
                             "document_id": split.document_id, "filename": filename,
                             "extraction_id": None, "pages": None, "status": None,
                             "stage": "split", "model": model, "prompt": prompt,
                             "completion": completion})

    table = prices()
    report = summarise(rows, table)
    report["period"] = {"since": since.isoformat(), "until": until.isoformat()}
    report["priced"] = bool(table)
    return report
