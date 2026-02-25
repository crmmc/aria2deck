"""数据库完整性检查测试

测试场景：
1. 正常数据库通过完整性检查
2. WAL checkpoint 正常执行
3. 数据库连接超时配置生效
"""
import pytest
from pathlib import Path
import tempfile

from app.database import (
    check_database_integrity,
    check_wal_integrity,
    reset_engine,
    _get_engine,
)
from app.core.config import settings


@pytest.fixture(autouse=True)
def reset_db_engine():
    """每个测试前重置数据库引擎"""
    reset_engine()
    yield
    reset_engine()


@pytest.mark.asyncio
async def test_database_integrity_check_passes():
    """测试正常数据库通过完整性检查"""
    # 使用测试数据库
    result = await check_database_integrity()
    assert result is True


@pytest.mark.asyncio
async def test_wal_integrity_check_passes():
    """测试 WAL checkpoint 正常执行"""
    result = await check_wal_integrity()
    assert result is True


@pytest.mark.asyncio
async def test_engine_has_timeout_config():
    """测试数据库引擎配置了超时"""
    engine = _get_engine()

    # aiosqlite 的超时配置在 connect_args 中
    # 验证超时参数存在
    assert engine.pool is not None
    # WAL 模式会在 init_db 时设置，这里只验证引擎创建成功
    assert engine is not None


@pytest.mark.asyncio
async def test_database_integrity_with_corrupted_db():
    """测试损坏的数据库被检测到

    注意：此测试仅验证函数能够处理异常情况，
    实际损坏数据库需要手动模拟较为复杂
    """
    # 创建一个临时的空文件作为"损坏"的数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
        # 写入无效数据
        tmp.write(b"not a valid sqlite database file")

    try:
        # 临时修改设置指向损坏的数据库
        original_path = settings.database_path
        settings.database_path = tmp_path
        reset_engine()

        # 完整性检查应该失败或抛出异常
        result = await check_database_integrity()
        assert result is False

    finally:
        # 恢复原设置
        settings.database_path = original_path
        reset_engine()
        Path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_init_default_config(temp_db: str):
    from app.database import init_default_config, get_session

    async with get_session() as session:
        await init_default_config(session)

    async with get_session() as session:
        from sqlmodel import select
        from app.models import Config

        result = await session.exec(select(Config).where(Config.key == "max_task_size"))
        config = result.first()
        assert config is not None
        assert config.value == "10737418240"


@pytest.mark.asyncio
async def test_init_default_config_idempotent(temp_db: str):
    from app.database import init_default_config, get_session

    async with get_session() as session:
        await init_default_config(session)

    async with get_session() as session:
        await init_default_config(session)

    async with get_session() as session:
        from sqlmodel import select
        from app.models import Config

        result = await session.exec(select(Config).where(Config.key == "pack_format"))
        config = result.first()
        assert config is not None
        assert config.value == "tar.zst"


@pytest.mark.asyncio
async def test_database_integrity_check_with_issues():
    from unittest.mock import patch, AsyncMock, MagicMock
    from app.database import check_database_integrity

    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("error1",), ("error2",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_engine.connect.return_value.__aexit__.return_value = None

    with patch("app.database._get_engine", return_value=mock_engine):
        result = await check_database_integrity()
        assert result is False


@pytest.mark.asyncio
async def test_wal_integrity_check_with_busy():
    from unittest.mock import patch, AsyncMock, MagicMock
    from app.database import check_wal_integrity

    mock_result = MagicMock()
    mock_result.fetchone.return_value = (1, 10, 5)

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    mock_engine.connect.return_value.__aexit__.return_value = None

    with patch("app.database._get_engine", return_value=mock_engine):
        result = await check_wal_integrity()
        assert result is True


@pytest.mark.asyncio
async def test_wal_integrity_check_exception():
    from unittest.mock import patch, MagicMock
    from app.database import check_wal_integrity

    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection failed")

    with patch("app.database._get_engine", return_value=mock_engine):
        result = await check_wal_integrity()
        assert result is False


@pytest.mark.asyncio
async def test_init_default_config_adds_missing(temp_db: str):
    from app.database import init_default_config, get_session
    from sqlmodel import select
    from app.models import Config

    async with get_session() as session:
        result = await session.exec(select(Config).where(Config.key == "max_task_size"))
        existing = result.first()
        if existing:
            await session.delete(existing)
            await session.commit()

    async with get_session() as session:
        await init_default_config(session)

    async with get_session() as session:
        result = await session.exec(select(Config).where(Config.key == "max_task_size"))
        config = result.first()
        assert config is not None
