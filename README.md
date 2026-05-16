# Aria2Deck

多用户下载管理平台，基于 aria2，适合个人和小团队。

一套 Web 界面搞定：添加任务、管理文件、多人协作、分享下载。

---

## 功能亮点

**下载管理**
- 支持磁力链接、种子文件、HTTP/FTP 链接，可批量添加
- WebSocket 实时推送任务状态和下载速度
- 任务完成/失败桌面通知提醒

**多用户隔离**
- 每个用户独立的任务列表和文件空间
- 管理员可分配用户存储配额
- 同一资源多人下载时自动复用，节省空间和带宽

**文件管理**
- 在线浏览、重命名、下载、删除已下载文件
- 多文件打包下载（ZIP / TAR.ZST）
- 存储空间用量实时展示

**文件分享**
- 生成分享链接，支持密码保护
- 可设置过期时间或限制下载次数
- 随时撤销分享

**外部客户端接入**
- 兼容 aria2 RPC 协议，可对接 AriaNg、Motrix 等客户端
- 每用户独立 RPC Secret，互不干扰

**系统管理**
- 用户管理、系统配置、磁盘空间监控
- 站点标题自定义
- 孤立文件检测与清理

---

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/crmmc/aria2deck.git
cd aria2deck
make docker-up
```

浏览器打开 `http://localhost:8001`

容器部署时，对外 HTTP 服务由 FastAPI/Uvicorn 提供；前端会先静态导出，再由后端统一托管。

默认账号：`admin` / `123456`，首次登录后请**立即修改密码**。

### 独立 Docker 运行

```bash
docker run -d \
  --name aria2deck \
  --network host \
  -e ARIA2C_DOWNLOAD_DIR=/Downloads/aria2deck \
  -v /your/data:/app/backend/data \
  -v /Downloads/aria2deck:/Downloads/aria2deck \
  ghcr.io/crmmc/aria2deck:<tag>
```

> `ARIA2C_DOWNLOAD_DIR` 自定义下载目录，需确保该路径在容器内可访问。

---

## 使用流程

1. 管理员登录，修改默认密码
2. 粘贴下载链接（或上传种子文件），创建任务
3. 下载完成后，在文件页面浏览和管理
4. 需要多人使用时，在用户管理创建新账号
5. 需要分享文件时，生成分享链接发给对方

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ARIA2C_ARIA2_RPC_URL` | aria2 RPC 地址 | `http://localhost:6800/jsonrpc` |
| `ARIA2C_ARIA2_RPC_SECRET` | aria2 RPC 密钥 | - |
| `ARIA2C_DOWNLOAD_DIR` | 下载文件存放目录 | `/app/backend/downloads` |
| `ARIA2C_DATABASE_PATH` | 数据库文件路径 | `./data/app.db` |
| `ARIA2C_SESSION_TTL_SECONDS` | 登录会话有效期（秒） | `43200` |
| `ARIA2C_ARIA2_POLL_INTERVAL` | aria2 状态轮询间隔（秒） | `2.0` |
| `ARIA2DECK_SHARE_JWT_SECRET` | 分享链接签名密钥 | - |
| `ARIA2C_EXTRA_CORS_ORIGINS` | 额外允许的 CORS 域名 | - |

---

## 数据持久化

Docker 部署时需要挂载两个目录：

| 容器路径 | 说明 |
|---------|------|
| `/app/backend/data` | 数据库文件，**必须备份** |
| `/app/backend/downloads` | 下载文件存放 |

---

## 安全建议

- 修改默认管理员密码
- 设置强随机的 `ARIA2C_ARIA2_RPC_SECRET` 和 `ARIA2DECK_SHARE_JWT_SECRET`
- 生产环境请通过反向代理（Nginx 等）访问，不要直接暴露到公网
- 定期备份 `data/` 目录

---

## 本地开发

```bash
# 安装依赖
make install

# 配置环境变量
cp backend/env.example backend/.env

# 三个终端分别启动
make dev-aria2    # aria2 后端
make dev-back     # API 后端（开发模式，自动重置 admin 密码为 123456）
make dev-front    # 前端开发服务器（http://localhost:3000）
```

开发态页面路径统一使用无后缀路由，例如 `http://localhost:3000/tasks`、`/files`、`/settings`。
生产态也保持相同的无后缀 URL；`.html` 仅是静态导出产物细节，不应出现在前端源码或手动访问路径中。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + SQLModel + aiohttp |
| 前端 | Next.js 14 + TypeScript |
| 数据库 | SQLite |
| 下载引擎 | aria2 (JSON-RPC) |
| 部署 | Docker |

---

## 许可协议

[MIT License](LICENSE)
