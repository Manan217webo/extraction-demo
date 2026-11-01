"""The batch worker and audit sender as their own process.

    uv run worker.py

In production (docker-compose) the web containers run with WORKER_IN_PROCESS=0
and this process does the background work, so a slow batch never competes with
reviewers' requests. In development the web process runs the same loops itself
and this file isn't needed. Several copies may run: runs are claimed with a
lease and the audit sender takes a Redis lock, so nothing is done twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

load_dotenv()
os.environ["WORKER_IN_PROCESS"] = "1"  # this *is* the worker

import app  # noqa: E402,F401  (configures logging; the stages live there)
import db  # noqa: E402
import edc_audit  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

log = logging.getLogger("extraction.worker")


async def main() -> None:
    await db.init()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    pipeline.start()
    edc_audit.start()
    log.info("worker running — Ctrl-C to stop")
    await stop.wait()
    await pipeline.stop()
    await edc_audit.stop()
    await store.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
