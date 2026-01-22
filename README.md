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

### 任务管理
- 支持 BT、HTTP、FTP 多协议下载
- 任务状态实时同步（2秒轮询间隔）
- 任务排序：按时间、速度、剩余时间（升序/降序）
- 任务筛选：全部、活动中、已完成、错误
- 批量操作：暂停、继续、删除
- 峰值指标追踪：峰值下载速度、峰值连接数
- 任务缓存机制：避免大任务创建时接口阻塞

### 文件管理
- 文件浏览器：支持目录导航、路径验证
- 文件操作：下载、删除、重命名
- 文件后缀名黑名单（可配置，如 .aria2、.tmp、.part）
- 智能清理：删除任务/文件时自动清理 .aria2 控制文件

### 动态配置
- aria2 RPC 连接配置（URL、Secret）
- 系统限制：最大任务大小、最小剩余磁盘空间
- 文件扩展名黑名单
- 配置热更新：无需重启服务

### 实时监控
- WebSocket 推送任务状态更新
- 下载速度实时图表
- 用户空间使用统计
- 机器磁盘空间监控（管理员）

## 技术栈

### 后端
- **框架**: FastAPI 0.111.0
- **服务器**: uvicorn
- **HTTP 客户端**: aiohttp 3.9.5
- **配置管理**: pydantic-settings 2.3.4
- **数据库**: SQLite（原生 SQL，无 ORM）
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
2. 设置环境变量（参考 `backend/env.example`）
3. 运行项目：
   - `PYTHONPATH=backend uv run uvicorn app.main:app --reload --port 8000`

## 前端配置 (Frontend)

1. 安装依赖：`bun install`
2. 设置环境变量（参考 `frontend/env.local.example`）
3. 运行项目：
   - `bun run dev`

## 一键构建与运行 (Makefile)

项目提供了 `Makefile` 以便快速部署：

- **安装所有依赖**：`make install`
- **编译前端并同步到后端**：`make build`（会将前端产物放在 `backend/static` 目录下）
- **启动整合服务**：`make run`（此时后端 8000 端口会同时提供 API 和前端页面）
- **清理构建产物**：`make clean`

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
- RPC 密钥：b（可在配置文件中修改）
- 下载目录：backend/downloads

### 手动启动（生产环境）

启动示例命令：

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

## 初始用户创建

服务首次启动会自动创建管理员账号：

- 用户名：`admin`
- 密码：18 位随机字符串（包含大小写与数字），写入 `backend/data/admin_credentials.txt`

之后创建用户需要管理员权限的会话。

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

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks` | 当前用户任务列表（支持状态筛选） |
| POST | `/api/tasks` | 新建任务（带容量检测与隔离） |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/detail` | 任务详细信息（含 aria2 实时状态、峰值数据） |
| DELETE | `/api/tasks/{id}?delete_files=bool` | 删除任务（可选删除文件） |
| DELETE | `/api/tasks?delete_files=bool` | 清空历史记录（可选删除文件） |
| PUT | `/api/tasks/{id}/status` | 暂停/恢复任务 |
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

### 后台配置（管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取系统配置 |
| PUT | `/api/config` | 修改配置（aria2 RPC、任务限制、文件黑名单等） |
| GET | `/api/config/aria2/version` | 获取当前连接的 aria2 版本信息 |
| POST | `/api/config/aria2/test` | 测试 aria2 连接 |

### Aria2 回调

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/hooks/aria2` | Aria2 回调入口（内部调用） |
