"""Base class for background jobs."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from tlgr.daemon.webhook import WebhookPusher
from tlgr.jobs.client import JobClient

log = logging.getLogger("tlgr.jobs")


class BaseJob(ABC):
    """Base class that all background jobs inherit from."""

    def __init__(
        self,
        config: Any,
        client: JobClient,
        webhook: WebhookPusher | None = None,
    ):
        self.config = config
        self.client = client
        self.webhook = webhook
        self._task: asyncio.Task | None = None
        self.enabled = config.enabled

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def job_type(self) -> str:
        return self.config.type

    @abstractmethod
    async def setup(self) -> None:
        """Resolve chat IDs and prepare the job. Called once before start."""

    @abstractmethod
    async def run(self) -> None:
        """Run the job (register event handlers, etc.). Should run indefinitely."""

    @abstractmethod
    async def teardown(self) -> None:
        """Clean up resources."""

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run_wrapper(), name=f"job:{self.name}")
        return self._task

    async def _run_wrapper(self) -> None:
        try:
            await self.setup()
            log.info("Job '%s' started", self.name)
            await self.run()
        except asyncio.CancelledError:
            log.info("Job '%s' cancelled", self.name)
        except Exception:
            log.exception("Job '%s' crashed", self.name)
        finally:
            await self.teardown()

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.job_type,
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
        }
