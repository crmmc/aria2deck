# TodoList - 修复 Critical 问题

## 任务列表

1. [x][P] T1: 添加 hook_secret 配置项 (`backend/app/core/config.py`)
2. [x][P] T3: db.py 添加写锁 (`backend/app/db.py`)
3. [x][P] T4: aria2/client.py 添加超时 (`backend/app/aria2/client.py`)
4. [x][P] T5: auth.py 修复时区处理 (`backend/app/auth.py`)
5. [x][P] T6: tasks/page.tsx WebSocket 重连 (`frontend/app/tasks/page.tsx`)
6. [x][S] T2: hooks.py 添加认证逻辑 (`backend/app/routers/hooks.py`) [依赖: T1]
7. [x][S] T7: 构建验证 (`make build`) [依赖: 所有]

## 执行计划

- **第一批 (并行)**: T1, T3, T4, T5, T6
- **第二批 (串行)**: T2 (依赖 T1)
- **第三批 (串行)**: T7 (验证构建)

## 状态说明

- `[ ]` 未开始
- `[-]` 进行中
- `[x]` 已完成
- `[E]` 失败
- `[P]` 并行任务
- `[S]` 串行任务
