# 任务拆分草稿

## 原子任务列表

### T1: 添加 hook_secret 配置项
- 文件: `backend/app/core/config.py`
- 内容: 添加 `hook_secret: str = ""` 配置
- 依赖: 无

### T2: hooks.py 添加认证逻辑
- 文件: `backend/app/routers/hooks.py`
- 内容: 添加 Header 验证，空值时跳过
- 依赖: T1

### T3: db.py 添加写锁
- 文件: `backend/app/db.py`
- 内容: 添加 `threading.Lock`，在 `db_cursor` 中使用
- 依赖: 无

### T4: aria2/client.py 添加超时
- 文件: `backend/app/aria2/client.py`
- 内容: 添加 `aiohttp.ClientTimeout(total=30)`
- 依赖: 无

### T5: auth.py 修复时区处理
- 文件: `backend/app/auth.py`
- 内容: 确保 `expires_at` 有时区信息
- 依赖: 无

### T6: tasks/page.tsx WebSocket 重连
- 文件: `frontend/app/tasks/page.tsx`
- 内容: 添加 onerror/onclose 处理，自动重连
- 依赖: 无

### T7: 构建验证
- 命令: `make build`
- 内容: 确保前后端编译通过
- 依赖: T1-T6

## 并行分析

- T1, T3, T4, T5, T6 可并行（无依赖）
- T2 依赖 T1
- T7 依赖所有任务
