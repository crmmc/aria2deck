<div align="center">

# Aria2Deck

**基于 aria2 的多用户下载管理平台**

一套 Web 界面搞定：添加任务、管理文件、多人协作、分享下载

[![Version](https://img.shields.io/badge/version-2.0.0-1e88e5.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-blue?logo=docker)](https://github.com/crmmc/aria2deck/pkgs/container/aria2deck)

</div>

<br>

## 功能亮点

**下载任务** — 支持磁力链接、种子文件、HTTP/HTTPS/FTP。可一次粘贴多个链接批量添加；上传种子时能预览完整文件树，勾选其中一部分下载。任务进度、速度曲线与状态变化通过 WebSocket 实时推送，完成或失败可触发浏览器桌面通知。

**任务历史** — 完整保留完成、失败、取消三种终态，按状态筛选、按名称搜索、支持单条重试与批量清理，并按保留天数自动回收。

**文件管理** — 目录结构浏览、文件名搜索、按时间或大小排序，支持重命名、下载、删除与批量操作。多选文件可打包成 ZIP 或 TAR.ZST 下载（格式与压缩级别由管理员在系统设置里指定），打包进度在「打包记录」抽屉里追踪，随时可取消。

**文件分享** — 生成分享链接，支持密码保护、有效期和下载次数上限，可单条撤销或一键失效全部。公开分享页免登录访问，同样支持目录浏览与逐文件下载。

**存储管理** — 管理员视角按内容哈希查看全部存储对象：大小、引用数、磁盘状态、引用用户，可清除无人引用的内容。

**未登记文件** — 扫描下载目录里磁盘上有、库里没登记的对象（升级丢数据、异常中断留下的残留），列出来交管理员决策：加入入库队列，由管理员挑合适时机开始校验入库，下次下载同内容时直接复用；或直接删除腾出空间。队列在后台逐条处理、可随时停止，处理中的条目也能看到当前进度。

**多用户隔离** — 每用户独立的任务列表、文件空间和存储配额，管理员可创建账号、分配配额、调整角色。同一资源被多人下载时自动复用已有内容，不重复占用磁盘和带宽。

**系统设置** — 基础配置、aria2 后端（连通性测试与版本查询）、打包、实时连接、Tracker 列表（支持远程源刷新）、安全与限流（各类接口限流、下载连接数，以及一键作废全部 API Token 与 RPC Secret）。

**外部客户端** — 内置 aria2 JSON-RPC 兼容端点，可对接 AriaNg、Motrix 等客户端；每用户独立 RPC Secret。

**界面** — 桌面端可折叠侧栏，移动端自动切换为底部导航；`⌘K` / `Ctrl+K` 调出快捷操作面板直接跳转功能与设置项。

<br>

## 界面预览

<table>
  <tr>
    <td align="center"><img src="docs/screenshots/tasks.png" width="400" alt="任务管理"><br><sub>任务管理</sub></td>
    <td align="center"><img src="docs/screenshots/files.png" width="400" alt="文件管理"><br><sub>文件管理</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/screenshots/shares.png" width="400" alt="分享管理"><br><sub>分享管理</sub></td>
    <td align="center"><img src="docs/screenshots/history.png" width="400" alt="任务历史"><br><sub>任务历史</sub></td>
  </tr>
</table>

<br>

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/crmmc/aria2deck.git
cd aria2deck
export ARIA2DECK_ARIA2_RPC_SECRET="$(openssl rand -hex 32)"
export ARIA2DECK_SHARE_JWT_SECRET="$(openssl rand -hex 32)"
export ARIA2DECK_CREDENTIAL_PEPPER="$(openssl rand -hex 32)"
read -rsp "初始管理员密码（至少 16 个字符）: " ARIA2DECK_INITIAL_ADMIN_PASSWORD
export ARIA2DECK_INITIAL_ADMIN_PASSWORD
printf '\n'
make docker-up
```

浏览器打开 `http://localhost:8001`，使用账号 `admin` 和上面设置的初始密码登录。已有管理员的升级部署可省略 `ARIA2DECK_INITIAL_ADMIN_PASSWORD`。

以上三个 secret 中，`ARIA2DECK_ARIA2_RPC_SECRET`、`ARIA2DECK_SHARE_JWT_SECRET`、`ARIA2DECK_CREDENTIAL_PEPPER` 缺失时 Compose 会直接拒绝启动；后两个必须各自至少 32 字节且互不相同，否则后端启动即报配置错误。

### 独立 Docker 运行

先按上文导出变量；已有管理员时可省略 `ARIA2DECK_INITIAL_ADMIN_PASSWORD`。

```bash
docker run -d \
  --name aria2deck \
  --network host \
  -e ARIA2DECK_HOST=127.0.0.1 \
  -e ARIA2DECK_ARIA2_RPC_URL=http://127.0.0.1:6800/jsonrpc \
  -e ARIA2DECK_ARIA2_RPC_SECRET \
  -e ARIA2DECK_SHARE_JWT_SECRET \
  -e ARIA2DECK_CREDENTIAL_PEPPER \
  -e ARIA2DECK_INITIAL_ADMIN_PASSWORD \
  -e ARIA2DECK_DOWNLOAD_DIR=/Downloads/aria2deck \
  -v /your/data:/app/backend/data \
  -v /Downloads/aria2deck:/Downloads/aria2deck \
  ghcr.io/crmmc/aria2deck:<tag>
```

<br>

## 使用流程

1. 管理员使用配置的初始密码登录，按页面提示修改密码
2. 粘贴下载链接、批量粘贴多行链接，或上传种子文件后勾选要下载的部分
3. 下载完成后在文件页浏览、重命名、打包或直接下载
4. 需要多人使用时，在用户管理创建账号并分配存储配额
5. 需要对外提供文件时，生成分享链接发给对方

<br>

## 从 V1 迁移

V2 是一次结构重写，用 V1 的数据库无法直接启动。仓库里带一个离线迁移工具，把 V1 的账号、文件内容、分享与系统设置搬到 V2。

先在数据所在的机器上预演，看清会迁移什么、会跳过什么：

```bash
cd backend/v2
uv run python -m tools.migrate_v1 \
  --v1-database /path/to/v1.db \
  --v1-download-dir /path/to/v1/downloads \
  --v2-root /path/to/new/v2-root \
  --dry-run
```

确认报告没问题后去掉 `--dry-run` 正式执行。产物在 `--v2-root` 下的 `data/v2.db` 与 `downloads/`，把它们放到 V2 部署的对应位置即可。

目标盘剩余空间装不下一份完整复制时，加 `--link` 用硬链接代替复制（零额外空间，要求 V1 下载目录与 `--v2-root` 在同一文件系统）；V1 数据依然原样保留，日后删除 V1 目录不影响 V2。

三件事需要提前知道：

- **V1 侧全程只读**，不改数据库、不动文件。失败重来 = 删掉目标目录再跑一次。
- **密码无法迁移**。迁移会给最早的管理员生成一个随机密码，**只在终端打印一次**，请立即保存。其余账号写为「锁定」状态，管理员在「用户管理」页逐个「重置密码」后才能登录。
- **带密码的分享会变成无密码**，各用户的外部客户端 RPC Secret 也会失效、需重新生成，报告中会逐条列出。

<br>

## 账号状态

每个账号任一时刻只处于一种状态，管理员在「用户管理」页可以看见并操作：

| 状态 | 含义 | 登录结果 | 恢复方式 |
|---|---|---|---|
| 正常 | 可正常使用 | 允许 | — |
| 冻结 | 管理员临时停用 | 403「该用户已冻结，详情请联系管理员」 | 管理员点「解冻」 |
| 锁定 | 从旧版迁移或结构升级后等待重置密码 | 403「该用户已锁定，请联系管理员轮换新的密码」 | 管理员点「重置密码」 |

- 冻结、锁定和重置密码都会立即撤销该账号的全部浏览器会话，并让它已有的 API Token 与 RPC Secret 无法继续使用；解冻不会恢复旧会话，需要重新登录。
- 账号下的文件、公开分享和已经在运行的任务不受影响，也不会被自动取消。
- 「重置密码」由系统生成一次性随机密码，**只显示一次**并提供复制。锁定账号重置后自动恢复为正常；冻结账号重置后仍是冻结，需要先解冻才能登录。
- 用户在「编辑用户」里手动填写新密码，效果与「重置密码」相同：旧的密码和会话失效，锁定账号随之解锁，冻结账号保持冻结。
- 管理员不能冻结自己的账号，也不能冻结最后一名仍可用的管理员。

<br>

## 已有数据库的结构升级

这一版新增 `users.status` 字段（schema 2）。应用**不会**自动升级旧数据库：直接启动新版本会因结构不一致而拒绝启动。升级步骤是停止应用 → 执行一次性命令 → 启动应用。

Docker Compose 部署：

```bash
docker compose stop app
docker compose run --rm --no-deps app uv run --no-sync python -m aria2deck.infrastructure.sqlite.upgrade
docker compose up -d app
```

非容器部署，在 `backend/v2` 下执行：

```bash
uv run python -m aria2deck.infrastructure.sqlite.upgrade
```

命令读取 `ARIA2DECK_DATABASE_PATH` 定位数据库，行为如下：

1. 取不到 `<数据库路径>.lock` 实例锁就退出——应用还在运行是升级不了库的。
2. 校验 SQLite 完整性与版本：只接受 schema 1 或已经升级完成的 schema 2，其余结构与版本一律拒绝，不做任何写入。
3. 用 SQLite 备份 API 在数据目录生成 `v2-pre-status-upgrade-<UTC 时间>.db`，同名文件已存在时自动换名，不会覆盖旧备份。
4. 在单个事务里重建 `users` 表：仍使用迁移占位密码的账号写为锁定，其余写为正常，最后把版本写为 2。任何一步失败都回滚，库保持升级前状态。
5. 提交后按目标结构逐字校验，并打印备份路径与正常/锁定账号数量（不打印密码哈希）。

对已经升级完成的库重复执行只会校验并报告，不会重新回填，因此已经被管理员解锁的账号不会被再次锁定。

回滚：先停止应用，把备份文件复制回 `v2.db`，删除（或改名隔离）同目录下旧的 `v2.db-wal` 与 `v2.db-shm`，再用旧版本镜像启动。备份文件务必保留，不要覆盖或删除唯一一份。

<br>

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.12 + FastAPI + SQLAlchemy Core + aiohttp |
| 前端 | React 18 + Vite 6 + TypeScript |
| 数据库 | SQLite |
| 下载引擎 | aria2（JSON-RPC） |
| 打包 | zipfile / tar + zstandard |
| 部署 | Docker + Docker Compose |

<br>

<details>
<summary><strong>环境变量</strong></summary>

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ARIA2DECK_ARIA2_RPC_URL` | aria2 RPC 地址 | `http://127.0.0.1:6800/jsonrpc` |
| `ARIA2DECK_ARIA2_RPC_SECRET` | aria2 RPC 密钥 | - |
| `ARIA2DECK_DATA_DIR` | 数据根目录 | `~/.local/share/aria2deck` |
| `ARIA2DECK_DATABASE_PATH` | 数据库文件路径 | `<数据根目录>/v2.db` |
| `ARIA2DECK_DOWNLOAD_DIR` | 下载文件存放目录 | `<数据根目录>/downloads` |
| `ARIA2DECK_STATIC_DIR` | 前端静态文件目录 | `static` |
| `ARIA2DECK_INTERNAL_BASE_URL` | 交给 aria2 的内部回源地址前缀，HTTP 下载经此网关做校验 | `http://127.0.0.1:8001` |
| `ARIA2DECK_SHARE_JWT_SECRET` | 分享链接与短时访问令牌签名密钥，非调试模式必填，至少 32 字节 | - |
| `ARIA2DECK_CREDENTIAL_PEPPER` | API Token 与用户 RPC Secret 摘要 pepper，非调试模式必填，至少 32 字节且不能与分享密钥相同 | - |
| `ARIA2DECK_INITIAL_ADMIN_PASSWORD` | 首次创建管理员时使用，至少 16 个字符 | - |
| `ARIA2DECK_DEBUG` | 调试模式：允许不配分享/凭证密钥、Cookie 关闭 Secure、首次启动不强制初始管理员密码；生产必须保持 `false` | `false` |
| `ARIA2C_RPC_ALLOW_ALL_ORIGINS` | 允许任意来源跨域访问 `/aria2/jsonrpc`（AriaNg、Motrix 等前端部署在其他域名时需要）；RPC 密钥认证仍必需，响应不带 Cookie 凭据 | `false` |
| `ARIA2C_RPC_ALLOWED_ORIGINS` | 额外允许的 RPC 跨域来源，逗号分隔精确匹配 | - |

容器内还接受 `ARIA2DECK_HOST` 与 `ARIA2DECK_PORT`，分别控制监听地址与端口，默认 `0.0.0.0:8001`。

</details>

<details>
<summary><strong>健康检查</strong></summary>

- `GET /api/health` 与 `GET /api/health/live` 只表示服务进程存活，不检查依赖。
- `GET /api/health/ready` 检查 SQLite、下载目录和同步、监听、打包、删除等后台 worker；容器 health check 使用此端点，任一 worker 异常时返回 503。

</details>

<details>
<summary><strong>数据持久化</strong></summary>

Docker 部署时需要挂载两个目录：

| 容器路径 | 说明 |
|---------|------|
| `/app/backend/data` | 数据库文件（`v2.db`），**必须备份** |
| `/app/backend/downloads` | 下载文件与打包产物 |

同一数据库文件只允许一个后端实例，启动时会获取 `<数据库路径>.lock` 实例锁，重复启动会直接失败。

</details>

<details>
<summary><strong>安全建议</strong></summary>

- 三个 secret 使用强随机且**互不相同**的值。`ARIA2DECK_SHARE_JWT_SECRET` 只签分享与内部短时令牌；`ARIA2DECK_CREDENTIAL_PEPPER` 只保护 API Token 与用户 RPC Secret 摘要，二者共用会被配置校验拒绝。
- 轮换 `ARIA2DECK_CREDENTIAL_PEPPER` 后，旧 API Token / RPC Secret 摘要无法再验证。正确做法：
  1. 备份数据库
  2. 配置新的 `ARIA2DECK_SHARE_JWT_SECRET` 与 `ARIA2DECK_CREDENTIAL_PEPPER`（各至少 32 字节、互不相同）
  3. 启动服务后，管理员在「系统设置 → 安全与限流」点击作废全部 API Token / RPC Secret
  4. 通知用户重新启用外部访问以获取新的 RPC Secret
- Compose 默认仅在 `127.0.0.1:8001` 提供 Web 服务，且不向宿主机发布 aria2 RPC 6800 端口。
- 远程访问必须使用提供 TLS 的反向代理；客户端 PBKDF2 结果仍是可重放的密码等价物，不能替代传输加密。
- 定期备份 `data/` 目录。

</details>

<details>
<summary><strong>本地开发</strong></summary>

依赖 `uv`（Python）与 `bun`（前端）：

```bash
make install          # 安装前后端依赖
```

代码位于 `backend/v2`（后端）与 `frontend/v2`（前端），`v1` 目录是旧版实现，不参与构建。

三个终端分别启动：

```bash
# 终端 1：本地 aria2。两边的 RPC secret 必须是同一个值
export ARIA2DECK_ARIA2_RPC_SECRET="$(openssl rand -hex 32)"
make dev-aria2

# 终端 2：后端（调试模式，跳过 secret 强制校验）
export ARIA2DECK_ARIA2_RPC_SECRET="<与终端 1 相同>"
read -rsp "开发管理员密码（至少 16 个字符）: " ARIA2DECK_INITIAL_ADMIN_PASSWORD
export ARIA2DECK_INITIAL_ADMIN_PASSWORD
make dev-back

# 终端 3：前端
make dev-front
```

后端在 `8001` 提供完整服务（含前端构建产物）。`make dev-front` 起 Vite 开发服务器（`http://localhost:3000`），它会把 `/api` 与 `/ws` 代理到 `8001` 的后端，改前端代码即时热更新；要看构建后的完整界面请访问 `http://localhost:8001`。

本地运行产生的数据默认落在 `~/.local/share/aria2deck`（`v2.db` 与 `downloads/`），需要放在项目内时用 `ARIA2DECK_DATA_DIR` 指定。

常用命令与测试：

```bash
make run                      # 构建前端并在 8001 端口启动完整服务
make build                    # 前端改动后重新构建并部署静态文件到后端
make check                    # lint + 类型检查 + 测试
cd backend/v2 && uv run pytest   # 后端测试
cd frontend/v2 && bun run test   # 前端测试
```

</details>

<br>

## 许可协议

[MIT License](LICENSE)
