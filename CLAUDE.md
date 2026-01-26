# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Aria2Deck - aria2 多用户下载管理平台。

- **后端**: FastAPI + SQLModel (SQLite) + aiohttp
- **前端**: Next.js 14 静态导出
- **包管理**: `uv` (后端), `bun` (前端)

## 常用命令

```bash
# 安装依赖
make install

# 构建前端并部署到后端
make build

# 启动开发服务器 (端口 8000)
make run

# 后端测试
cd backend && uv run pytest
cd backend && uv run pytest tests/test_foo.py::test_bar -v  # 单个测试

# 前端开发
cd frontend && bun run dev
```

## 包管理规范

**必须使用 `uv add` 添加 Python 依赖**：
```bash
uv add <package>
uv add --dev <package>
```

**禁止**手动编辑 `pyproject.toml` 或 `requirements.txt`。

**后端代码修改后必须运行测试**：`cd backend && uv run pytest`

## 架构

### 后端结构

```
backend/app/
├── main.py          # FastAPI 应用入口，lifespan 管理
├── auth.py          # 认证逻辑 (require_user, require_admin 依赖)
├── models.py        # SQLModel 模型 (User, Session, Task, Config, PackTask)
├── database.py      # 异步数据库引擎 (get_session 上下文管理器)
├── db.py            # 遗留模块 (init_db 用于 schema 迁移)
├── schemas.py       # Pydantic 请求/响应模型
├── routers/         # API 路由按功能划分
├── aria2/
│   ├── client.py    # JSON-RPC 客户端
│   └── sync.py      # 后台任务同步 (2秒轮询)
└── services/
    └── pack.py      # 打包任务处理
```

### 前端结构

```
frontend/
├── app/             # Next.js App Router 页面
├── components/      # React 组件
├── lib/
│   ├── api.ts       # API 客户端 (统一使用 api.xxx())
│   └── AuthContext.tsx
└── types.ts         # TypeScript 类型定义
```

### 数据流

1. **认证**: Cookie-based Session (`aria2_session`)
2. **任务同步**: 后台 `sync_tasks` 每 2 秒轮询 aria2 RPC，更新数据库
3. **实时更新**: WebSocket `/ws/tasks` 推送任务状态
4. **用户隔离**: 每用户独立下载目录默认 `backend/downloads/{user_id}/`（由 `ARIA2C_DOWNLOAD_DIR` 控制）

### 数据库

SQLModel ORM，5 张表：
- `users`: 用户 (quota, rpc_secret)
- `sessions`: 会话
- `tasks`: 下载任务 (峰值指标: peak_download_speed, peak_connections)
- `config`: 系统配置
- `pack_tasks`: 打包任务

数据库访问使用 `async with get_session() as db:` 模式。

## 关键模式

### 添加 API 端点

1. 在 `backend/app/routers/` 创建或修改路由
2. 使用 `Depends(require_user)` 或 `Depends(require_admin)` 做认证
3. 在 `main.py` 注册路由

### 添加前端页面

1. 在 `frontend/app/{route}/page.tsx` 创建页面
2. 在 `main.py` 的 `alias_map` 添加 SPA 路由映射
3. 运行 `make build` 更新静态文件

### 数据库操作

```python
from app.database import get_session
from app.models import Task

async with get_session() as db:
    result = await db.exec(select(Task).where(Task.owner_id == user.id))
    tasks = result.all()
```

## 约束

- 使用参数化 SQL，禁止字符串拼接
- API 错误信息使用中文
- 前端使用 `@/lib/api` 的 `api` 对象，禁止直接 `fetch`
- 禁止 `as any`、`@ts-ignore`
