# Task 004 - 任务拆分草稿

## 任务依赖关系

```
T1 数据库表
    │
    ▼
T2 Token CRUD API ──────────────┐
    │                           │
    ▼                           ▼
T3 RPC 方法处理器            T5 前端 Token 管理
    │
    ▼
T4 RPC 路由注册
    │
    ▼
T6 集成验证
```

---

## T1: 数据库 - 新增 api_tokens 表

**类型**: 串行 (基础设施)

**输入**:
- 设计文档中的表结构

**任务描述**:
1. 在 `backend/app/db.py` 的 `init_db()` 函数中新增 `api_tokens` 表创建语句
2. 表结构:
   ```sql
   CREATE TABLE IF NOT EXISTS api_tokens (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER NOT NULL,
       token TEXT NOT NULL UNIQUE,
       name TEXT,
       created_at TEXT NOT NULL,
       last_used_at TEXT,
       FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
   )
   ```
3. 添加索引: `CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token)`

**输出**:
- 数据库初始化时自动创建 `api_tokens` 表

**验证**:
- 启动后端，检查数据库中表是否存在

---

## T2: 后端 - Token CRUD API

**类型**: 串行 (依赖 T1)

**输入**:
- api_tokens 表结构
- Token 格式: `aria2_{24位随机字符串}`

**任务描述**:
在 `backend/app/routers/config.py` 中新增以下 API:

1. `GET /api/config/tokens` - 获取当前用户的 Token 列表
   - 返回: `[{id, name, token, created_at, last_used_at}]`
   - 需要登录认证 (`require_user`)

2. `POST /api/config/tokens` - 生成新 Token
   - 请求体: `{name?: string}` (可选命名)
   - Token 生成逻辑:
     ```python
     import secrets
     import string
     chars = string.ascii_letters + string.digits
     random_part = ''.join(secrets.choice(chars) for _ in range(24))
     token = f"aria2_{random_part}"
     ```
   - 返回: `{id, name, token, created_at}`

3. `DELETE /api/config/tokens/{token_id}` - 删除 Token
   - 只能删除自己的 Token
   - 返回: `{ok: true}`

**输出**:
- 3 个新 API 端点

**验证**:
- 使用 curl 测试 CRUD 操作

---

## T3: 后端 - RPC 方法处理器

**类型**: 串行 (核心业务逻辑)

**输入**:
- aria2 RPC 文档
- 设计文档中的方法列表

**任务描述**:
创建 `backend/app/services/aria2_rpc_handler.py`:

1. **类结构**:
   ```python
   class Aria2RpcHandler:
       def __init__(self, user_id: int, aria2_client: Aria2Client):
           self.user_id = user_id
           self.client = aria2_client

       async def handle(self, method: str, params: list) -> Any:
           # 路由到具体方法
   ```

2. **完整实现的方法**:
   - `aria2.addUri(uris, options?, position?)` - 添加任务，忽略 options 中的 dir
   - `aria2.addTorrent(torrent, uris?, options?, position?)` - 添加种子
   - `aria2.remove(gid)` / `aria2.forceRemove(gid)` - 删除任务（校验 owner）
   - `aria2.pause(gid)` / `aria2.forcePause(gid)` - 暂停任务（校验 owner）
   - `aria2.unpause(gid)` - 恢复任务（校验 owner）
   - `aria2.tellStatus(gid, keys?)` - 查询任务状态（数据脱敏）
   - `aria2.tellActive(keys?)` - 查询活动任务（仅用户自己的）
   - `aria2.tellWaiting(offset, num, keys?)` - 查询等待任务
   - `aria2.tellStopped(offset, num, keys?)` - 查询已停止任务
   - `aria2.getFiles(gid)` - 获取文件列表（路径脱敏）
   - `aria2.getUris(gid)` - 获取 URI 列表
   - `aria2.getGlobalStat()` - 返回用户任务统计
   - `aria2.getVersion()` - 返回版本信息
   - `aria2.changePosition(gid, pos, how)` - 调整位置
   - `system.listMethods()` - 返回支持的方法列表
   - `system.multicall(methods)` - 批量调用

3. **静默处理的方法**:
   - `aria2.getOption` → `{}`
   - `aria2.changeOption` → `"OK"`
   - `aria2.getGlobalOption` → `{}`
   - `aria2.changeGlobalOption` → `"OK"`
   - `aria2.shutdown` / `aria2.forceShutdown` → `"OK"`
   - `aria2.saveSession` → `"OK"`
   - `aria2.purgeDownloadResult` → `"OK"`
   - `aria2.removeDownloadResult` → `"OK"`
   - `aria2.pauseAll` / `aria2.forcePauseAll` → `"OK"`
   - `aria2.unpauseAll` → `"OK"`
   - `aria2.getSessionInfo` → `{"sessionId": "proxy"}`

