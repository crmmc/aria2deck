from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen

import aiohttp
import pytest
import uvicorn
from aiohttp.abc import AbstractResolver
from fastapi import FastAPI

from app.routers import internal_fetch
from app.services.gateway import (
    CAPABILITY_HEADER,
    SourceRequestOptions,
    create_capability,
)
from tests.helpers_v0 import create_global_download_v0

ARIA2C = shutil.which("aria2c")


class LoopbackFixtureResolver(AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        if host != "safe.test":
            raise OSError("unexpected integration hostname")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


@contextmanager
def _serve_app(app: FastAPI) -> Iterator[str]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("integration app failed to start")
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def _rpc_call(
    rpc_url: str,
    secret: str,
    method: str,
    params: list[object],
) -> object:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "integration",
            "method": method,
            "params": [f"token:{secret}", *params],
        }
    ).encode()
    request = Request(
        rpc_url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        result = json.loads(response.read())
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    return result["result"]


def _run_aria2(uri: str, capability: str, output_dir: Path) -> str:
    port_listener = socket.socket()
    port_listener.bind(("127.0.0.1", 0))
    rpc_port = int(port_listener.getsockname()[1])
    port_listener.close()
    rpc_secret = "integration-rpc-secret"
    rpc_url = f"http://127.0.0.1:{rpc_port}/jsonrpc"
    process = subprocess.Popen(
        [
            str(ARIA2C),
            "--no-conf=true",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={rpc_port}",
            f"--rpc-secret={rpc_secret}",
            "--console-log-level=warn",
            "--summary-interval=0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                _rpc_call(rpc_url, rpc_secret, "aria2.getVersion", [])
                break
            except (OSError, URLError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    stderr = process.stderr.read() if process.stderr else ""
                    raise RuntimeError(f"aria2 RPC failed to start: {stderr}")
                time.sleep(0.05)
        options = {
            "dir": str(output_dir),
            "out": "payload",
            "header": [f"{CAPABILITY_HEADER}: {capability}"],
            "split": "1",
            "max-connection-per-server": "1",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
            "continue": "true",
            "max-tries": "1",
            "connect-timeout": "5",
            "timeout": "5",
            "file-allocation": "none",
        }
        gid = str(_rpc_call(rpc_url, rpc_secret, "aria2.addUri", [[uri], options]))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = _rpc_call(rpc_url, rpc_secret, "aria2.tellStatus", [gid])
            if isinstance(result, dict) and result.get("status") in {
                "complete",
                "error",
                "removed",
            }:
                return str(result["status"])
            time.sleep(0.05)
        raise TimeoutError("aria2 download did not reach a terminal status")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(ARIA2C is None, reason="aria2c is not installed")
def test_real_aria2_downloads_only_through_internal_gateway(
    temp_db: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    requests: list[str] = []
    upstream_headers: list[tuple[str, str | None, str | None, str | None]] = []
    trap_hits: list[str] = []

    class TrapHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            trap_hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    trap = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
    trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
    trap_thread.start()

    class UpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            requests.append(self.path)
            upstream_headers.append(
                (
                    self.path,
                    self.headers.get("X-Source"),
                    self.headers.get("Authorization"),
                    self.headers.get(CAPABILITY_HEADER),
                )
            )
            if self.path == "/redirect-safe":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://safe.test:{self.server.server_port}/payload",
                )
                self.end_headers()
                return
            if self.path == "/redirect-private":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{trap.server_port}/target",
                )
                self.end_headers()
                return
            if self.path == "/oversized":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in (b"1234", b"5678"):
                        self.wfile.write(f"{len(chunk):x}\r\n".encode())
                        self.wfile.write(chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
                return
            payload = b"real-aria2-gateway-payload"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    try:
        safe_uri = f"http://safe.test:{upstream.server_port}/redirect-safe"
        private_uri = f"http://safe.test:{upstream.server_port}/redirect-private"
        oversized_uri = f"http://safe.test:{upstream.server_port}/oversized"
        safe_download = asyncio.run(
            create_global_download_v0(
                resource_key="aria2:gateway:safe",
                resource_kind="http",
                source_uri=safe_uri,
                status="active",
            )
        )
        private_download = asyncio.run(
            create_global_download_v0(
                resource_key="aria2:gateway:private",
                resource_kind="http",
                source_uri=private_uri,
                status="active",
            )
        )
        oversized_download = asyncio.run(
            create_global_download_v0(
                resource_key="aria2:gateway:oversized",
                resource_kind="http",
                source_uri=oversized_uri,
                status="active",
            )
        )
        safe_capability = create_capability(
            int(safe_download["id"]),
            safe_uri,
            SourceRequestOptions(
                headers=(("X-Source", "expected"),),
                username="alice",
                password="password",
            ),
        )
        private_capability = create_capability(
            int(private_download["id"]), private_uri, SourceRequestOptions()
        )
        oversized_capability = create_capability(
            int(oversized_download["id"]),
            oversized_uri,
            SourceRequestOptions(),
        )
        max_size = [1024]
        monkeypatch.setattr(
            "app.services.gateway.create_public_connector",
            lambda: aiohttp.TCPConnector(
                resolver=LoopbackFixtureResolver(),
                use_dns_cache=False,
                force_close=True,
            ),
        )
        monkeypatch.setattr(
            "app.services.gateway.get_max_task_size", lambda: max_size[0]
        )
        app = FastAPI()
        app.include_router(internal_fetch.router)
        with _serve_app(app) as base_url:
            safe_output = tmp_path / "safe"
            private_output = tmp_path / "private"
            oversized_output = tmp_path / "oversized"
            safe_output.mkdir()
            private_output.mkdir()
            oversized_output.mkdir()
            safe_result = _run_aria2(
                f"{base_url}/_internal/fetch/{safe_download['id']}/0",
                safe_capability,
                safe_output,
            )
            private_result = _run_aria2(
                f"{base_url}/_internal/fetch/{private_download['id']}/0",
                private_capability,
                private_output,
            )
            max_size[0] = 5
            oversized_result = _run_aria2(
                f"{base_url}/_internal/fetch/{oversized_download['id']}/0",
                oversized_capability,
                oversized_output,
            )

        assert safe_result == "complete"
        assert (safe_output / "payload").read_bytes() == b"real-aria2-gateway-payload"
        assert private_result == "error"
        assert oversized_result == "error"
        oversized_payload = oversized_output / "payload"
        assert not oversized_payload.exists() or oversized_payload.stat().st_size <= 5
        assert requests == [
            "/redirect-safe",
            "/payload",
            "/redirect-private",
            "/oversized",
        ]
        assert upstream_headers[:2] == [
            (
                "/redirect-safe",
                "expected",
                "Basic YWxpY2U6cGFzc3dvcmQ=",
                None,
            ),
            (
                "/payload",
                "expected",
                "Basic YWxpY2U6cGFzc3dvcmQ=",
                None,
            ),
        ]
        assert trap_hits == []
    finally:
        upstream.shutdown()
        upstream.server_close()
        trap.shutdown()
        trap.server_close()
        upstream_thread.join(timeout=5)
        trap_thread.join(timeout=5)
