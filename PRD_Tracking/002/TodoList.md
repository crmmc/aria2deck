# TodoList - 修复 P2/P3 问题

## 任务列表

1. [x][P] T1: 创建速率限制器模块 (`backend/app/core/rate_limit.py`)
2. [x][P] T3: 输入验证 - schemas.py (`backend/app/schemas.py`)
3. [x][P] T4: 配置缓存 (`backend/app/routers/config.py`)
4. [x][P] T5: 同步任务并发化 (`backend/app/aria2/sync.py`)
5. [x][P] T6: 符号链接检查 (`backend/app/routers/files.py`)
6. [x][P] T7: 前端数值输入验证 (`frontend/app/settings/page.tsx`)
7. [x][P] T8: 菜单活跃链接修复 (`frontend/components/Sidebar.tsx`)
8. [x][P] T9: 前端工具函数 (`frontend/lib/utils.ts`)
9. [x][P] T11: 磁盘空间缓存 (`backend/app/routers/files.py`)
10. [x][S] T2: 登录接口添加速率限制 (`backend/app/routers/auth.py`) [依赖: T1]
11. [x][S] T10: 应用工具函数到 settings 页面 (`frontend/app/settings/page.tsx`) [依赖: T9]
12. [x][S] T12: 构建验证 (`make build`) [依赖: 所有]

## 执行计划

- **第一批 (并行)**: T1, T3, T4, T5, T6, T7, T8, T9, T11 (9个任务)
- **第二批 (串行)**: T2, T10 (可并行，但依赖不同)
- **第三批 (串行)**: T12 (验证构建)

## 状态说明

- `[ ]` 未开始
- `[-]` 进行中
- `[x]` 已完成
- `[E]` 失败
- `[P]` 并行任务
- `[S]` 串行任务
