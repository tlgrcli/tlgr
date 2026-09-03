"""Job runner — manages lifecycle of background jobs."""

from __future__ import annotations

import logging
from typing import Any

from tlgr.core.client import ClientWrapper
from tlgr.daemon.webhook import WebhookPusher
from tlgr.gateway.config import GatewayConfig
from tlgr.gateway.engine import Gateway
from tlgr.jobs.base import BaseJob

log = logging.getLogger("tlgr.daemon.jobs")


class JobRunner:
    def __init__(self):
        self._jobs: dict[str, BaseJob] = {}

    def create_job(
        self,
        config: GatewayConfig,
        client: ClientWrapper,
        webhook: WebhookPusher | None = None,
        bus: Any = None,
    ) -> BaseJob:
        job = Gateway(config, client, webhook, bus)
        self._jobs[config.name] = job
        return job

    async def start_all(self) -> None:
        for name, job in self._jobs.items():
            if job.enabled:
                job.start()
                log.info("Started job: %s", name)

    async def stop_all(self) -> None:
        for name, job in self._jobs.items():
            await job.stop()
            log.info("Stopped job: %s", name)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [j.status() for j in self._jobs.values()]

    async def remove_job(self, name: str) -> bool:
        job = self._jobs.pop(name, None)
        if job is None:
            return False
        await job.stop()
        return True

    async def enable_job(self, name: str) -> bool:
        job = self._jobs.get(name)
        if job is None:
            return False
        if not job.enabled:
            job.enabled = True
            job.start()
        return True

    async def disable_job(self, name: str) -> bool:
        job = self._jobs.get(name)
        if job is None:
            return False
        job.enabled = False
        await job.stop()
        return True
