# aria2 控制器 (aria2 controler)

## 概述

一个基于 aria2 的多用户下载管理平台，采用前后端分离架构：

- **FastAPI 后端**：任务管理、Session 认证、WebSocket 实时更新、动态配置
- **Next.js 前端**：静态导出模式，任务列表、详情展示、速度图表、文件管理

## 核心特性

### 多用户系统
- 基于 Session Cookie 的认证机制
- 管理员和普通用户角色分离
- 用户空间完全隔离（独立下载目录）
- 用户配额管理（默认 100GB，可调整）
- 密码安全：客户端 PBKDF2 哈希后传输，服务端无法获取明文密码

### 任务管理
- 支持 BT、HTTP、FTP 多协议下载
- 实时状态同步：WebSocket 事件监听（毫秒级）+ 轮询兜底（可配置间隔）
- 任务排序：按时间、速度、剩余时间（升序/降序）
- 任务筛选：全部、活动中、已完成、错误
- 关键词搜索：支持任务名称模糊搜索
- 批量操作：暂停、继续、删除
- 峰值指标追踪：峰值下载速度、峰值连接数
- 任务异步创建：避免 aria2 RPC 调用阻塞接口
- 空间超限保护：自动终止超限任务，保留历史记录支持重试
- 孤立任务清理：文件删除后自动标记任务为 removed 状态

### 文件管理
- 文件浏览器：支持目录导航、路径验证
- 文件操作：下载、删除、重命名
- 打包下载：多文件打包为 ZIP/7z（可配置）
- 隐藏文件后缀列表（可配置，如 .aria2、.tmp、.part）
- 智能清理：删除任务/文件时自动清理 .aria2 控制文件

### 动态配置
- aria2 RPC 连接配置（URL、Secret）
- 系统限制：最大任务大小、最小剩余磁盘空间
- WebSocket 重连参数：最大延迟、抖动系数、指数因子
- 隐藏文件后缀列表
- 配置热更新：无需重启服务

### 实时监控
- aria2 WebSocket 事件监听（自动重连、指数退避）
- WebSocket 推送任务状态更新
- 下载速度实时图表
- 用户空间使用统计
- 机器磁盘空间监控（管理员）

### 前端特性
- 统一的 CSS 设计系统（变量、工具类）
- 自定义 Toast/Confirm 组件（替代浏览器原生弹窗）
- 响应式设计
- 文件名截断悬浮提示
- 任务历史记录查看

## 技术栈

### 后端
- **框架**: FastAPI 0.111.0
- **服务器**: uvicorn
- **HTTP 客户端**: aiohttp 3.9.5
- **配置管理**: pydantic-settings 2.3.4
- **数据库**: SQLite + SQLModel（历史模块保留原生 SQL 兼容）
- **Python 版本**: 3.12+
- **包管理器**: uv

### 前端
- **框架**: Next.js 14.2.5（静态导出模式）
- **运行时**: React 18.3.1
- **语言**: TypeScript 5.5.2
- **包管理器**: Bun
- **构建输出**: 静态 HTML/JS 导出到 `backend/static/`

### 外部依赖
- **aria2c**: 下载引擎，需开启 RPC
- **Node.js**: 18+（用于前端工具链）

## 前置要求

- Python 3.12+
- Node.js 18+
- Bun（用于前端依赖安装与构建）
- uv（用于后端依赖管理）
- 安装并运行开启了 RPC 的 aria2c

## 后端配置 (Backend)

1. 安装依赖：`uv sync`
2. 复制并编辑环境变量：`cp backend/env.example backend/.env`
3. 运行项目：`PYTHONPATH=backend uv run uvicorn app.main:app --reload --port 8000`

## 前端配置 (Frontend)

1. 安装依赖：`cd frontend && bun install`
2. 复制并编辑环境变量：`cp frontend/env.local.example frontend/.env.local`
3. 运行项目：`bun run dev`

## 一键构建与运行 (Makefile)

项目提供了 `Makefile` 以便快速部署：

- **安装所有依赖**：`make install`
- **编译前端并同步到后端**：`make build`（会将前端产物放在 `backend/static` 目录下）
- **启动整合服务**：`make run`（此时后端 8000 端口会同时提供 API 和前端页面）
- **清理构建产物**：`make clean`

## Docker 部署

本项目采用**分离部署**架构：app 容器（FastAPI 应用）+ aria2 容器（下载引擎）分开运行。

### 快速启动

```bash
# 启动服务（自动构建镜像）
make docker-up

# 查看日志
make docker-logs

# 停止服务
make docker-down
```

