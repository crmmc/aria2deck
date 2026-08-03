from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.services.internal_fetch import (
    CAPABILITY_HEADER,
    GatewayDownloadNotFound,
    GatewayDownloadUnavailable,
    GatewaySizeExceeded,
    GatewayTargetError,
    GatewayUpstreamError,
    InvalidCapabilityError,
    InvalidRangeError,
    authorize_gateway_request,
    open_gateway_stream,
)

router = APIRouter(prefix="/_internal/fetch", include_in_schema=False)


@router.get("/{download_id}/{source_index}")
async def fetch_download(
    download_id: int,
    source_index: int,
    request: Request,
) -> StreamingResponse:
    capability = request.headers.get(CAPABILITY_HEADER)
    if not capability:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少下载凭证",
        )

    try:
        source_uri, source_options = await authorize_gateway_request(
            download_id,
            source_index,
            capability,
        )
    except GatewayDownloadNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="下载任务不存在",
        ) from exc
    except InvalidCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="下载凭证无效",
        ) from exc
    except GatewayDownloadUnavailable as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_410_GONE
                if exc.terminal
                else status.HTTP_403_FORBIDDEN
            ),
            detail="下载任务已结束" if exc.terminal else "下载任务类型无效",
        ) from exc

    try:
        stream = await open_gateway_stream(
            source_uri=source_uri,
            options=source_options,
            range_header=request.headers.get("Range"),
        )
    except InvalidRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=str(exc),
        ) from exc
    except GatewaySizeExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except GatewayTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except GatewayUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        headers=stream.headers,
        background=BackgroundTask(stream.close),
    )
