# Task 005 - 任务拆分草稿

## 后端任务

### T1: 数据库迁移
- 删除 `api_tokens` 表
- `users` 表新增 `rpc_secret VARCHAR(64) NULL`
- `users` 表新增 `rpc_secret_created_at TEXT NULL`
- 添加唯一索引 `idx_users_rpc_secret`
- 文件: `backend/app/db.py`

### T2: 新增 Pydantic 模型
- `RpcAccessStatus`: enabled, secret, created_at
- `RpcAccessToggle`: enabled
- 文件: `backend/app/schemas.py`

### T3: 新增 RPC 访问管理 API
- `GET /api/users/me/rpc-access` - 获取状态
- `PUT /api/users/me/rpc-access` - 开启/关闭
- `POST /api/users/me/rpc-access/refresh` - 刷新 Secret
- 使用 `secrets.token_urlsafe(32)` 生成 Secret
- 文件: `backend/app/routers/users.py`

### T4: 重构 RPC 代理接口
- 删除旧的 `/aria2/{token}/jsonrpc` 路由
- 新增统一 `/aria2/jsonrpc` 路由
- 从 `params[0]` 提取 `token:xxx`
- 通过 `rpc_secret` 查询用户
- 使用 `secrets.compare_digest` 防时序攻击
- 替换为真正的 aria2 secret 后转发
- 添加限流 100次/分钟/IP
- 文件: `backend/app/routers/aria2_rpc.py`

### T5: 清理旧代码
- 删除 `get_user_by_token` 函数（旧 Token 验证）
- 删除 api_tokens 相关的所有引用
- 文件: `backend/app/routers/aria2_rpc.py`, `backend/app/routers/users.py`

## 前端任务

### T6: 新增类型定义
- `RpcAccessStatus` 接口
- `RpcAccessToggle` 接口
- 文件: `frontend/types.ts`

### T7: 新增 API 调用函数
- `getRpcAccess()` - GET
- `setRpcAccess(enabled)` - PUT
- `refreshRpcSecret()` - POST
- 删除旧的 Token 相关 API
- 文件: `frontend/lib/api.ts`

### T8: 重构用户设置页面
- 删除旧的 API Token 管理 UI
- 新增"外部访问"开关
- 开关旁显示说明文字
- 开启后显示: Secret、复制按钮、刷新按钮、连接示例
- 关闭时隐藏 Secret 相关 UI
- 文件: `frontend/app/profile/page.tsx`

## 依赖关系

```
T1 (数据库)
    ↓
T2 (Schema) → T3 (API) → T5 (清理)
                ↓
              T4 (RPC重构)

T6 (类型) → T7 (API) → T8 (UI)
```

## 任务合并分析

- T1, T2 可合并: 都是后端基础设施，无外部依赖
- T3, T4, T5 需串行: 有代码依赖关系
- T6, T7 可合并: 都是前端基础设施
- T8 依赖 T7

## 最终任务列表

1. **[S] 后端基础: 数据库迁移 + Schema** (T1+T2)
2. **[S] 后端 API: RPC 访问管理接口** (T3)
3. **[S] 后端重构: RPC 代理接口 + 清理旧代码** (T4+T5)
4. **[S] 前端基础: 类型定义 + API 调用** (T6+T7)
5. **[S] 前端 UI: 用户设置页面重构** (T8)
6. **[S] 构建验证: make build 确保无错误**
