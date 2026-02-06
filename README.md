# Aria2Deck

一个面向个人/小团队的多用户下载管理平台，基于 aria2，提供网页界面、用户隔离、任务共享与文件管理能力。

如果你想要的是：
- 不用一直盯着命令行
- 多人共用一套下载服务
- 下载完能统一在网页里管理文件

那这个项目就是给你准备的。

---

## 你能用它做什么

- 用网页添加下载任务（磁力、种子、HTTP/FTP）
- 多用户登录，各自管理自己的任务和文件
- 同一资源自动复用下载结果，省时间和空间
- 下载完成后直接在网页里浏览、重命名、下载、删除
- 支持多文件打包下载（ZIP/7z）

---

## 快速开始（推荐：Docker）

> 适合大多数用户，最省事。

### 1) 准备环境

请确保机器上有：
- Docker
- Docker Compose（`docker compose` 命令可用）

### 2) 启动服务

```bash
make docker-up
```

### 3) 打开页面

浏览器访问：

```text
http://localhost:8000
```

默认管理员账号：
- 用户名：`admin`
- 密码：`123456`

首次登录后请**立即修改密码**。

---

## 本地运行（开发/调试）

> 你想在本机直接改代码、看效果，就用这套。

### 1) 安装依赖

```bash
make install
```

### 2) 配置环境变量

```bash
cp backend/env.example backend/.env
cp frontend/env.local.example frontend/.env.local
```

### 3) 构建前端

```bash
make build
```

### 4) 启动服务（开发推荐开三个终端）

```bash
# 终端 1：启动 aria2 测试后端
make dev-aria2

# 终端 2：启动后端开发模式（详细日志）
make dev-back

# 终端 3：启动前端开发环境
make dev-front
```

`make dev-back` 会在每次启动时将 `admin` 密码重置为 `123456`，只改密码相关字段，不会清空数据库配置。

默认访问地址：
- 后端：`http://localhost:8000`
- 前端：`http://localhost:3000`

---

## 新手使用流程（3 分钟版）

1. 管理员登录后，先改默认密码。
2. 进入任务页面，粘贴下载链接（或上传种子）创建任务。
3. 下载完成后，到文件页面管理文件。
4. 需要多人使用时，管理员在用户管理里创建账号。

---

## 常用命令

```bash
# 安装依赖
make install

# 构建前端静态资源
make build

# 启动 aria2 测试后端（前台运行）
make dev-aria2

# 启动后端开发模式（详细日志，自动重置 admin 密码）
make dev-back

# 启动后端标准日志模式（兼容旧命令，不重置密码）
make run

# 启动前端开发环境
make dev-front

# 查看 Docker 日志
make docker-logs

# 停止 Docker 服务
make docker-down
```

---

## 常见问题

### 1) 页面打不开怎么办？

先检查服务是否启动，再确认端口是否被占用。

- 本地模式：确认 `make run` 没报错
- Docker 模式：执行 `make docker-logs` 查看日志

### 2) 提示连不上 aria2 怎么办？

通常是 RPC 地址或密钥不一致：
- 检查 `backend/.env` 里的 `ARIA2C_ARIA2_RPC_URL`
- 检查 `ARIA2C_ARIA2_RPC_SECRET` 是否和 aria2 配置一致

### 3) 为什么文件列表里看不到刚下载的内容？

先看任务是否已完成；未完成的任务不会出现在最终文件列表。

---

## 安全建议（强烈建议）

- 修改默认管理员密码
- 使用强随机的 RPC Secret
- 生产环境不要裸露在公网，至少加一层反向代理和访问控制
- 定期备份 `backend/data/` 下的数据库文件

---

## 目录说明（用户视角）

- `backend/`：后端服务（API、认证、任务调度）
- `frontend/`：网页前端
- `backend/static/`：前端构建后的静态文件
- `backend/data/`：数据库与运行数据（重要，记得备份）

---

## 许可协议

本项目使用 `MIT License`。
