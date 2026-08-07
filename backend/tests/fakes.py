"""Shared test doubles for aria2 RPC client.

Instead of constructing ``AsyncMock()`` and manually setting attributes in every
test file, import :func:`make_aria2_client` for a one-liner with sensible
defaults.  Every parameter accepts three kinds of value:

* ``str`` / ``dict`` / ``list`` → ``return_value``
* ``list`` → ``side_effect`` (responses consumed in order)
* ``Exception`` (or subclass) → ``side_effect`` (raises immediately)

Example::

    client = make_aria2_client(add_uri="gid-1")
    client = make_aria2_client(
        tell_status=[
            {"status": "paused", "totalLength": "0"},
            {"status": "active", "totalLength": "100"},
        ],
    )
    client = make_aria2_client(force_remove=RuntimeError("gid not found"))
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.aria2.client import Aria2Client

_DEFAULT_TELL_STATUS: dict[str, Any] = {
    "gid": "test_gid",
    "status": "active",
    "totalLength": "1000000",
    "completedLength": "0",
    "uploadLength": "0",
    "downloadSpeed": "10000",
    "uploadSpeed": "0",
    "connections": "1",
    "numSeeders": "0",
    "seeder": "false",
    "pieceLength": "0",
    "numPieces": "0",
    "bittorrent": {"info": {}},
    "infoHash": "",
    "dir": "",
    "files": [],
    "following": "",
    "followedBy": [],
    "belongsTo": "",
    "errorCode": "0",
    "errorMessage": "",
}

_DEFAULT_ADD_URI = "test_gid"

_DEFAULT_OK = "OK"


def _apply(
    mock_method: AsyncMock,
    value: Any,
    default: Any,
    *,
    list_as_side_effect: bool = True,
) -> None:
    """Configure *mock_method* based on the type of *value*.

    When *list_as_side_effect* is ``False`` (used for ``tell_active`` etc.),
    a ``list`` value is set as ``return_value`` instead of ``side_effect``.
    """
    if value is ... or value is None:
        if isinstance(default, Exception) or (
            isinstance(default, type) and issubclass(default, Exception)
        ):
            mock_method.side_effect = default
        else:
            mock_method.return_value = default
    elif isinstance(value, Exception) or (
        isinstance(value, type) and issubclass(value, Exception)
    ):
        mock_method.side_effect = value
    elif isinstance(value, list) and list_as_side_effect:
        mock_method.side_effect = value
    elif callable(value):
        mock_method.side_effect = value
    else:
        mock_method.return_value = value


def make_aria2_client(
    *,
    add_uri: Any = ...,
    add_torrent: Any = ...,
    tell_status: Any = ...,
    tell_active: Any = ...,
    tell_waiting: Any = ...,
    tell_stopped: Any = ...,
    pause: Any = ...,
    unpause: Any = ...,
    force_remove: Any = ...,
    remove: Any = ...,
    remove_download_result: Any = ...,
    change_option: Any = ...,
    change_position: Any = ...,
    get_global_stat: Any = ...,
    get_files: Any = ...,
    get_uris: Any = ...,
    get_peers: Any = ...,
    get_servers: Any = ...,
    get_version: Any = ...,
) -> AsyncMock:
    """Return a spec-typed ``AsyncMock`` mimicking :class:`Aria2Client`.

    All parameters default to ``...`` (Ellipsis), meaning "use the built-in
    default".  Pass ``None`` to leave a method as a bare ``AsyncMock`` with no
    pre-set return value.
    """
    client = AsyncMock(spec=Aria2Client)

    _apply(client.add_uri, add_uri, _DEFAULT_ADD_URI)
    _apply(client.add_torrent, add_torrent, _DEFAULT_ADD_URI)
    _apply(client.tell_status, tell_status, _DEFAULT_TELL_STATUS)
    _apply(client.tell_active, tell_active, [], list_as_side_effect=False)
    _apply(client.tell_waiting, tell_waiting, [], list_as_side_effect=False)
    _apply(client.tell_stopped, tell_stopped, [], list_as_side_effect=False)
    _apply(client.pause, pause, _DEFAULT_OK)
    _apply(client.unpause, unpause, _DEFAULT_OK)
    _apply(client.force_remove, force_remove, _DEFAULT_OK)
    _apply(client.remove, remove, _DEFAULT_OK)
    _apply(client.remove_download_result, remove_download_result, _DEFAULT_OK)
    _apply(client.change_option, change_option, _DEFAULT_OK)
    _apply(client.change_position, change_position, 0)
    _apply(client.get_global_stat, get_global_stat, {})
    _apply(client.get_files, get_files, [])
    _apply(client.get_uris, get_uris, [])
    _apply(client.get_peers, get_peers, [])
    _apply(client.get_servers, get_servers, [])
    _apply(client.get_version, get_version, {"version": "1.36.0"})

    return client


# ---------------------------------------------------------------------------
# aiohttp mock factories
# ---------------------------------------------------------------------------


def make_aiohttp_response(
    status: int = 200,
    headers: dict[str, str] | None = None,
    url: str | None = None,
    reason: str | None = None,
) -> MagicMock:
    """Return a mock ``aiohttp.ClientResponse``.

    Only attributes read by the HTTP probe are set.  Unspecified keyword
    arguments are omitted so the mock stays minimal.

    Example::

        resp = make_aiohttp_response(
            status=302,
            url="https://example.com/start",
            headers={"Location": "/files/final.zip"},
        )
    """
    response = MagicMock()
    response.status = status
    response.headers = headers if headers is not None else {}
    if url is not None:
        response.url = url
    if reason is not None:
        response.reason = reason
    return response


def make_aiohttp_session(
    *responses: Any,
    method: str = "head",
) -> MagicMock:
    """Return a mock ``aiohttp.ClientSession``.

    Each item in *responses* configures one sequential return of
    ``session.<method>()``.  Response mocks are wrapped in an async context
    manager; exception instances are raised directly as ``side_effect``.

    *method* selects ``session.head`` (default) or ``session.get``.

    Example::

        # single response
        session = make_aiohttp_session(response)

        # redirect chain
        session = make_aiohttp_session(redirect, final)

        # GET fallback that raises
        session = make_aiohttp_session(RuntimeError("boom"), method="get")
    """
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    if not responses:
        return session

    prepared: list[Any] = []
    for item in responses:
        if isinstance(item, BaseException):
            prepared.append(item)
        else:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=item)
            ctx.__aexit__ = AsyncMock(return_value=None)
            prepared.append(ctx)

    if len(prepared) == 1:
        item = prepared[0]
        if isinstance(item, BaseException):
            setattr(session, method, MagicMock(side_effect=item))
        else:
            setattr(session, method, MagicMock(return_value=item))
    else:
        setattr(session, method, MagicMock(side_effect=prepared))

    return session
