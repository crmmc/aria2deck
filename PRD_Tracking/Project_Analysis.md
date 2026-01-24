# Project Analysis - aria2 Controller

## 📂 项目结构与布局

```
aria2_controler/
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口，中间件，路由注册
│   │   ├── auth.py           # Session Cookie 认证
│   │   ├── db.py             # SQLite 操作（原生 SQL）
│   │   ├── schemas.py        # Pydantic 请求/响应模型
│   │   ├── core/
│   │   │   ├── config.py     # pydantic-settings 配置
│   │   │   ├── security.py   # 密码哈希
│   │   │   └── state.py      # 应用状态管理
│   │   ├── routers/          # API 路由
│   │   │   ├── auth.py       # 登录/登出/当前用户
│   │   │   ├── users.py      # 用户管理 (管理员)
│   │   │   ├── tasks.py      # 任务管理
│   │   │   ├── files.py      # 文件浏览器
│   │   │   ├── stats.py      # 系统统计
│   │   │   ├── config.py     # 系统配置 (管理员)
│   │   │   ├── hooks.py      # aria2 回调
│   │   │   └── ws.py         # WebSocket
│   │   └── aria2/
│   │       ├── client.py     # JSON-RPC 客户端
│   │       └── sync.py       # 后台同步任务
│   ├── aria2/                # aria2 配置文件
│   ├── data/                 # SQLite DB (gitignored)
│   ├── static/               # 前端构建产物 (gitignored)
│   └── downloads/            # 用户下载目录
├── frontend/
│   ├── app/                  # Next.js App Router
│   ├── components/           # React 组件
│   ├── lib/                  # API 客户端，工具函数
│   └── types.ts              # TypeScript 类型定义
├── Makefile                  # 构建脚本
├── pyproject.toml            # 后端依赖
└── uv.lock                   # 依赖锁定
```

## 🎨 代码风格规范

### Python
- 类型注解：必须使用，优先 `|` 语法
- 导入顺序：stdlib → third-party → local
- 命名：函数/变量 `snake_case`，类 `PascalCase`，私有 `_prefix`
- 数据库：原生 SQL + 参数化查询，禁止字符串拼接
- 错误：HTTPException + 中文 detail

### TypeScript
- 路径别名：`@/types`, `@/lib/api`, `@/components/`
- 组件：函数式 + TypeScript props
- 客户端组件：`"use client"` 指令
- API 调用：统一使用 `api` 对象

## 📦 技术栈分析

| 层级 | 技术 | 版本 |
|------|------|------|
| 后端框架 | FastAPI | 0.111.0 |
| 服务器 | uvicorn | 0.30.1 |
| HTTP 客户端 | aiohttp | 3.9.5 |
| 配置 | pydantic-settings | 2.3.4 |
| 数据库 | SQLite | 原生 SQL |
| 前端框架 | Next.js | 14.2.5 |
| 运行时 | React | 18.3.1 |
| 语言 | TypeScript | 5.5.2 |
| 后端包管理 | uv | - |
| 前端包管理 | Bun | - |

## 🏗️ 架构设计

### 认证机制
- Cookie-based Session 认证 (`aria2_session`)
- Sessions 存储在 SQLite `sessions` 表
- 依赖注入：`require_user`, `require_admin`

### 数据流
```
Frontend → API → SQLite (任务元数据)
                → aria2 RPC (下载控制)
                ← WebSocket (实时更新)
```

### 前端构建
- 静态导出模式 (`output: 'export'`)
- 构建产物复制到 `backend/static/`
- FastAPI 中间件处理 SPA 路由

## ⚙️ 工程实践

### 构建命令
```bash
make install  # 安装所有依赖
make build    # 编译前端 → static
make run      # 启动服务 (port 8000)
make clean    # 清理构建产物
```

### 环境变量 (prefix: ARIA2C_)
- `ARIA2C_DEBUG`: 调试模式
- `ARIA2C_DATABASE_PATH`: 数据库路径
- `ARIA2C_SESSION_TTL_SECONDS`: Session 有效期
- `ARIA2C_ARIA2_RPC_URL`: aria2 RPC 地址
- `ARIA2C_ARIA2_RPC_SECRET`: aria2 RPC 密钥

## 🧪 测试策略

> 当前无测试框架配置。计划使用：
> - 后端: pytest
> - 前端: 待定

## 🔧 遗留问题识别 (代码审查结果)

### Critical (P1) - ✅ 全部修复 (Task 001)
1. ~~`hooks.py` - aria2 回调接口无认证~~ → 添加 `X-Hook-Secret` 验证
2. ~~`db.py:19` - SQLite `check_same_thread=False` 并发不安全~~ → 添加写锁
3. ~~`aria2/client.py:14-26` - HTTP 请求无超时设置~~ → 添加 30s 超时
4. ~~`tasks/page.tsx:50-71` - WebSocket 无错误处理/重连~~ → 添加重连逻辑
5. ~~`auth.py:32-35` - 会话时区比较陷阱~~ → 确保 datetime 有时区

### Important (P2) - ✅ 全部修复 (Task 002)
1. ~~登录无速率限制~~ → IP 限流 (5次/5分钟)
2. ~~输入验证缺失~~ → Pydantic Field 约束
3. ~~配置查询无缓存~~ → 60s TTL 缓存
4. ~~同步任务顺序执行~~ → asyncio.gather 并发
5. ~~CORS 配置过于宽松~~ → 已修复 (仅开发域名)
6. ~~路径验证未检查符号链接~~ → 符号链接解析+边界校验
7. ~~数值输入无验证~~ → min/max 属性

### Nit (P3) - ✅ 部分修复 (Task 002)
- ~~PBKDF2 轮数偏低~~ → 120000 已符合 OWASP 2023 (保持)
- `aria2_rpc_secret` 建议 SecretStr → 暂不修改 (影响范围大)
- ~~菜单活跃链接判断可能误判~~ → 精确匹配+前缀匹配
- ~~配额单位转换重复代码~~ → 提取 utils.ts 工具函数
- ~~磁盘空间计算性能~~ → 30s TTL 缓存

## 🚀 UX 增强功能 (Task 003)

### 新增功能

| 功能 | 说明 | 状态 |
|------|------|------|
| BT 种子上传 | 支持上传 .torrent 文件创建下载任务 | ✅ 已完成 |
| 浏览器通知 | 任务完成/出错时发送桌面通知 | ✅ 已完成 |
| 任务拖拽排序 | 拖拽调整 waiting 任务的队列位置 | ✅ 已完成 |

### 新增 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/tasks/torrent` | POST | 上传种子文件创建任务 |
| `/api/tasks/{id}/position` | PUT | 调整任务队列位置 |

### 新增前端依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| `@dnd-kit/core` | ^6.3.1 | 拖拽核心库 |
| `@dnd-kit/sortable` | ^10.0.0 | 列表排序 |

## 🔑 关键发现总结

1. **安全问题**：✅ 全部修复
2. **健壮性**：✅ 全部修复
3. **性能**：✅ 全部修复
4. **代码质量**：整体良好，遵循项目规范
5. **用户体验**：✅ 三项增强功能已完成
