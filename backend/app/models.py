from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship, UniqueConstraint


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_str() -> str:
    """Return current UTC datetime as ISO string."""
    return utc_now().isoformat()


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, max_length=50)
    password_hash: str
    is_admin: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    quota: int = Field(default=107374182400)  # 100GB
    created_at: str
    rpc_secret: Optional[str] = Field(default=None, max_length=64)
    rpc_secret_created_at: Optional[str] = None
    is_initial_password: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})

    # Relationships
    sessions: list["Session"] = Relationship(back_populates="user", cascade_delete=True)
    tasks: list["Task"] = Relationship(back_populates="owner", cascade_delete=True)
    pack_tasks: list["PackTask"] = Relationship(back_populates="owner", cascade_delete=True)
    task_subscriptions: list["UserTaskSubscription"] = Relationship(
        back_populates="owner", cascade_delete=True
    )
    files: list["UserFile"] = Relationship(back_populates="owner", cascade_delete=True)


class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    expires_at: str

    user: Optional[User] = Relationship(back_populates="sessions")


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="users.id")
    gid: Optional[str] = None
    uri: str
    status: str
    name: Optional[str] = None
    total_length: int = Field(default=0)
    completed_length: int = Field(default=0)
    download_speed: int = Field(default=0)
    upload_speed: int = Field(default=0)
    error: Optional[str] = None
    created_at: str
    updated_at: str
    artifact_path: Optional[str] = None
    artifact_token: Optional[str] = None
    peak_download_speed: int = Field(default=0)
    peak_connections: int = Field(default=0)

    owner: Optional[User] = Relationship(back_populates="tasks")


class Config(SQLModel, table=True):
    __tablename__ = "config"

    key: str = Field(primary_key=True)
    value: str


class PackTask(SQLModel, table=True):
    __tablename__ = "pack_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="users.id")
    folder_path: str
    folder_size: int
    reserved_space: int
    output_path: Optional[str] = None
    output_name: Optional[str] = None
    output_size: Optional[int] = None
    status: str = Field(default="pending")
    progress: int = Field(default=0)
    error_message: Optional[str] = None
    created_at: str
    updated_at: str

    owner: Optional[User] = Relationship(back_populates="pack_tasks")


# ========== Shared Download Architecture Models ==========


class DownloadTask(SQLModel, table=True):
    """下载任务（全局共享，与用户无关）

    每个唯一的下载资源对应一个 DownloadTask，多个用户可以订阅同一个任务。
    使用 uri_hash 进行去重：
    - 磁力链接：info_hash
    - 种子文件：info_hash
    - HTTP(S)：最终 URL 的 sha256
    """
    __tablename__ = "download_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)

    # 任务标识（去重用）
    uri_hash: str = Field(index=True, unique=True)  # 普通URL: sha256(url), 磁力: info_hash
    uri: str  # 原始 URI（脱敏存储）

    # aria2 关联
    gid: Optional[str] = Field(default=None, index=True)  # aria2 GID

    # 任务状态
    status: str = Field(default="queued")  # queued, active, complete, error
    name: Optional[str] = None
    total_length: int = Field(default=0)
    completed_length: int = Field(default=0)
    download_speed: int = Field(default=0)
    upload_speed: int = Field(default=0)
    error: Optional[str] = None
    error_display: Optional[str] = None  # 用户可见的错误信息

    # 完成后关联的存储文件
    stored_file_id: Optional[int] = Field(default=None, foreign_key="stored_files.id")

    # 时间戳
    created_at: str = Field(default_factory=utc_now_str)
    updated_at: str = Field(default_factory=utc_now_str)
    completed_at: Optional[str] = None

    # 峰值统计
    peak_download_speed: int = Field(default=0)
    peak_connections: int = Field(default=0)

    # Relationships
    stored_file: Optional["StoredFile"] = Relationship(back_populates="download_tasks")
    subscriptions: list["UserTaskSubscription"] = Relationship(
        back_populates="task", cascade_delete=True
    )


class StoredFile(SQLModel, table=True):
    """存储的文件（实际物理文件）

    下载完成后，文件移动到 /data/store/{content_hash}/ 目录。
    使用引用计数管理文件生命周期。
    """
    __tablename__ = "stored_files"

    id: Optional[int] = Field(default=None, primary_key=True)

    # 文件标识
    content_hash: str = Field(index=True, unique=True)  # 文件内容 hash

    # 存储信息
    real_path: str  # 实际存储路径 /data/store/ab/abc123.../
    size: int
    is_directory: bool = Field(default=False)  # BT 下载的目录

    # 引用计数
    ref_count: int = Field(default=0)

    # 元数据
    original_name: str  # 原始文件名

    # 时间戳
    created_at: str = Field(default_factory=utc_now_str)

    # Relationships
    download_tasks: list["DownloadTask"] = Relationship(back_populates="stored_file")
    user_files: list["UserFile"] = Relationship(back_populates="stored_file")


class UserFile(SQLModel, table=True):
    """用户文件引用

    用户对 StoredFile 的引用，支持自定义显示名称。
    同一用户不能有重复引用同一个 StoredFile。
    """
    __tablename__ = "user_files"
    __table_args__ = (UniqueConstraint("owner_id", "stored_file_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    owner_id: int = Field(foreign_key="users.id", index=True)
    stored_file_id: int = Field(foreign_key="stored_files.id", index=True)

    # 显示名称（用户可重命名）
    display_name: str

    # 时间戳
    created_at: str = Field(default_factory=utc_now_str)

    # Relationships
    owner: Optional[User] = Relationship(back_populates="files")
    stored_file: Optional[StoredFile] = Relationship(back_populates="user_files")


class UserTaskSubscription(SQLModel, table=True):
    """用户任务订阅

    用户对 DownloadTask 的订阅，记录空间冻结和订阅状态。
    同一用户不能重复订阅同一个任务。
    """
    __tablename__ = "user_task_subscriptions"
    __table_args__ = (UniqueConstraint("owner_id", "task_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    owner_id: int = Field(foreign_key="users.id", index=True)
    task_id: int = Field(foreign_key="download_tasks.id", index=True)

    # 空间冻结
    frozen_space: int = Field(default=0)  # 冻结的空间大小

    # 状态
    status: str = Field(default="pending")  # pending, success, failed
    error_display: Optional[str] = None  # 失败原因（用户可见）

    # 时间戳
    created_at: str = Field(default_factory=utc_now_str)

    # Relationships
    owner: Optional[User] = Relationship(back_populates="task_subscriptions")
    task: Optional[DownloadTask] = Relationship(back_populates="subscriptions")
