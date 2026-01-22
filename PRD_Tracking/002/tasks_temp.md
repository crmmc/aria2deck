# 任务拆分草稿

## 原子任务列表

### T1: 创建速率限制器模块
- 文件: `backend/app/core/rate_limit.py` (新建)
- 内容: LoginRateLimiter 类
- 依赖: 无

### T2: 登录接口添加速率限制
- 文件: `backend/app/routers/auth.py`
- 内容: 集成速率限制器
- 依赖: T1

### T3: 输入验证 - schemas.py
- 文件: `backend/app/schemas.py`
- 内容: 添加 Field 约束
- 依赖: 无

### T4: 配置缓存
- 文件: `backend/app/routers/config.py`
- 内容: 添加 _config_cache 和 TTL 逻辑
- 依赖: 无

### T5: 同步任务并发化
- 文件: `backend/app/aria2/sync.py`
- 内容: 使用 asyncio.gather
- 依赖: 无

### T6: 符号链接检查
- 文件: `backend/app/routers/files.py`
- 内容: 修改 _validate_path 函数
- 依赖: 无

### T7: 前端数值输入验证
- 文件: `frontend/app/settings/page.tsx`
- 内容: 添加 min/max 属性
- 依赖: 无

### T8: 菜单活跃链接修复
- 文件: `frontend/components/Sidebar.tsx`
- 内容: 修改 isActive 逻辑
- 依赖: 无

### T9: 前端工具函数
- 文件: `frontend/lib/utils.ts` (新建)
- 内容: bytesToGB, gbToBytes 函数
- 依赖: 无

### T10: 应用工具函数到 settings 页面
- 文件: `frontend/app/settings/page.tsx`
- 内容: 使用工具函数替换重复代码
- 依赖: T9

### T11: 磁盘空间缓存
- 文件: `backend/app/routers/files.py`
- 内容: 添加 _dir_size_cache
- 依赖: 无

### T12: 构建验证
- 命令: `make build`
- 依赖: 所有

## 并行分析

- 第一批并行: T1, T3, T4, T5, T6, T7, T8, T9, T11
- 第二批串行: T2 (依赖 T1)
- 第三批串行: T10 (依赖 T9)
- 第四批串行: T12 (依赖所有)
