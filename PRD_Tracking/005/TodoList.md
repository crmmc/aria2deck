# Task 005 - TodoList

## RPC Secret 认证重构

### 任务列表

1. [x][S] **后端基础: 数据库迁移 + Schema**
   - 输入: 现有 db.py, schemas.py
   - 操作:
     - db.py: 删除 api_tokens 表创建语句，users 表新增 rpc_secret/rpc_secret_created_at 字段
     - schemas.py: 新增 RpcAccessStatus, RpcAccessToggle 模型
   - 输出: 数据库结构更新，新 Schema 可用
   - 验证: Python 语法检查通过

2. [x][S] **后端 API: RPC 访问管理接口**
   - 输入: Task 1 完成
   - 操作:
     - users.py: 新增 GET/PUT /api/users/me/rpc-access, POST /api/users/me/rpc-access/refresh
     - 使用 secrets.token_urlsafe(32) 生成 Secret
   - 输出: 三个新 API 端点可用
   - 验证: Python 语法检查通过

3. [x][S] **后端重构: RPC 代理接口**
   - 输入: Task 2 完成
   - 操作:
     - aria2_rpc.py: 删除旧 /aria2/{token}/jsonrpc 路由
     - 新增统一 /aria2/jsonrpc 路由
     - 从 params[0] 提取 token:xxx，查询 rpc_secret
     - 使用 secrets.compare_digest 防时序攻击
     - 添加限流装饰器
     - 删除 get_user_by_token 函数和 api_tokens 相关代码
   - 输出: 新 RPC 代理接口可用
   - 验证: Python 语法检查通过

4. [x][S] **前端基础: 类型定义 + API 调用**
   - 输入: 后端 API 设计
   - 操作:
     - types.ts: 新增 RpcAccessStatus 接口
     - api.ts: 新增 getRpcAccess, setRpcAccess, refreshRpcSecret 函数
     - 删除旧的 Token 相关类型和 API
   - 输出: 前端可调用新 API
   - 验证: TypeScript 类型检查通过

5. [x][S] **前端 UI: 用户设置页面重构**
   - 输入: Task 4 完成
   - 操作:
     - profile/page.tsx: 删除旧 API Token 管理 UI
     - 新增"外部访问"开关 + 说明文字
     - 开启后显示 Secret、复制/刷新按钮、连接示例
   - 输出: 新 UI 可用
   - 验证: 页面渲染无错误

6. [x][S] **构建验证**
   - 输入: Task 1-5 完成
   - 操作: 运行 make build
   - 输出: 构建成功，无错误
   - 验证: 构建日志无 error

### 依赖关系
```
Task 1 → Task 2 → Task 3
                      ↓
Task 4 ──────────→ Task 5 → Task 6
```

### 状态说明
- `[ ]` 未开始
- `[-]` 进行中
- `[x]` 已完成
- `[E]` 失败
- `[S]` 串行任务
- `[P]` 并行任务
