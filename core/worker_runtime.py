"""Background execution of the side-effect-free M1 simulation."""

import asyncio
import logging
import sqlite3
from uuid import uuid4

from core.task_manager import TaskManager


logger = logging.getLogger(__name__)


async def wait_for_stop(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def run_worker(manager: TaskManager, stop: asyncio.Event, *, step_seconds: float,
                     lease_seconds: float) -> None:
    worker = uuid4().hex
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            attempt = await asyncio.to_thread(manager.claim, worker, lease_seconds)
            if attempt is None:
                await wait_for_stop(stop, 0.1)
                continue
            end = loop.time() + step_seconds
            while True:
                interval = min(0.1, lease_seconds / 3, max(0, end - loop.time()))
                stopping = stop.is_set() or await wait_for_stop(stop, interval)
                keep_running = await asyncio.to_thread(
                    manager.advance, attempt, lease_seconds, release=stopping,
                    finished=not stopping and loop.time() >= end,
                )
                if not keep_running:
                    break
        except sqlite3.OperationalError:
            logger.exception("Simulated worker could not access SQLite; retrying after the polling interval")
            await wait_for_stop(stop, 0.1)
