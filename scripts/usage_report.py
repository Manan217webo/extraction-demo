"""Token use and cost of the AI reads over a period.

    uv run scripts/usage_report.py              # last 30 days
    uv run scripts/usage_report.py --days 90
    uv run scripts/usage_report.py --json > usage.json

Prices come from TOKEN_PRICES (US dollars per million tokens, by model); see
usage_report.py. The same report is served to administrators at
GET /api/admin/usage?days=30.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import usage_report  # noqa: E402


def _money(value, complete=True) -> str:
    if value is None:
        return "—"
    return f"${value:,.4f}" + ("" if complete else " (partial)")


def _table(title: str, rows: dict) -> None:
    print(f"\n{title}")
    print(f"  {'':28} {'records':>8} {'input':>14} {'output':>12} {'cost':>18}")
    for name, t in rows.items():
        print(f"  {name[:28]:28} {t['records']:>8} {t['prompt_tokens']:>14,} "
              f"{t['completion_tokens']:>12,} {_money(t['cost_usd'], t['cost_complete']):>18}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()

    until = dt.datetime.now(dt.timezone.utc)
    report = asyncio.run(usage_report.build(until - dt.timedelta(days=args.days), until))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    t, avg, waste = report["totals"], report["averages"], report["waste"]
    print(f"AI token use, last {args.days} days")
    print(f"  Total: {t['total_tokens']:,} tokens ({t['prompt_tokens']:,} in, "
          f"{t['completion_tokens']:,} out) · {_money(t['cost_usd'], t['cost_complete'])}")
    print(f"  {avg['documents']} documents, {avg['runs']} runs, {avg['pages']} pages")
    print(f"  Per document: {avg['tokens_per_document'] or '—'} tokens · "
          f"{_money(avg['cost_per_document_usd'])}")
    print(f"  Per page:     {avg['tokens_per_page'] or '—'} tokens · "
          f"{_money(avg['cost_per_page_usd'])}")
    _table("By model", report["by_model"])
    _table("By stage", report["by_stage"])
    print("\nWhere it may be saved")
    print(f"  Failed or cancelled runs: {waste['failed_runs']} "
          f"({waste['failed_tokens']:,} tokens, {_money(waste['failed_cost_usd'])})")
    print(f"  Documents read more than once: {waste['documents_read_more_than_once']} "
          f"({waste['extra_runs']} extra runs)")
    if report["top_runs"]:
        print("\nLargest runs")
        for r in report["top_runs"]:
            print(f"  {r['tokens']:>10,} tokens  {_money(r['cost_usd']):>10}  "
                  f"{r.get('pages') or '?':>3} pages  {r.get('filename') or r['document_id']}")
    if report["unpriced_models"]:
        print("\nNo price in TOKEN_PRICES for: " + ", ".join(report["unpriced_models"]))


if __name__ == "__main__":
    main()
