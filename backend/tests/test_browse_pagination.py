"""目录浏览分页（files 与 shares 两个入口）的 SQL 层分页行为。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db.engine import transaction
from app.db.schema import stored_file_entries, stored_files, user_files
from app.repositories import files as files_repo

TIMESTAMP = 1_700_000_000_000


def _sort_key(parent_path: str, is_dir: bool, name: str) -> str:
    return f"{parent_path}\0{'0' if is_dir else '1'}\0{name.lower()}"


def _entry(
    stored_file_id: int,
    relative_path: str,
    parent_path: str,
    name: str,
    *,
    is_dir: bool,
    size: int = 1,
) -> dict:
    return {
        "stored_file_id": stored_file_id,
        "relative_path": relative_path,
        "parent_path": parent_path,
        "name": name,
        "size_bytes": size,
        "is_dir": 1 if is_dir else 0,
        "mtime_ms": TIMESTAMP,
        "sort_key": _sort_key(parent_path, is_dir, name),
    }


def _seed_archive(
    test_user: dict,
    *,
    content_hash: str,
    file_count: int,
    dir_names: tuple[str, ...] = (),
    nested_count: int = 0,
) -> None:
    """造一个入库目录：顶层 file_count 个文件 + dir_names 目录，首个目录下嵌套 nested_count 个文件。"""

    def seed() -> None:
        async def run() -> None:
            async with transaction() as conn:
                stored = (
                    (
                        await conn.execute(
                            insert(stored_files)
                            .values(
                                content_hash=content_hash,
                                real_path=f"/tmp/store/{content_hash}",
                                size_bytes=0,
                                is_directory=1,
                                original_name=content_hash,
                                created_at_ms=TIMESTAMP,
                            )
                            .returning(stored_files)
                        )
                    )
                    .mappings()
                    .one()
                )
                sid = int(stored["id"])
                await conn.execute(
                    insert(user_files).values(
                        user_id=test_user["id"],
                        stored_file_id=sid,
                        display_name=content_hash,
                        created_at_ms=TIMESTAMP,
                        updated_at_ms=TIMESTAMP,
                    )
                )
                entries = [
                    _entry(sid, ".", "", ".", is_dir=True, size=0),
                ]
                entries.extend(
                    _entry(
                        sid,
                        f"file-{i:05d}.txt",
                        "",
                        f"file-{i:05d}.txt",
                        is_dir=False,
                    )
                    for i in range(file_count)
                )
                for dir_name in dir_names:
                    entries.append(
                        _entry(sid, dir_name, "", dir_name, is_dir=True, size=0)
                    )
                    if dir_name == dir_names[0]:
                        entries.extend(
                            _entry(
                                sid,
                                f"{dir_name}/nested-{i:03d}.txt",
                                dir_name,
                                f"nested-{i:03d}.txt",
                                is_dir=False,
                            )
                            for i in range(nested_count)
                        )
                await conn.execute(insert(stored_file_entries), entries)

        asyncio.run(run())

    seed()


def _seed_empty_archive(test_user: dict, content_hash: str) -> None:
    _seed_archive(test_user, content_hash=content_hash, file_count=0)


class TestRepositoryPagination:
    async def test_directory_entries_page_caps_rows_at_limit(self, temp_db: str) -> None:
        """2 万条目目录下，repository 层返回条数必须等于 limit（SQL 层 LIMIT，而非全量取行）。"""
        entries = [
            {
                "relative_path": f"file-{i:05d}.txt",
                "parent_path": "",
                "name": f"file-{i:05d}.txt",
                "size_bytes": 1,
                "is_dir": 0,
                "mtime_ms": TIMESTAMP,
                "sort_key": _sort_key("", False, f"file-{i:05d}.txt"),
            }
            for i in range(20_000)
        ]
        row, _ = await files_repo.create_stored_file_with_entries(
            {
                "content_hash": "big_flat_hash",
                "real_path": "/tmp/big_flat",
                "size_bytes": 0,
                "is_directory": 1,
                "original_name": "big_flat",
                "created_at_ms": TIMESTAMP,
            },
            entries,
        )
        sid = int(row["id"])

        parent_is_dir, first_page, total = await files_repo.directory_entries_page(
            sid, "", limit=200, offset=0
        )
        assert parent_is_dir is True
        assert len(first_page) == 200
        assert total == 20_000

        _, tail_page, tail_total = await files_repo.directory_entries_page(
            sid, "", limit=200, offset=19_900
        )
        assert len(tail_page) == 100
        assert tail_total == 20_000
        assert tail_page[0]["name"] == "file-19900.txt"

    async def test_directory_entries_page_survives_huge_offset(self, temp_db: str) -> None:
        """超大 page 的 (page-1)*page_size 不得击穿 SQLite int64 导致 500。"""
        entries = [
            {
                "relative_path": f"f-{i:03d}.txt",
                "parent_path": "",
                "name": f"f-{i:03d}.txt",
                "size_bytes": 1,
                "is_dir": 0,
                "mtime_ms": TIMESTAMP,
                "sort_key": _sort_key("", False, f"f-{i:03d}.txt"),
            }
            for i in range(5)
        ]
        row, _ = await files_repo.create_stored_file_with_entries(
            {
                "content_hash": "huge_offset_hash",
                "real_path": "/tmp/huge_offset",
                "size_bytes": 0,
                "is_directory": 1,
                "original_name": "huge_offset",
                "created_at_ms": TIMESTAMP,
            },
            entries,
        )
        # page=10^15、page_size=200 → offset=2e17，远超 int64 也要安全返回空页
        parent_is_dir, items, total = await files_repo.directory_entries_page(
            int(row["id"]), "", limit=200, offset=(10**15 - 1) * 200
        )
        assert parent_is_dir is True
        assert items == []
        assert total == 5

    async def test_directory_entries_unbounded_function_removed(self) -> None:
        """防止无上限目录查询路径复活。"""
        assert not hasattr(files_repo, "directory_entries")


class TestFilesBrowsePagination:
    def test_browse_empty_directory(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_empty_archive(test_user, "empty_dir_hash")
        response = authenticated_client.get("/api/files/empty_dir_hash/browse")
        assert response.status_code == 200
        data = response.json()
        assert data == {"items": [], "total": 0, "page": 1, "page_size": 200}

    def test_browse_single_page_directory(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(test_user, content_hash="small_dir_hash", file_count=150)
        response = authenticated_client.get("/api/files/small_dir_hash/browse")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 150
        assert data["total"] == 150
        assert data["page"] == 1
        assert data["page_size"] == 200

    def test_browse_multi_page_stable_order(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(
            test_user,
            content_hash="paged_dir_hash",
            file_count=448,
            dir_names=("zdir", "adir"),
        )
        collected: list[str] = []
        for page in (1, 2, 3):
            response = authenticated_client.get(
                "/api/files/paged_dir_hash/browse", params={"page": page}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 450
            assert data["page_size"] == 200
            assert len(data["items"]) == (200, 200, 50)[page - 1]
            collected.extend(item["name"] for item in data["items"])
        # 目录在前，其后文件按名稳定排序，翻页不重不漏
        assert collected[:2] == ["adir", "zdir"]
        assert collected[2:] == [f"file-{i:05d}.txt" for i in range(448)]
        assert len(set(collected)) == 450

    def test_browse_out_of_range_page_returns_empty_items_with_total(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(test_user, content_hash="oob_dir_hash", file_count=10)
        response = authenticated_client.get(
            "/api/files/oob_dir_hash/browse", params={"page": 99}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 10

    def test_browse_page_size_clamped_to_max(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(test_user, content_hash="clamp_dir_hash", file_count=450)
        response = authenticated_client.get(
            "/api/files/clamp_dir_hash/browse", params={"page_size": 999}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page_size"] == 200
        assert len(data["items"]) == 200
        assert data["total"] == 450

    def test_browse_page_size_clamped_to_min(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(test_user, content_hash="min_dir_hash", file_count=3)
        response = authenticated_client.get(
            "/api/files/min_dir_hash/browse", params={"page_size": 0}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page_size"] == 1
        assert len(data["items"]) == 1

    def test_browse_page_below_one_clamped(
        self, authenticated_client: TestClient, test_user: dict, temp_db: str
    ) -> None:
        _seed_archive(test_user, content_hash="page0_dir_hash", file_count=3)
        response = authenticated_client.get(
            "/api/files/page0_dir_hash/browse", params={"page": 0}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 1
        assert len(data["items"]) == 3


class TestShareBrowsePagination:
    def test_share_browse_pagination_contract(
        self, authenticated_client: TestClient, client: TestClient,
        test_user: dict, temp_db: str,
    ) -> None:
        _seed_archive(test_user, content_hash="share_paged_hash", file_count=5)
        user_file_id = None

        async def fetch_user_file_id() -> int:
            async with transaction() as conn:
                row = (
                    await conn.execute(
                        stored_files.select().where(
                            stored_files.c.content_hash == "share_paged_hash"
                        )
                    )
                ).mappings().one()
                user_row = (
                    await conn.execute(
                        user_files.select().where(
                            user_files.c.stored_file_id == row["id"]
                        )
                    )
                ).mappings().one()
                return int(user_row["id"])

        user_file_id = asyncio.run(fetch_user_file_id())
        share = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file_id}
        ).json()
        url = f"/api/s/{share['share_code']}/browse"

        first = client.get(url, params={"page": 1, "page_size": 2})
        assert first.status_code == 200
        data = first.json()
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert [item["name"] for item in data["items"]] == [
            "file-00000.txt",
            "file-00001.txt",
        ]

        third = client.get(url, params={"page": 3, "page_size": 2})
        assert third.status_code == 200
        assert [item["name"] for item in third.json()["items"]] == ["file-00004.txt"]

        out_of_range = client.get(url, params={"page": 9, "page_size": 2})
        assert out_of_range.status_code == 200
        assert out_of_range.json()["items"] == []
        assert out_of_range.json()["total"] == 5

    def test_share_browse_page_size_clamped(
        self, authenticated_client: TestClient, client: TestClient,
        test_user: dict, temp_db: str,
    ) -> None:
        _seed_archive(test_user, content_hash="share_clamp_hash", file_count=450)
        async def fetch_user_file_id() -> int:
            async with transaction() as conn:
                row = (
                    await conn.execute(
                        stored_files.select().where(
                            stored_files.c.content_hash == "share_clamp_hash"
                        )
                    )
                ).mappings().one()
                user_row = (
                    await conn.execute(
                        user_files.select().where(
                            user_files.c.stored_file_id == row["id"]
                        )
                    )
                ).mappings().one()
                return int(user_row["id"])

        user_file_id = asyncio.run(fetch_user_file_id())
        share = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file_id}
        ).json()
        response = client.get(
            f"/api/s/{share['share_code']}/browse", params={"page_size": 10_000}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["page_size"] == 200
        assert len(data["items"]) == 200
        assert data["total"] == 450

    def test_share_browse_subpath_paginates(
        self, authenticated_client: TestClient, client: TestClient,
        test_user: dict, temp_db: str,
    ) -> None:
        _seed_archive(
            test_user,
            content_hash="share_nested_hash",
            file_count=1,
            dir_names=("nested",),
            nested_count=5,
        )
        async def fetch_user_file_id() -> int:
            async with transaction() as conn:
                row = (
                    await conn.execute(
                        stored_files.select().where(
                            stored_files.c.content_hash == "share_nested_hash"
                        )
                    )
                ).mappings().one()
                user_row = (
                    await conn.execute(
                        user_files.select().where(
                            user_files.c.stored_file_id == row["id"]
                        )
                    )
                ).mappings().one()
                return int(user_row["id"])

        user_file_id = asyncio.run(fetch_user_file_id())
        share = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file_id}
        ).json()
        response = client.get(
            f"/api/s/{share['share_code']}/browse",
            params={"subpath": "nested", "page": 2, "page_size": 2},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert [item["name"] for item in data["items"]] == [
            "nested-002.txt",
            "nested-003.txt",
        ]
