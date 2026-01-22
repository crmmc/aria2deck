# aria2 本地测试环境

本目录包含用于本地开发测试的 aria2 配置和启动脚本。

## 目录结构

```
backend/aria2/
├── aria2.conf       # aria2 配置文件
├── start.sh         # 启动脚本（前台运行）
├── aria2.log        # 日志文件（运行时生成）
├── aria2.session    # 会话文件（运行时生成）
└── README.md        # 本文件
```

## 安装 aria2

### macOS

```bash
brew install aria2
```

### Ubuntu/Debian

```bash
sudo apt-get update
sudo apt-get install aria2
```

### CentOS/RHEL

```bash
sudo yum install aria2
```

## 启动 aria2 服务

在项目根目录执行：

```bash
./backend/aria2/start.sh
```

服务将在前台运行，按 `Ctrl+C` 停止。

## 配置说明

### RPC 设置

- **RPC 地址**: `http://localhost:6800/jsonrpc`
- **RPC 端口**: `6800`
- **RPC 密钥**: 默认为空（无密钥）

### 下载设置

- **下载目录**: `backend/downloads`
- **最大并发下载**: 5
- **单服务器最大连接数**: 16
- **分片数**: 16

### 日志和会话

- **日志文件**: `backend/aria2/aria2.log`
- **会话文件**: `backend/aria2/aria2.session`

## 修改配置

编辑 `backend/aria2/aria2.conf` 文件，然后重启 aria2 服务。

### 常用配置项

#### 设置 RPC 密钥

```conf
rpc-secret=your_secret_key
```

#### 限制下载速度（单位：字节/秒）

```conf
max-overall-download-limit=1M
max-download-limit=500K
```

#### 修改下载目录

```conf
dir=/path/to/your/download/directory
```

## 测试 aria2 服务

### 1. 检查服务状态

```bash
curl http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","method":"aria2.getVersion","id":"1"}'
```

应该返回类似：

```json
{"id":"1","jsonrpc":"2.0","result":{"enabledFeatures":["Async DNS","BitTorrent","Firefox3 Cookie","GZip","HTTPS","Message Digest","Metalink","XML-RPC"],"version":"1.36.0"}}
```

### 2. 添加下载任务

```bash
curl http://localhost:6800/jsonrpc -d '{
  "jsonrpc":"2.0",
  "method":"aria2.addUri",
  "id":"1",
  "params":[["http://example.com/file.zip"]]
}'
```

### 3. 查看活跃任务

```bash
curl http://localhost:6800/jsonrpc -d '{
  "jsonrpc":"2.0",
  "method":"aria2.tellActive",
  "id":"1"
}'
```

## 与 aria2 控制器集成

确保后端配置文件中的 aria2 RPC 地址正确：

```bash
# backend/env.example 或环境变量
ARIA2C_ARIA2_RPC_URL=http://localhost:6800/jsonrpc
ARIA2C_ARIA2_RPC_SECRET=
```

## 常见问题

### Q: 端口 6800 已被占用

**A**: 修改 `aria2.conf` 中的 `rpc-listen-port` 配置，同时更新后端配置。

### Q: 下载速度很慢

**A**: 检查以下配置：

- `max-connection-per-server`: 增加单服务器连接数
- `split`: 增加分片数
- `min-split-size`: 减小最小分片大小

### Q: BT 下载没有速度

**A**:

- 确保 DHT 已启用：`enable-dht=true`
- 添加 BT tracker 列表到 `bt-tracker` 配置
- 检查防火墙是否允许 BT 端口（6881-6999）

### Q: 如何查看日志

**A**:

```bash
tail -f backend/aria2/aria2.log
```

## 停止服务

在运行 `start.sh` 的终端按 `Ctrl+C` 即可停止服务。

## 清理

删除下载文件和日志：

```bash
rm -rf backend/downloads/*
rm -f backend/aria2/aria2.log
rm -f backend/aria2/aria2.session
```
