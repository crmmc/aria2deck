# ChangeLogs

## 2026-01-22 - 修复 Critical 安全/健壮性问题

### 任务编号: 001

### 变更内容

| 问题 | 修复 | 文件 |
|------|------|------|
| aria2 回调接口无认证 | 添加 `X-Hook-Secret` Header 验证 | `routers/hooks.py` |
| SQLite 并发写入不安全 | 添加 `threading.Lock` 写锁 | `db.py` |
| HTTP 请求无超时 | 添加 30 秒超时 | `aria2/client.py` |
| 时区比较陷阱 | 确保 datetime 有时区信息 | `auth.py` |
| WebSocket 无重连 | 添加 onerror/onclose 处理，3 秒重连 | `tasks/page.tsx` |

### 新增配置项

| 配置 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `hook_secret` | `ARIA2C_HOOK_SECRET` | `""` | aria2 回调认证密钥，为空时不验证 |

### 影响范围

- 后端: 5 个文件修改
- 前端: 1 个文件修改
- 向后兼容: 是（新配置默认为空，不影响现有部署）

### 验收标准

- [x] `make build` 编译通过
- [x] 代码风格一致
- [x] 无破坏性变更

---

## 2026-01-22 - 修复 P2/P3 代码质量问题

### 任务编号: 002

### 变更内容

#### P2 (Important) 修复

| 问题 | 修复 | 文件 |
|------|------|------|
| 登录无速率限制 | 基于 IP 的速率限制器 (5次/5分钟) | `core/rate_limit.py`, `routers/auth.py` |
| 输入验证缺失 | Pydantic Field 约束 (长度/范围) | `schemas.py` |
| 配置查询无缓存 | 60 秒 TTL 缓存 | `routers/config.py` |
| 同步任务串行执行 | asyncio.gather 并发化 | `aria2/sync.py` |
| 路径验证无符号链接检查 | 符号链接解析+边界校验 | `routers/files.py` |
| 前端数值输入无限制 | min/max 属性约束 | `settings/page.tsx` |

#### P3 (Nit) 修复

| 问题 | 修复 | 文件 |
|------|------|------|
| 菜单活跃链接逻辑有缺陷 | 精确匹配 + 前缀匹配 | `components/Sidebar.tsx` |
| 配额单位转换重复代码 | 工具函数抽取 | `lib/utils.ts` (新建) |
| 目录大小计算性能差 | 30 秒 TTL 缓存 | `routers/files.py` |

### 新增文件

| 文件 | 说明 |
|------|------|
| `backend/app/core/rate_limit.py` | 登录速率限制器模块 |
| `frontend/lib/utils.ts` | 前端工具函数 (bytesToGB, gbToBytes, formatBytes) |

### 未修复项 (设计决定)

| 问题 | 原因 |
|------|------|
| PBKDF2 迭代次数 | 120000 已符合 OWASP 2023 标准 |
| SecretStr 类型 | 影响范围大，当前实现已满足需求 |

### 影响范围

- 后端: 6 个文件修改，1 个新建
- 前端: 2 个文件修改，1 个新建
- 向后兼容: 是

### 验收标准

- [x] `make build` 编译通过
- [x] 所有 12 个子任务完成
- [x] 代码风格一致
- [x] 无破坏性变更
