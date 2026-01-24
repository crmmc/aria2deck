from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


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

    # Relationships
    sessions: list["Session"] = Relationship(back_populates="user", cascade_delete=True)
    tasks: list["Task"] = Relationship(back_populates="owner", cascade_delete=True)
    pack_tasks: list["PackTask"] = Relationship(back_populates="owner", cascade_delete=True)


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
