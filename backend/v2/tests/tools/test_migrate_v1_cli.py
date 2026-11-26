"""单元 6：CLI 契约——dry-run 零写入、目标非空拒绝、参数错误、报告文件、版本不符。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.tools.conftest import SampleLayout, checksum_tree
from tools.migrate_v1.__main__ import main


def _args(v1_sample: SampleLayout, v2_root: Path, *extra: str) -> list[str]:
    return [
        "--v1-database",
        str(v1_sample.database),
        "--v1-download-dir",
        str(v1_sample.download_dir),
        "--v2-root",
        str(v2_root),
        *extra,
    ]


def test_dry_run_writes_nothing(v1_sample: SampleLayout, tmp_path: Path, capsys) -> None:
    v2_root = tmp_path / "v2"
    before_v1 = checksum_tree(v1_sample.download_dir)

    exit_code = main(_args(v1_sample, v2_root, "--dry-run"))

    assert exit_code == 0
    assert not v2_root.exists()
    assert checksum_tree(v1_sample.download_dir) == before_v1
    output = capsys.readouterr().out
    assert "预演（--dry-run）" in output
    assert "结论: 迁移成功" in output


def test_real_run_creates_database_and_objects(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    v2_root = tmp_path / "v2"

    exit_code = main(_args(v1_sample, v2_root))

    assert exit_code == 0
    assert (v2_root / "data" / "v2.db").is_file()
    assert (v2_root / "downloads" / "objects").is_dir()
    assert "正式执行" in capsys.readouterr().out


def test_refuses_non_empty_target(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    v2_root = tmp_path / "v2"
    v2_root.mkdir()
    (v2_root / "existing.txt").write_text("do not overwrite")

    exit_code = main(_args(v1_sample, v2_root))

    assert exit_code == 1
    assert (v2_root / "existing.txt").read_text() == "do not overwrite"
    assert "非空" in capsys.readouterr().out


def test_refuses_when_v2_root_is_a_file(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    v2_root = tmp_path / "v2"
    v2_root.write_text("not a dir")

    assert main(_args(v1_sample, v2_root)) == 1
    assert "不是目录" in capsys.readouterr().out


@pytest.mark.parametrize(
    "missing",
    ["--v1-database", "--v1-download-dir", "--v2-root"],
)
def test_missing_required_argument_exits_2(missing: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main([argument for argument in _all_args(tmp_path) if not argument.startswith(missing)])

    assert raised.value.code == 2


def _all_args(tmp_path: Path) -> list[str]:
    return [
        "--v1-database",
        str(tmp_path / "v1.db"),
        "--v1-download-dir",
        str(tmp_path / "downloads"),
        "--v2-root",
        str(tmp_path / "v2"),
    ]


def test_version_mismatch_exits_1(v1_sample: SampleLayout, tmp_path: Path, capsys) -> None:
    connection = sqlite3.connect(v1_sample.database)
    try:
        connection.execute("UPDATE schema_meta SET version = 17 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()
    v2_root = tmp_path / "v2"

    exit_code = main(_args(v1_sample, v2_root))

    assert exit_code == 1
    assert not v2_root.exists()
    assert "版本" in capsys.readouterr().out


def test_missing_store_dir_exits_1(v1_sample: SampleLayout, tmp_path: Path, capsys) -> None:
    import shutil

    shutil.rmtree(v1_sample.store_dir)
    v2_root = tmp_path / "v2"

    exit_code = main(_args(v1_sample, v2_root))

    assert exit_code == 1
    assert "store/" in capsys.readouterr().out


def test_missing_database_exits_1(tmp_path: Path, capsys) -> None:
    exit_code = main(_all_args(tmp_path))

    assert exit_code == 1
    assert "不存在" in capsys.readouterr().out


def test_report_file_is_written(v1_sample: SampleLayout, tmp_path: Path, capsys) -> None:
    v2_root = tmp_path / "v2"
    report_path = tmp_path / "reports" / "migration.txt"

    exit_code = main(_args(v1_sample, v2_root, "--dry-run", "--report", str(report_path)))

    assert exit_code == 0
    content = report_path.read_text(encoding="utf-8")
    assert "V1 → V2 迁移报告" in content
    assert content == capsys.readouterr().out


def test_report_file_is_written_on_abort(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    v2_root = tmp_path / "v2"
    v2_root.mkdir()
    (v2_root / "keep.txt").write_text("x")
    report_path = tmp_path / "abort.txt"

    exit_code = main(_args(v1_sample, v2_root, "--report", str(report_path)))

    assert exit_code == 1
    assert "迁移中止" in report_path.read_text(encoding="utf-8")
    capsys.readouterr()


def test_username_conflict_aborts_without_residue(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    connection = sqlite3.connect(v1_sample.database)
    try:
        connection.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, quota_bytes, "
            "created_at_ms, updated_at_ms) VALUES (9, 'Alice', 'x', 0, 1024, 9000, 9000)"
        )
        connection.commit()
    finally:
        connection.close()
    v2_root = tmp_path / "v2"

    exit_code = main(_args(v1_sample, v2_root))

    assert exit_code == 1
    assert not v2_root.exists()
    output = capsys.readouterr().out
    assert "大小写冲突" in output
    assert "Alice" in output
    assert "alice" in output


def test_report_has_no_secrets(v1_sample: SampleLayout, tmp_path: Path) -> None:
    from tests.tools.conftest import (
        V1_RPC_SECRET,
        V1_RPC_SECRET_ENCRYPTED,
        V1_SHARE_PASSWORD_HASH,
        V1_USER_PASSWORD_HASH,
    )

    report_path = tmp_path / "report.txt"
    main(_args(v1_sample, tmp_path / "v2", "--dry-run", "--report", str(report_path)))

    text = report_path.read_text(encoding="utf-8")
    for secret in (
        V1_USER_PASSWORD_HASH,
        V1_SHARE_PASSWORD_HASH,
        V1_RPC_SECRET,
        V1_RPC_SECRET_ENCRYPTED,
    ):
        assert secret not in text


def test_help_mentions_reset_password(v1_sample: SampleLayout) -> None:
    from tools.migrate_v1.__main__ import build_parser

    text = build_parser().format_help()

    assert "--dry-run" in text
    assert "重置密码" in text
    assert "锁定状态" in text


def test_insufficient_space_aborts(
    v1_sample: SampleLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import tools.migrate_v1.__main__ as cli

    monkeypatch.setattr(cli, "shutil", _NoSpace())

    exit_code = main(_args(v1_sample, tmp_path / "v2"))

    assert exit_code == 1
    assert "空间不足" in capsys.readouterr().out


class _NoSpace:
    @staticmethod
    def disk_usage(_: Path):
        class _Usage:
            free = 1

        return _Usage()


# ------------------------------------ 引导管理员凭据：只上终端，绝不进 --report 文件


BANNER = "管理员登录凭据（仅本次显示，请立即保存）"


def _credentials_from(stdout: str) -> tuple[str, str]:
    """从终端输出里取出（用户名, 密码），取不到就让测试失败。"""
    user_prefix, password_prefix = "用户名: ", "密  码: "
    lines = stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if BANNER in line)
    username = next(line for line in lines[start:] if line.startswith(user_prefix))[
        len(user_prefix) :
    ]
    password = next(line for line in lines[start:] if line.startswith(password_prefix))[
        len(password_prefix) :
    ]
    return username, password


def test_dry_run_prints_no_credentials(v1_sample: SampleLayout, tmp_path: Path, capsys) -> None:
    exit_code = main(_args(v1_sample, tmp_path / "v2", "--dry-run"))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert BANNER not in output
    assert "密  码: " not in output
    # 预演仍要说明正式执行会怎么做
    assert "正式执行时会把账号 alice" in output


def test_real_run_prints_credentials_once(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    exit_code = main(_args(v1_sample, tmp_path / "v2"))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count(BANNER) == 1
    username, password = _credentials_from(output)
    assert username == "alice"
    assert len(password) >= 24
    assert "为其他账号设置临时密码" in output


def test_credentials_never_reach_the_report_file(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "reports" / "migration.txt"

    exit_code = main(_args(v1_sample, tmp_path / "v2", "--report", str(report_path)))

    stdout = capsys.readouterr().out
    assert exit_code == 0
    _username, password = _credentials_from(stdout)
    report_text = report_path.read_text(encoding="utf-8")

    assert password not in report_text
    assert BANNER not in report_text
    assert "密  码" not in report_text
    # 报告里仍然要点名是哪个账号
    assert "已将账号 alice" in report_text
    # 报告文件内容就是报告本体，凭据段只在终端
    assert report_text in stdout


def test_aborted_run_prints_no_credentials(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    v2_root = tmp_path / "v2"
    v2_root.mkdir()
    (v2_root / "keep.txt").write_text("x")

    exit_code = main(_args(v1_sample, v2_root))

    output = capsys.readouterr().out
    assert exit_code == 1
    assert BANNER not in output


def test_help_mentions_the_generated_admin_password() -> None:
    from tools.migrate_v1.__main__ import build_parser

    text = build_parser().format_help()

    assert "随机密码" in text


def test_checksum_mismatch_still_prints_the_password(
    v1_sample: SampleLayout, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入成功但校验失败时必须照旧打印：密码已在库里，不打出来就永久丢失。"""
    import tools.migrate_v1.__main__ as cli
    from tools.migrate_v1.report import Check

    def failing_checks(**_: object) -> tuple[Check, ...]:
        return (Check("用户数", "3", "2", False),)

    monkeypatch.setattr(cli, "build_checks", failing_checks)

    exit_code = main(_args(v1_sample, tmp_path / "v2"))

    output = capsys.readouterr().out
    assert exit_code == 1
    assert BANNER in output
    username, password = _credentials_from(output)
    assert username == "alice"
    assert len(password) >= 24


def test_link_flag_parses() -> None:
    from tools.migrate_v1.__main__ import build_parser

    options = build_parser().parse_args(
        ["--v1-database", "a.db", "--v1-download-dir", "d", "--v2-root", "v2", "--link"]
    )
    assert options.link is True
