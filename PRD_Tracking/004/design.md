# Task 004: 对外 aria2 RPC 兼容接口

## 📌 功能概述

为 aria2 controller 提供对外的 aria2 RPC 兼容接口，用户可使用标准 aria2 客户端（如 AriaNg、Motrix 等）通过 Token 认证连接，实现任务的增删查改。

**核心特性**：
- Token 认证（路径级别，兼容标准客户端）
- 用户隔离（只能管理自己的任务）
- 数据脱敏（路径转为相对路径）
- 空间监测（配额不足时拒绝添加）
- 静默处理危险/配置操作

---

## 🏗️ 架构设计

### 接口路径设计

```
POST /aria2/{token}/jsonrpc
```

**设计理由**：
1. `/aria2` 前缀：清晰表明这是 aria2 兼容接口，与内部 `/api/*` 隔离
2. `{token}` 在路径中：兼容不支持自定义 Header 的 aria2 客户端
3. 不暴露内部路由结构，对外安全

**用户连接示例**：
```
RPC 地址: https://your-domain.com/aria2/aria2_AbCdEfGhIjKlMnOpQrStUvWx/jsonrpc
RPC 密钥: 留空（已在路径中认证）
```

### 数据库设计

新增 `api_tokens` 表：

```sql
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    name TEXT,                    -- 用户命名（如 "Motrix"、"AriaNg"）
    created_at TEXT NOT NULL,
    last_used_at TEXT,            -- 最后使用时间
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
)
```

Token 格式：`aria2_{24位随机字符串}`
- 字符集：`a-zA-Z0-9`
- 示例：`aria2_AbCdEfGhIjKlMnOpQrStUvWx`

### 模块结构

```
backend/app/
├── routers/
│   └── aria2_rpc.py          # 新增：对外 RPC 路由
├── services/
│   └── aria2_rpc_handler.py  # 新增：RPC 方法处理器
└── db.py                     # 修改：新增 api_tokens 表
```

---

## 🔄 业务流程

### Token 管理流程

```
用户 → 设置页面 → 生成 Token → 获取连接地址
                 → 删除 Token → Token 失效
```

### RPC 请求流程

```
aria2 客户端
    │
    ▼
POST /aria2/{token}/jsonrpc
    │
    ▼
Token 验证 → 失败 → 返回 401 错误
    │
    ▼ 成功
解析 JSON-RPC 请求
    │
    ▼
方法路由
    │
    ├─ 支持的方法 → 执行业务逻辑 → 数据脱敏 → 返回结果
    │
    ├─ 忽略的方法 → 返回 "OK" 或 {}
    │
    └─ 未知方法 → 返回 JSON-RPC error
```

### 数据脱敏流程

```
aria2 返回数据
    │
    ▼
路径字段处理:
  - dir: /downloads/123/.incomplete → .incomplete
  - files[].path: /downloads/123/movie/file.mp4 → movie/file.mp4
    │
    ▼
敏感字段过滤:
  - 移除 aria2 配置信息
  - 保留：gid, status, totalLength, completedLength, downloadSpeed, uploadSpeed, files, bittorrent
```

---

## 🎨 设计原则

### 1. 迭代兼容性（最高优先级）
- 新增独立路由 `/aria2/{token}/jsonrpc`，不影响现有 `/api/*` 接口
- Token 认证与现有 Session 认证并行，互不干扰

### 2. 继承现有规范
- 复用现有 `Aria2Client` 与 aria2 通信
- 复用现有任务数据库表结构
- 复用现有空间检查逻辑

### 3. 安全设计
- Token 长度 30 字符（`aria2_` + 24位随机），暴力破解不可行
- 用户隔离：所有操作校验 `owner_id`
- 静默处理危险操作，不执行但返回成功

---

## 🚨 风险分析

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| Token 泄露 | 他人可操作用户任务 | 用户可随时删除/重新生成 Token |
| 客户端发送危险命令 | 可能影响系统 | 静默忽略，返回成功 |
| 配额绕过 | 超额下载 | 复用现有空间检查逻辑 |
| 并发请求 | 数据不一致 | 复用现有数据库锁机制 |

---

## 🛠️ 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 协议 | JSON-RPC 2.0 over HTTP POST | aria2 标准协议 |
| 认证 | 路径 Token | 兼容所有 aria2 客户端 |
| 随机数 | `secrets.token_urlsafe` | 密码学安全 |

---

## 📏 验收标准

### 功能验收