### 单独构建镜像

```bash
# 构建镜像（自适应当前机器架构）
make docker-build

# 指定目标架构（如部署到 Linux amd64 服务器）
docker build --platform linux/amd64 -t aria2-controler .
```

> **架构说明**：默认构建的镜像架构与当前机器一致。如需跨架构部署，请使用 `--platform` 参数指定目标架构。

### 环境变量

#### App 容器 (aria2-controler)

详见下方[环境变量参考](#环境变量参考)章节。Docker 部署时需注意：
- `ARIA2C_ARIA2_RPC_URL` 应设为 `http://aria2:6800/jsonrpc`（容器间通信）
- `ARIA2C_DOWNLOAD_DIR` 和 `ARIA2C_DATABASE_PATH` 应使用容器内路径

#### Aria2 容器 (p3terx/aria2-pro)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RPC_SECRET` | - | RPC 密钥（需与 app 的 `ARIA2C_ARIA2_RPC_SECRET` 一致） |
| `RPC_PORT` | `6800` | RPC 端口 |
| `LISTEN_PORT` | `6888` | BT 监听端口 |
| `PUID` / `PGID` | `1000` | 运行用户/组 ID |

### docker-compose.yml 示例

```yaml
services:
  aria2:
    image: p3terx/aria2-pro:latest
    container_name: aria2
    restart: unless-stopped
    environment:
      - PUID=1000
      - PGID=1000
      - RPC_SECRET=${ARIA2_RPC_SECRET:-changeme}
      - RPC_PORT=6800
      - LISTEN_PORT=6888
    volumes:
      - ./data/aria2-config:/config
      - ./data/downloads:/downloads
    ports:
      - "6800:6800"
      - "6888:6888"
      - "6888:6888/udp"
    logging:
      driver: json-file
      options:
        max-size: 1m

  app:
    image: aria2-controler:latest
    # 或者使用 build: . 从源码构建
    container_name: aria2-controler
    restart: unless-stopped
    depends_on:
      - aria2
    environment:
      - ARIA2C_ARIA2_RPC_URL=http://aria2:6800/jsonrpc
      - ARIA2C_ARIA2_RPC_SECRET=${ARIA2_RPC_SECRET:-changeme}
      - ARIA2C_DOWNLOAD_DIR=/app/backend/downloads
      - ARIA2C_DATABASE_PATH=/app/backend/data/app.db
      - ARIA2C_DEBUG=false
    volumes:
      - ./data/app:/app/backend/data
      - ./data/downloads:/app/backend/downloads
    ports:
      - "8000:8000"
    logging:
      driver: json-file
      options:
        max-size: 10m
```

### 数据持久化

启动后会在项目目录下创建 `data/` 目录：

| 目录 | 说明 |
|------|------|
| `data/app/` | SQLite 数据库 |
| `data/downloads/` | 下载文件（两个容器共享） |
| `data/aria2-config/` | aria2 配置 |

### 部署示例

```bash
# 1. 设置 RPC 密钥
export ARIA2_RPC_SECRET=your_secure_secret

# 2. 启动
docker compose up -d

# 3. 访问 http://localhost:8000
# 首次启动会创建 admin 账户，登录时需设置密码
```

## aria2 配置

### 快速启动（开发环境）

项目提供了开箱即用的 aria2 配置：

```bash
# 在项目根目录运行
bash backend/aria2/start.sh
```

配置文件位于 `backend/aria2/aria2.conf`，默认配置：
- RPC 端口：6800
- RPC 地址：http://localhost:6800/jsonrpc
- RPC 密钥：1（可在配置文件中修改）
- 下载目录：backend/downloads

### 事件通知机制

本项目通过 **WebSocket 监听 aria2 事件**实现毫秒级响应，无需配置 hook 脚本。

如需使用 hook 脚本（可选，用于特殊场景或兼容老版本 aria2）：

```bash
aria2c --enable-rpc --rpc-listen-all --rpc-allow-origin-all \
       --rpc-secret=YOUR_SECRET \
       --on-download-start=backend/scripts/aria2_hook.sh \
       --on-download-pause=backend/scripts/aria2_hook.sh \
       --on-download-stop=backend/scripts/aria2_hook.sh \
       --on-download-complete=backend/scripts/aria2_hook.sh \
       --on-download-error=backend/scripts/aria2_hook.sh \
       --on-bt-download-complete=backend/scripts/aria2_hook.sh
```

回调脚本环境变量：

- `ARIA2_HOOK_URL`：后端回调接口地址，默认 `http://localhost:8000/api/hooks/aria2`
- `ARIA2_HOOK_SECRET`：Hook 认证密钥（需与后端 `ARIA2C_HOOK_SECRET` 一致）

## 初始用户创建

服务首次启动会自动创建管理员账号：

- 用户名：`admin`
- 默认密码：`123456`

可通过环境变量 `ARIA2C_ADMIN_PASSWORD` 自定义初始密码。

> ⚠️ **安全提示**：使用默认密码登录时，系统会提示修改密码。请在首次登录后立即更改密码。

之后创建用户需要管理员权限的会话。

---

## 环境变量参考

所有环境变量以 `ARIA2C_` 为前缀。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARIA2C_APP_NAME` | `aria2-controler` | 应用名称 |
| `ARIA2C_DEBUG` | `false` | 调试模式 |
| `ARIA2C_DATABASE_PATH` | `./data/app.db` | SQLite 数据库路径 |
| `ARIA2C_DOWNLOAD_DIR` | `./downloads` | 下载目录 |
| `ARIA2C_SESSION_TTL_SECONDS` | `43200` | Session 过期时间（秒） |
| `ARIA2C_ARIA2_RPC_URL` | `http://localhost:6800/jsonrpc` | aria2 RPC 地址 |
| `ARIA2C_ARIA2_RPC_SECRET` | - | aria2 RPC 密钥 |
| `ARIA2C_ARIA2_POLL_INTERVAL` | `2.0` | aria2 状态轮询间隔（秒） |
| `ARIA2C_HOOK_SECRET` | - | Hook 回调认证密钥 |
| `ARIA2C_ADMIN_PASSWORD` | `123456` | 初始管理员密码 |

---

## API 接口文档

### 认证相关

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前用户信息 |

### 用户管理（管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/users` | 用户列表 |
| GET | `/api/users/{id}` | 获取单个用户详情 |
| POST | `/api/users` | 创建用户 |
| PUT | `/api/users/{id}` | 更新用户信息 |
| DELETE | `/api/users/{id}` | 删除用户 |

### 任务管理

说明：多数任务接口的 `{id}` 支持任务 ID 或 GID。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 当前用户任务列表（支持状态筛选） |
| POST | `/api/tasks` | 新建任务（带容量检测与隔离） |
| POST | `/api/tasks/torrent` | 上传种子创建任务 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/detail` | 任务详细信息（含 aria2 实时状态、峰值数据） |
| DELETE | `/api/tasks/{id}?delete_files=bool` | 删除任务（可选删除文件） |
| DELETE | `/api/tasks?delete_files=bool` | 清空历史记录（可选删除文件） |
| PUT | `/api/tasks/{id}/status` | 暂停/恢复任务 |
| POST | `/api/tasks/{id}/retry` | 重试失败任务（非种子任务） |
| PUT | `/api/tasks/{id}/position` | 调整任务队列位置 |
| GET | `/api/tasks/{id}/files` | 任务文件列表 |
| GET | `/api/tasks/artifacts/{token}` | 下载制品 |

### 系统状态

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stats` | 用户空间使用统计 |
| GET | `/api/stats/machine` | 机器磁盘空间（管理员） |

### 文件管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/files?path=xxx` | 列出文件和文件夹 |
| GET | `/api/files/download?path=xxx` | 下载文件 |
| DELETE | `/api/files?path=xxx` | 删除文件/文件夹（自动清理 .aria2） |
| PUT | `/api/files/rename` | 重命名文件/文件夹 |
| GET | `/api/files/quota` | 获取用户配额信息 |
| POST | `/api/files/pack/calculate-size` | 计算打包体积 |
| GET | `/api/files/pack/available-space` | 获取可用打包空间 |
| GET | `/api/files/pack` | 获取打包任务列表 |
| POST | `/api/files/pack` | 创建打包任务 |
| GET | `/api/files/pack/{task_id}` | 获取打包任务状态 |
| DELETE | `/api/files/pack/{task_id}` | 取消或删除打包任务 |
| GET | `/api/files/pack/{task_id}/download` | 下载打包文件 |

### 后台配置（管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取系统配置 |
| PUT | `/api/config` | 修改配置（aria2 RPC、任务限制、隐藏后缀列表等） |
| GET | `/api/config/aria2/version` | 获取当前连接的 aria2 版本信息 |
| POST | `/api/config/aria2/test` | 测试 aria2 连接 |

### Aria2 回调

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/hooks/aria2` | Aria2 回调入口（内部调用） |
