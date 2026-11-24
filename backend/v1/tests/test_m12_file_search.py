"""M12 Task 3: GET /api/files/search + 根列表稳定序（T1-T12c / T23 / T23b / T24 / T26）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.core.rate_limit_config import rate_limit_config
from app.db.engine import transaction
from app.db.schema import stored_file_entries, stored_files, user_files
from app.main import app
from app.services import file_service
from tests.helpers_v0 import create_session_v0, create_user_v0, now_ms


@pytest.fixture(autouse=True)
def pin_file_search_limit():
    original = rate_limit_config.file_search
    rate_limit_config.file_search = 20
    yield
    rate_limit_config.file_search = original


def entry(
    relative_path: str,
    name: str,
    *,
    is_dir: bool = False,
    parent_path: str = "",
    size_bytes: int = 5,
) -> dict[str, Any]:
    return {
        "relative_path": relative_path,
        "parent_path": parent_path,
        "name": name,
        "size_bytes": size_bytes,
        "is_dir": 1 if is_dir else 0,
        "mtime_ms": 1_700_000_000_000,
        "sort_key": f"{'0' if is_dir else '1'}:{name}",
    }


def seed_user_file(
    user_id: int,
    *,
    content_hash: str,
    display_name: str,
    entries: list[dict[str, Any]] | None = None,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    """插入 stored_files + user_files（可选 stored_file_entries），返回关键标识。"""

    async def _seed() -> dict[str, Any]:
        timestamp = created_at_ms if created_at_ms is not None else now_ms()
        real_path = Path(settings.download_dir) / "store" / content_hash
        async with transaction() as conn:
            stored = (
                (
                    await conn.execute(
                        insert(stored_files)
                        .values(
                            content_hash=content_hash,
                            real_path=str(real_path),
                            size_bytes=10,
                            is_directory=1 if entries else 0,
                            original_name=display_name,
                            created_at_ms=timestamp,
                        )
                        .returning(stored_files)
                    )
                )
                .mappings()
                .one()
            )
            user_file = (
                (
                    await conn.execute(
                        insert(user_files)
                        .values(
                            user_id=user_id,
                            stored_file_id=stored["id"],
                            display_name=display_name,
                            created_at_ms=timestamp,
                            updated_at_ms=timestamp,
                        )
                        .returning(user_files)
                    )
                )
                .mappings()
                .one()
            )
            if entries:
                await conn.execute(
                    insert(stored_file_entries),
                    [{"stored_file_id": stored["id"], **item} for item in entries],
                )
        return {
            "user_file_id": int(user_file["id"]),
            "content_hash": str(stored["content_hash"]),
            "stored_file_id": int(stored["id"]),
            "display_name": display_name,
        }

    return asyncio.run(_seed())


def client_for_session(session_id: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session_id)
    return client


def search(
    client: TestClient, q: str, **params: Any
) -> dict[str, Any]:
    resp = client.get("/api/files/search", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSearchValidation:
    def test_t1_empty_or_blank_q_returns_400(
        self, authenticated_client: TestClient, test_user: dict
    ):
        for q in ("", "   "):
            resp = authenticated_client.get("/api/files/search", params={"q": q})
            assert resp.status_code == 400
            assert resp.json()["detail"] == "请输入关键词"
        resp = authenticated_client.get("/api/files/search")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "请输入关键词"

    def test_t8_unauthenticated_401(self, client: TestClient, temp_db: str):
        resp = client.get("/api/files/search", params={"q": "x"})
        assert resp.status_code == 401

    def test_t11_route_exists(self, authenticated_client: TestClient, temp_db: str):
        resp = authenticated_client.get("/api/files/search", params={"q": "x"})
        assert resp.status_code != 404
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0, "truncated": False}

    def test_t5b_invalid_scope_path_400_without_quota(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"],
            content_hash="t5b-pkg",
            display_name="包",
            entries=[entry("a.txt", "a.txt")],
        )
        rate_limit_config.file_search = 1
        for bad in ("../escape", "/abs/path", ".."):
            resp = authenticated_client.get(
                "/api/files/search",
                params={"q": "a", "scope_content_hash": "t5b-pkg", "scope_path": bad},
            )
            assert resp.status_code == 400
        assert search(authenticated_client, "a")["total"] == 1


class TestSearchMatching:
    def test_t2_trim_and_casefold(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(test_user["id"], content_hash="t2-cn", display_name="女王驾到.zip")
        seed_user_file(test_user["id"], content_hash="t2-en", display_name="Queen.zip")
        assert [i["name"] for i in search(authenticated_client, " 女王 ")["items"]] == [
            "女王驾到.zip"
        ]
        assert [i["name"] for i in search(authenticated_client, "qUeEn")["items"]] == [
            "Queen.zip"
        ]

    def test_t3_second_page_file_searchable(
        self, authenticated_client: TestClient, test_user: dict
    ):
        base = now_ms()
        for i in range(11):
            seed_user_file(
                test_user["id"],
                content_hash=f"t3-f{i}",
                display_name=f"分页文件{i:02d}.zip",
                created_at_ms=base + i,
            )
        page2 = authenticated_client.get("/api/files", params={"page": 2})
        assert page2.status_code == 200
        assert [f["name"] for f in page2.json()["files"]] == ["分页文件00.zip"]
        items = search(authenticated_client, "分页文件00")["items"]
        assert [i["name"] for i in items] == ["分页文件00.zip"]
        assert items[0]["root_index"] == 10

    def test_t4_internal_entry_hit(
        self, authenticated_client: TestClient, test_user: dict
    ):
        pkg = seed_user_file(
            test_user["id"],
            content_hash="t4-pkg",
            display_name="剧集包",
            entries=[
                entry(".", "剧集包", is_dir=True, size_bytes=0),
                entry("第一季", "第一季", is_dir=True, size_bytes=0),
                entry("第一季/秘密档案.mp4", "秘密档案.mp4", parent_path="第一季"),
                entry("其他.txt", "其他.txt"),
            ],
        )
        items = search(authenticated_client, "秘密")["items"]
        assert len(items) == 1
        item = items[0]
        assert item["user_file_id"] == pkg["user_file_id"]
        assert item["content_hash"] == "t4-pkg"
        assert item["name"] == "秘密档案.mp4"
        assert item["entry_path"] == "第一季/秘密档案.mp4"
        assert item["path"] == "/剧集包/第一季/秘密档案.mp4"
        assert item["is_directory"] is False
        assert item["rank"] == 0
        assert item["root_index"] == 0
        assert "created_at" not in item

    def test_t5_scope_semantics(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(test_user["id"], content_hash="t5-top", display_name="needle_top.txt")
        seed_user_file(
            test_user["id"],
            content_hash="t5-a",
            display_name="包A",
            entries=[entry("needle_a.txt", "needle_a.txt")],
        )
        seed_user_file(
            test_user["id"],
            content_hash="t5-b",
            display_name="包B",
            entries=[entry("needle_b.txt", "needle_b.txt")],
        )
        scoped = search(authenticated_client, "needle", scope_content_hash="t5-a")
        assert [i["path"] for i in scoped["items"]] == ["/包A/needle_a.txt"]
        global_result = search(authenticated_client, "needle")
        assert {i["path"] for i in global_result["items"]} == {
            "/needle_top.txt",
            "/包A/needle_a.txt",
            "/包B/needle_b.txt",
        }

    def test_t5_same_display_name_packages_do_not_mix(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"],
            content_hash="t5-dup-1",
            display_name="同名包.zip",
            entries=[entry("needle.txt", "needle.txt")],
        )
        second = seed_user_file(
            test_user["id"],
            content_hash="t5-dup-2",
            display_name="同名包.zip",
            entries=[entry("needle.txt", "needle.txt")],
        )
        scoped = search(
            authenticated_client, "needle", scope_content_hash=second["content_hash"]
        )
        assert len(scoped["items"]) == 1
        assert scoped["items"][0]["content_hash"] == "t5-dup-2"
        assert scoped["items"][0]["path"] == "/同名包.zip/needle.txt"
        dup_top = search(authenticated_client, "同名")
        assert {i["content_hash"] for i in dup_top["items"]} == {"t5-dup-1", "t5-dup-2"}

    def test_t5c_foreign_or_missing_hash_returns_empty(
        self, authenticated_client: TestClient, temp_db: str
    ):
        other = asyncio.run(create_user_v0(username="t5c-other"))
        seed_user_file(
            other["id"],
            content_hash="t5c-hash",
            display_name="他人包",
            entries=[entry("needle.txt", "needle.txt")],
        )
        for scope_hash in ("t5c-hash", "no-such-hash"):
            resp = authenticated_client.get(
                "/api/files/search",
                params={"q": "needle", "scope_content_hash": scope_hash},
            )
            assert resp.status_code == 200
            assert resp.json() == {"items": [], "total": 0, "truncated": False}

    def test_t5d_scope_path_narrows(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"],
            content_hash="t5d-pkg",
            display_name="混合包",
            entries=[
                entry("dirA", "dirA", is_dir=True, size_bytes=0),
                entry("dirA/needle.txt", "needle.txt", parent_path="dirA"),
                entry("dirB", "dirB", is_dir=True, size_bytes=0),
                entry("dirB/needle.txt", "needle.txt", parent_path="dirB"),
            ],
        )
        scoped = search(
            authenticated_client,
            "needle",
            scope_content_hash="t5d-pkg",
            scope_path="dirB",
        )
        assert [i["entry_path"] for i in scoped["items"]] == ["dirB/needle.txt"]

    def test_t5e_scope_path_excludes_top_level_package_name(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"],
            content_hash="t5e-pkg",
            display_name="needle包",
            entries=[
                entry("dirA", "dirA", is_dir=True, size_bytes=0),
                entry("dirA/needle.txt", "needle.txt", parent_path="dirA"),
            ],
        )
        scoped = search(
            authenticated_client,
            "needle",
            scope_content_hash="t5e-pkg",
            scope_path="dirA",
        )
        assert [i["path"] for i in scoped["items"]] == ["/needle包/dirA/needle.txt"]
        # scope 为包根（scope_path 为空）时，顶层包名仍可命中
        root_scoped = search(
            authenticated_client, "needle", scope_content_hash="t5e-pkg"
        )
        assert [i["path"] for i in root_scoped["items"]] == [
            "/needle包",
            "/needle包/dirA/needle.txt",
        ]

    def test_t6_rank_ordering(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"], content_hash="t6-sub", display_name="Q_u_e_e_n.zip", created_at_ms=1_000
        )
        seed_user_file(
            test_user["id"], content_hash="t6-contains", display_name="xxQueen.zip", created_at_ms=2_000
        )
        seed_user_file(
            test_user["id"], content_hash="t6-prefix", display_name="Queen.zip", created_at_ms=3_000
        )
        items = search(authenticated_client, "queen")["items"]
        assert [i["name"] for i in items] == ["Queen.zip", "xxQueen.zip", "Q_u_e_e_n.zip"]
        assert [i["rank"] for i in items] == [0, 1, 2]

    def test_t7_other_user_files_hidden(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ):
        other = asyncio.run(create_user_v0(username="t7-other"))
        mine = seed_user_file(
            test_user["id"], content_hash="t7-mine", display_name="共享名.zip"
        )
        seed_user_file(other["id"], content_hash="t7-other", display_name="共享名.zip")
        items = search(authenticated_client, "共享名")["items"]
        assert len(items) == 1
        assert items[0]["user_file_id"] == mine["user_file_id"]

    def test_t9_scope_path_ignored_without_hash(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(test_user["id"], content_hash="t9-top", display_name="t9needle.zip")
        result = search(authenticated_client, "t9needle", scope_path="foo")
        assert [i["path"] for i in result["items"]] == ["/t9needle.zip"]

    def test_t10_no_match_empty_result(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(test_user["id"], content_hash="t10", display_name="普通.zip")
        assert search(authenticated_client, "绝不存在的关键词") == {
            "items": [],
            "total": 0,
            "truncated": False,
        }

    def test_t10b_truncation(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        assert file_service.SEARCH_RESULT_LIMIT == 200
        for i in range(3):
            seed_user_file(
                test_user["id"], content_hash=f"t10b-{i}", display_name=f"批量{i:02d}.zip"
            )
        monkeypatch.setattr(file_service, "SEARCH_RESULT_LIMIT", 2)
        result = search(authenticated_client, "批量")
        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["truncated"] is True

    def test_t12c_dot_entry_not_returned(
        self, authenticated_client: TestClient, test_user: dict
    ):
        seed_user_file(
            test_user["id"],
            content_hash="t12c-pkg",
            display_name="QueenShow",
            entries=[
                entry(".", "QueenShow", is_dir=True, size_bytes=0),
                entry("queen笔记.txt", "queen笔记.txt"),
            ],
        )
        items = search(authenticated_client, "queen")["items"]
        assert all(i["entry_path"] != "." for i in items)
        assert [(i["path"], i["entry_path"]) for i in items] == [
            ("/QueenShow", None),
            ("/QueenShow/queen笔记.txt", "queen笔记.txt"),
        ]


class TestListStableOrder:
    def test_t12_list_shape_and_tie_break_by_id_desc(
        self, authenticated_client: TestClient, test_user: dict
    ):
        timestamp = 1_700_000_000_000
        first = seed_user_file(
            test_user["id"],
            content_hash="t12-old",
            display_name="同刻旧.zip",
            created_at_ms=timestamp,
        )
        second = seed_user_file(
            test_user["id"],
            content_hash="t12-new",
            display_name="同刻新.zip",
            created_at_ms=timestamp,
        )
        resp = authenticated_client.get("/api/files")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"files", "total", "space"}
        assert set(data["files"][0].keys()) == {
            "id",
            "content_hash",
            "name",
            "size",
            "is_directory",
            "created_at",
        }
        assert [f["id"] for f in data["files"]] == [
            second["user_file_id"],
            first["user_file_id"],
        ]

    def test_t12b_root_index_matches_canonical_list(
        self, authenticated_client: TestClient, test_user: dict
    ):
        base = now_ms()
        seed_user_file(
            test_user["id"], content_hash="t12b-a", display_name="甲批.zip", created_at_ms=base
        )
        seed_user_file(
            test_user["id"], content_hash="t12b-b", display_name="乙批.zip", created_at_ms=base + 5
        )
        seed_user_file(
            test_user["id"], content_hash="t12b-c", display_name="丙批.zip", created_at_ms=base + 10
        )
        listing = authenticated_client.get("/api/files").json()["files"]
        expected_index = {f["content_hash"]: idx for idx, f in enumerate(listing)}
        items = search(authenticated_client, "批")["items"]
        assert len(items) == 3
        for item in items:
            assert item["root_index"] == expected_index[item["content_hash"]]


class TestSearchRateLimit:
    def test_t23_default_limit_20_then_429(
        self, authenticated_client: TestClient, test_user: dict
    ):
        for _ in range(20):
            resp = authenticated_client.get("/api/files/search", params={"q": "x"})
            assert resp.status_code == 200
        resp = authenticated_client.get("/api/files/search", params={"q": "x"})
        assert resp.status_code == 429
        assert resp.json()["detail"] == "操作过于频繁，请稍后再试"
        assert "Retry-After" in resp.headers

    def test_t23b_buckets_are_independent(
        self, authenticated_client: TestClient, test_user: dict
    ):
        original_api = rate_limit_config.authenticated_api
        rate_limit_config.authenticated_api = 1
        try:
            assert authenticated_client.get("/api/files").status_code == 200
            assert authenticated_client.get("/api/files").status_code == 429
            resp = authenticated_client.get("/api/files/search", params={"q": "x"})
            assert resp.status_code == 200
        finally:
            rate_limit_config.authenticated_api = original_api
        asyncio.run(api_limiter.clear_all())
        rate_limit_config.file_search = 1
        try:
            assert (
                authenticated_client.get("/api/files/search", params={"q": "x"}).status_code
                == 200
            )
            assert (
                authenticated_client.get("/api/files/search", params={"q": "x"}).status_code
                == 429
            )
            assert authenticated_client.get("/api/files").status_code == 200
        finally:
            rate_limit_config.file_search = 20

    def test_t24_admin_tightens_file_search_limit(self, temp_db: str):
        admin = asyncio.run(create_user_v0(username="t24-admin", is_admin=True))
        admin_client = client_for_session(
            asyncio.run(create_session_v0(admin["id"], "t24-admin-sess"))
        )
        user = asyncio.run(create_user_v0(username="t24-user"))
        user_client = client_for_session(
            asyncio.run(create_session_v0(user["id"], "t24-user-sess"))
        )
        put = admin_client.put("/api/config", json={"rate_limit_file_search": 2})
        assert put.status_code == 200
        assert (
            user_client.get("/api/files/search", params={"q": "x"}).status_code == 200
        )
        assert (
            user_client.get("/api/files/search", params={"q": "x"}).status_code == 200
        )
        third = user_client.get("/api/files/search", params={"q": "x"})
        assert third.status_code == 429

    def test_t26_empty_q_does_not_consume_quota(
        self, authenticated_client: TestClient, test_user: dict
    ):
        for _ in range(5):
            resp = authenticated_client.get("/api/files/search", params={"q": "  "})
            assert resp.status_code == 400
        for _ in range(20):
            resp = authenticated_client.get("/api/files/search", params={"q": "x"})
            assert resp.status_code == 200