- [ ] 用户可在设置页面生成/删除 API Token
- [ ] 使用标准 aria2 客户端（如 AriaNg）能成功连接
- [ ] 能通过 RPC 添加 HTTP/磁力链接/种子任务
- [ ] 能查询任务状态，路径已脱敏为相对路径
- [ ] 能暂停/恢复/删除任务
- [ ] 用户 A 无法操作用户 B 的任务
- [ ] 配额不足时拒绝添加任务
- [ ] 危险操作（shutdown 等）静默返回成功

### 兼容性验收

- [ ] AriaNg 能正常连接和操作
- [ ] Motrix 能正常连接和操作
- [ ] aria2 CLI (`aria2rpc`) 能正常调用

---

## 📋 支持的 RPC 方法

### 完整实现

| 方法 | 说明 |
|------|------|
| `aria2.addUri` | 添加 HTTP/FTP/磁力链接任务 |
| `aria2.addTorrent` | 添加种子任务 |
| `aria2.remove` | 删除任务 |
| `aria2.forceRemove` | 强制删除任务（等同 remove） |
| `aria2.pause` | 暂停任务 |
| `aria2.forcePause` | 强制暂停任务（等同 pause） |
| `aria2.unpause` | 恢复任务 |
| `aria2.tellStatus` | 查询单个任务状态 |
| `aria2.tellActive` | 查询活动任务列表 |
| `aria2.tellWaiting` | 查询等待任务列表 |
| `aria2.tellStopped` | 查询已停止任务列表 |
| `aria2.getFiles` | 获取任务文件列表 |
| `aria2.getUris` | 获取任务 URI 列表 |
| `aria2.getGlobalStat` | 获取用户统计（伪全局） |
| `aria2.getVersion` | 返回版本信息 |
| `aria2.changePosition` | 调整任务队列位置 |
| `system.listMethods` | 返回支持的方法列表 |
| `system.multicall` | 批量调用 |

### 静默处理（返回 OK 或空对象）

| 方法 | 返回值 |
|------|--------|
| `aria2.getOption` | `{}` |
| `aria2.changeOption` | `"OK"` |
| `aria2.getGlobalOption` | `{}` |
| `aria2.changeGlobalOption` | `"OK"` |
| `aria2.shutdown` | `"OK"` |
| `aria2.forceShutdown` | `"OK"` |
| `aria2.saveSession` | `"OK"` |
| `aria2.purgeDownloadResult` | `"OK"` |
| `aria2.removeDownloadResult` | `"OK"` |
| `aria2.pauseAll` | `"OK"` |
| `aria2.forcePauseAll` | `"OK"` |
| `aria2.unpauseAll` | `"OK"` |
| `aria2.getSessionInfo` | `{"sessionId": "proxy"}` |

---

## 🖥️ 前端设计

### 设置页面新增区块

```
┌─────────────────────────────────────────────────────────┐
│  API Token 管理                                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  使用 API Token 可以通过 aria2 兼容客户端连接本服务        │
│                                                         │
│  ┌─────────────────────────────────────────────────────┐│
│  │ 名称          Token                    操作         ││
│  ├─────────────────────────────────────────────────────┤│
│  │ AriaNg       aria2_AbCd...WxYz        [复制] [删除] ││
│  │ Motrix       aria2_1234...abcd        [复制] [删除] ││
│  └─────────────────────────────────────────────────────┘│
│                                                         │
│  [+ 生成新 Token]                                        │
│                                                         │
│  ─────────────────────────────────────────────────────  │
│  连接示例：                                              │
│  RPC 地址: https://your-domain.com/aria2/{token}/jsonrpc│
│  RPC 密钥: 留空                                          │
└─────────────────────────────────────────────────────────┘
```

---

## 📁 修改文件清单

### 后端

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/app/db.py` | 修改 | 新增 `api_tokens` 表初始化 |
| `backend/app/routers/aria2_rpc.py` | 新增 | 对外 RPC 路由 |
| `backend/app/services/aria2_rpc_handler.py` | 新增 | RPC 方法处理器 |
| `backend/app/routers/config.py` | 修改 | 新增 Token CRUD API |
| `backend/app/main.py` | 修改 | 注册新路由 |

### 前端

| 文件 | 操作 | 说明 |
|------|------|------|
| `frontend/app/settings/page.tsx` | 修改 | 新增 Token 管理区块 |
| `frontend/lib/api.ts` | 修改 | 新增 Token API 调用 |
| `frontend/types.ts` | 修改 | 新增 Token 类型定义 |
