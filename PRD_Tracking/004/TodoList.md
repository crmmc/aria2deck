# Task 004 - TodoList

## 任务进度追踪

| ID | 状态 | 类型 | 任务 | 依赖 |
|----|------|------|------|------|
| T1 | [x] | [S] | 数据库 - 新增 api_tokens 表 | - |
| T2 | [x] | [S] | 后端 - Token CRUD API | T1 |
| T3 | [x] | [S] | 后端 - RPC 方法处理器 | T1 |
| T4 | [x] | [S] | 后端 - RPC 路由注册 | T3 |
| T5 | [x] | [P] | 前端 - Token 管理 UI | T2 |
| T6 | [x] | [S] | 集成验证 | T4, T5 |

---

## 详细任务列表

### 1. [x][S] 数据库 - 新增 api_tokens 表

在 `backend/app/db.py` 的 `init_db()` 中新增:
- `api_tokens` 表 (id, user_id, token, name, created_at, last_used_at)
- Token 索引 `idx_api_tokens_token`

验证: 启动后端检查表存在

---

### 2. [x][S] 后端 - Token CRUD API

在 `backend/app/routers/config.py` 新增:
- `GET /api/config/tokens` - 获取用户 Token 列表
- `POST /api/config/tokens` - 生成新 Token (格式: `aria2_{24位随机}`)
- `DELETE /api/config/tokens/{token_id}` - 删除 Token

验证: curl 测试 CRUD

---

### 3. [x][S] 后端 - RPC 方法处理器

创建 `backend/app/services/aria2_rpc_handler.py`:
- `Aria2RpcHandler` 类
- 完整实现: addUri, addTorrent, remove, pause, unpause, tellStatus, tellActive, tellWaiting, tellStopped, getFiles, getUris, getGlobalStat, getVersion, changePosition, system.listMethods, system.multicall
- 静默处理: getOption, changeOption, shutdown 等返回 OK/{}
- 数据脱敏: 路径转相对路径
- 用户隔离: 校验任务 owner

验证: 方法逻辑正确

---

### 4. [x][S] 后端 - RPC 路由注册

创建 `backend/app/routers/aria2_rpc.py`:
- Token 验证函数 (查询 api_tokens 表, 更新 last_used_at)
- `POST /aria2/{token}/jsonrpc` 路由
- JSON-RPC 2.0 请求/响应格式
- 错误码处理
- 在 main.py 注册路由

验证: curl 发送 JSON-RPC 请求

---

### 5. [x][P] 前端 - Token 管理 UI

修改文件:
- `frontend/types.ts` - ApiToken 类型
- `frontend/lib/api.ts` - Token API 方法
- `frontend/app/settings/page.tsx` - Token 管理区块

功能:
- Token 列表展示
- 生成新 Token (可选命名)
- 复制 Token
- 删除 Token
- 连接示例展示

验证: 浏览器测试

---

### 6. [x][S] 集成验证

测试内容:
- 使用 aria2 客户端连接
- 添加 HTTP/磁力链接/种子任务
- 查询/暂停/恢复/删除任务
- 验证数据脱敏
- 验证用户隔离

验证: 完整流程无报错

---

## 执行顺序

```
T1 (数据库)
    ↓
T2 (Token API) → T5 (前端 UI) [并行]
    ↓
T3 (RPC 处理器)
    ↓
T4 (RPC 路由)
    ↓
T6 (集成验证)
```