4. **数据脱敏函数**:
   ```python
   def _sanitize_task(self, task: dict) -> dict:
       # 将绝对路径转为用户相对路径
       # /downloads/123/movie/file.mp4 → movie/file.mp4
   ```

5. **用户任务校验**:
   ```python
   def _verify_task_owner(self, gid: str) -> bool:
       # 检查 gid 对应的任务是否属于当前用户
   ```

**输出**:
- `aria2_rpc_handler.py` 文件

**验证**:
- 单元测试各方法

---

## T4: 后端 - RPC 路由注册

**类型**: 串行 (依赖 T3)

**输入**:
- Aria2RpcHandler 类
- 路由路径: `/aria2/{token}/jsonrpc`

**任务描述**:
创建 `backend/app/routers/aria2_rpc.py`:

1. **Token 验证函数**:
   ```python
   def get_user_by_token(token: str) -> dict | None:
       # 查询 api_tokens 表
       # 更新 last_used_at
       # 返回用户信息
   ```

2. **JSON-RPC 请求处理**:
   ```python
   @router.post("/aria2/{token}/jsonrpc")
   async def jsonrpc_handler(token: str, request: Request):
       # 1. Token 验证
       # 2. 解析 JSON-RPC 请求 (支持单个和批量)
       # 3. 调用 Aria2RpcHandler
       # 4. 返回 JSON-RPC 响应
   ```

3. **JSON-RPC 格式**:
   - 请求: `{"jsonrpc": "2.0", "method": "...", "params": [...], "id": "..."}`
   - 成功响应: `{"jsonrpc": "2.0", "result": ..., "id": "..."}`
   - 错误响应: `{"jsonrpc": "2.0", "error": {"code": ..., "message": "..."}, "id": "..."}`

4. **错误码**:
   - `-32700`: Parse error
   - `-32600`: Invalid Request
   - `-32601`: Method not found
   - `-32602`: Invalid params
   - `-32603`: Internal error
   - `1`: Unauthorized (Token 无效)
   - `2`: Task not found or not owned

5. 在 `main.py` 注册路由

**输出**:
- RPC 路由可用

**验证**:
- 使用 curl 发送 JSON-RPC 请求测试

---

## T5: 前端 - Token 管理 UI

**类型**: 并行 (依赖 T2，可与 T3/T4 并行)

**输入**:
- Token CRUD API
- 设计文档中的 UI 布局

**任务描述**:

1. **更新类型定义** (`frontend/types.ts`):
   ```typescript
   export interface ApiToken {
     id: number;
     name: string | null;
     token: string;
     created_at: string;
     last_used_at: string | null;
   }
   ```

2. **更新 API 客户端** (`frontend/lib/api.ts`):
   ```typescript
   listApiTokens: () => request<ApiToken[]>("/api/config/tokens"),
   createApiToken: (name?: string) => request<ApiToken>("/api/config/tokens", {
     method: "POST",
     body: JSON.stringify({ name }),
   }),
   deleteApiToken: (id: number) => request<{ok: boolean}>(`/api/config/tokens/${id}`, {
     method: "DELETE",
   }),
   ```

3. **修改设置页面** (`frontend/app/settings/page.tsx`):
   - 新增 "API Token 管理" 区块
   - Token 列表展示 (名称、Token 前缀...后缀、创建时间、最后使用时间)
   - 生成 Token 按钮 (弹窗输入可选名称)
   - 复制完整 Token 按钮
   - 删除 Token 按钮 (确认对话框)
   - 连接示例展示

**输出**:
- 设置页面 Token 管理功能

**验证**:
- 浏览器中测试生成/复制/删除 Token

---

## T6: 集成验证

**类型**: 串行 (最后执行)

**任务描述**:
1. 使用 AriaNg 或类似客户端测试连接
2. 测试完整流程:
   - 生成 Token
   - 配置客户端连接
   - 添加 HTTP 链接任务
   - 添加磁力链接任务
   - 查询任务状态
   - 暂停/恢复/删除任务
3. 验证数据脱敏 (路径不暴露服务器绝对路径)
4. 验证用户隔离 (无法操作他人任务)

**验证**:
- 所有功能正常工作
- 无报错或异常
