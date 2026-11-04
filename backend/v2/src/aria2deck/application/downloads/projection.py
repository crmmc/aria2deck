from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ...domain.downloads import (
    SubscriptionStatus,
    can_retry,
    effective_task_status,
    task_status_label,
)
from .models import DownloadObservation, DownloadRecord, TaskProjection

OBSERVATION_TTL_MS = 15_000


class ObservationSource(Protocol):
    def get(self, gid: str) -> DownloadObservation | None: ...


class TaskProjector:
    """REST、RPC、WS 与统计共用的投影；仅接受 15 秒内的非未来快照。"""

    def __init__(
        self,
        *,
        observations: ObservationSource,
        clock_ms: Callable[[], int],
        observation_ttl_ms: int = OBSERVATION_TTL_MS,
    ) -> None:
        self.observations = observations
        self.clock_ms = clock_ms
        self.observation_ttl_ms = observation_ttl_ms

    def project(self, record: DownloadRecord) -> TaskProjection:
        observation = self.observations.get(record.gid)
        now_ms = self.clock_ms()
        fresh = (
            observation is not None
            and 0 <= now_ms - observation.observed_at_ms <= self.observation_ttl_ms
        )
        if not fresh:
            observation = None
        status = effective_task_status(record.subscription_status, record.attempt_status)
        is_active = status is SubscriptionStatus.ACTIVE
        ingestion_pending = record.ingestion_pending or (
            observation is not None and observation.status == "complete"
        )
        return TaskProjection(
            id=record.id,
            status=status,
            status_label=task_status_label(status, ingestion_pending=ingestion_pending),
            name=record.name,
            display_uri=record.display_uri,
            total_bytes=record.total_bytes,
            completed_bytes=record.completed_bytes,
            download_speed=observation.download_speed if observation is not None and is_active else 0,
            upload_speed=observation.upload_speed if observation is not None and is_active else 0,
            peer_count=observation.peer_count if observation is not None else 0,
            connection_count=observation.connection_count if observation is not None else 0,
            files=observation.files if observation is not None else (),
            observation_fresh=fresh,
            reserved_bytes=record.reserved_bytes,
            error_code=record.error_code,
            retryable=can_retry(status),
            created_at_ms=record.created_at_ms,
            updated_at_ms=record.updated_at_ms,
            finished_at_ms=record.finished_at_ms,
        )
