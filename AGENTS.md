# AGENTS.md - Aria2Deck

> 给在本仓库工作的 AI 编码代理的指引。

## 项目概述

Aria2Deck - aria2 多用户下载管理平台。

- **后端**：FastAPI + SQLModel (SQLite) + aiohttp
- **前端**：Next.js 14 静态导出
- **包管理**：`uv`（后端）、`bun`（前端）
- **关键能力**：用户隔离、配额与磁盘空间校验、任务排序/筛选/批量操作、文件管理与打包、动态配置、WebSocket 实时推送、中文 UI
- **aria2 RPC 兼容**：`POST /aria2/jsonrpc`（供 AriaNg/Motrix 等）

---

## 常用命令

```bash
# 安装依赖
make install

# 构建前端并拷贝到后端 static
make build

# 启动开发服务器 (端口 8000)
make run

# 清理
make clean

# 后端测试
cd backend && uv run pytest

# 前端开发
cd frontend && bun run dev
```

## 测试要求

> ⚠️ **修改 `backend/` 必须运行测试**

```bash
cd backend && uv run pytest
```

## 项目结构

```
aria2_controler/
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口 + 静态导出路由映射
│   │   ├── auth.py           # 会话认证依赖
│   │   ├── models.py         # SQLModel 模型
│   │   ├── database.py       # 异步引擎与 get_session
│   │   ├── db.py             # 旧版 SQLite 迁移/兼容逻辑
│   │   ├── schemas.py        # Pydantic 请求/响应模型
│   │   ├── core/
│   │   │   ├── config.py     # pydantic-settings 配置
│   │   │   ├── security.py   # 密码哈希
│   │   │   └── state.py      # App 状态管理
│   │   ├── routers/
│   │   │   ├── auth.py       # 登录/退出/当前用户
│   │   │   ├── users.py      # 用户管理（管理员）
│   │   │   ├── tasks.py      # 任务管理
│   │   │   ├── files.py      # 文件管理 + 打包任务
│   │   │   ├── stats.py      # 系统统计
│   │   │   ├── config.py     # 系统配置（管理员）
│   │   │   ├── hooks.py      # aria2 回调
│   │   │   ├── ws.py         # WebSocket
│   │   │   └── aria2_rpc.py  # aria2 RPC 兼容接口
│   │   ├── services/
│   │   │   ├── pack.py       # 打包任务
│   │   │   └── aria2_rpc_handler.py
│   │   └── aria2/
│   │       ├── client.py     # JSON-RPC 客户端
│   │       └── sync.py       # 后台任务同步
│   ├── aria2/                # aria2 配置文件
│   ├── scripts/              # aria2 hook 脚本
│   ├── data/                 # SQLite DB（gitignored）
│   ├── static/               # 前端构建产物（gitignored）
│   └── downloads/            # 用户下载目录
├── frontend/
│   ├── app/                  # Next.js App Router
│   ├── components/           # React 组件
│   ├── lib/
│   │   ├── api.ts            # API 客户端
│   │   └── AuthContext.tsx
│   └── types.ts              # TypeScript 类型定义
├── Makefile
├── pyproject.toml
└── uv.lock
```

## 架构要点

- **认证**：Cookie Session（`aria2_session`），依赖 `require_user` / `require_admin`，当前用户 `GET /api/auth/me`
- **任务同步**：`sync_tasks` 默认每 2 秒轮询 aria2 RPC，更新数据库并通过 `WebSocket /ws/tasks` 推送
- **用户隔离**：下载目录默认 `backend/downloads/{user_id}/`（由 `ARIA2C_DOWNLOAD_DIR` 控制），配额与磁盘剩余双重校验
- **动态配置**：配置存储在 `config` 表，包含 RPC 地址/密钥、隐藏后缀、打包参数等
- **文件与打包**：文件浏览/删除/重命名，打包任务在 `files` 路由下
- **aria2 RPC 兼容**：`/aria2/jsonrpc` 支持外部客户端，逻辑在 `services/aria2_rpc_handler.py`

## 数据库

- 使用 **SQLModel + AsyncSession**（`app.database.get_session`）
- 表：`users`、`sessions`、`tasks`、`config`、`pack_tasks`
- `app.db` 为历史兼容模块（迁移 + 默认管理员），仅在必要时用，必须参数化 SQL

```python
from sqlmodel import select
from app.database import get_session
from app.models import Task

async with get_session() as db:
    result = await db.exec(select(Task).where(Task.owner_id == user.id))
    tasks = result.all()
```

```python
# legacy: 参数化 SQL
execute("SELECT * FROM tasks WHERE id = ?", [task_id])
```

## 代码规范

### Python（后端）

- 导入顺序：stdlib → third-party → local
- 必须类型注解，错误信息用中文
- 新代码优先 SQLModel；历史模块才用 `app.db`/`sqlite3`（禁止拼接 SQL）

### TypeScript（前端）

- 使用路径别名 `@/`
- 类型集中在 `frontend/types.ts`
- API 调用统一走 `@/lib/api`
- 客户端组件加 `"use client"`
- 禁止 `as any` / `@ts-ignore` / `@ts-expect-error`

## 环境变量

- 后端：以 `backend/env.example` 为准（前缀 `ARIA2C_`）
- `ARIA2C_HOOK_SECRET` 用于 `/api/hooks/aria2` 回调鉴权（未配置会返回 503）
- 前端：`frontend/env.local.example`（`NEXT_PUBLIC_API_BASE`）

## 常见操作

### 添加 API 端点

1. 在 `backend/app/routers/` 创建或修改路由
2. 使用 `Depends(require_user)` / `Depends(require_admin)`
3. 在 `main.py` 注册 router

### 添加前端页面

1. 在 `frontend/app/{route}/page.tsx` 新建页面
2. 在 `backend/app/main.py` 的 `alias_map` 添加静态导出映射
3. 运行 `make build`

## 不要做

- 不要用 `as any`、`@ts-ignore`、`@ts-expect-error`
- 不要手动编辑 `pyproject.toml` 或 `requirements.txt`，用 `uv add`
- 不要在 SQL 中拼字符串，必须参数化
- 不要提交 `backend/data/` 或 `backend/static/`
- 不要跳过后端测试
- 不要让用户在前端改动未 `make build` 前验收
