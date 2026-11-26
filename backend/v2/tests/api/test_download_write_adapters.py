from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

from fastapi.responses import StreamingResponse

from aria2deck.api.contracts import TaskActionRequest
from aria2deck.api.downloads import (
    delete_history as delete_history_handler,
)
from aria2deck.api.downloads import (
    internal_fetch as internal_fetch_handler,
)
from aria2deck.api.downloads import (
    list_history as list_history_handler,
)
from aria2deck.application.accounts.models import Principal
from aria2deck.application.downloads.gateway import DownloadGatewayService
from aria2deck.application.downloads.history import HistoryService
from aria2deck.application.downloads.models import GatewayResponse, TaskPage
from aria2deck.application.downloads.queries import DownloadQueryService
from aria2deck.domain.accounts import AccountStatus, CredentialKind, User
from aria2deck.domain.downloads import SubscriptionStatus


def principal() -> Principal:
    return Principal(
        User(1, "admin", "admin", "ab" * 32, AccountStatus.ACTIVE, True, 1000, False, False, 1, 1, 1),
        CredentialKind.SESSION,
        "session",
    )


async def test_history_api_adapters_map_requests_and_typed_responses() -> None:
    service = AsyncMock(spec=HistoryService)
    service.list_history.return_value = TaskPage([], 0, 2, 10)
    listed = await list_history_handler(
        page=2, page_size=10, status=SubscriptionStatus.FAILED, q="archive", principal=principal(), service=service,
    )
    assert listed.total == 0
    service.list_history.assert_awaited_once_with(
        user_id=1, status=SubscriptionStatus.FAILED, search="archive", page=2, page_size=10,
    )

    service.delete.return_value = []
    deleted = await delete_history_handler(
        payload=TaskActionRequest(task_ids=[1]), principal=principal(), service=service, queries=AsyncMock(spec=DownloadQueryService),
    )
    assert deleted.accepted_count == 0
    service.delete.assert_awaited_once_with(user_id=1, task_ids=[1])



async def test_internal_fetch_adapter_returns_streaming_response() -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b"ok"

    service = AsyncMock(spec=DownloadGatewayService)
    service.open.return_value = GatewayResponse(206, {"Content-Range": "bytes 0-1/2"}, body())
    response = await internal_fetch_handler(
        attempt_id=1, source_index=0, capability="capability", range_header="bytes=0-1", service=service,
    )
    assert isinstance(response, StreamingResponse)
    assert response.status_code == 206
    assert response.headers["Content-Range"] == "bytes 0-1/2"
    service.open.assert_awaited_once_with(
        attempt_id=1, source_index=0, capability="capability", range_header="bytes=0-1",
    )
