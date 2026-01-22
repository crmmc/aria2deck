# aria2 RPC 接口文档

> 基于 aria2 1.37.0 官方文档整理

## 目录

- [概述](#概述)
- [术语](#术语)
- [RPC 接口类型](#rpc-接口类型)
- [认证](#认证)
- [方法列表](#方法列表)
- [通知](#通知)
- [选项](#选项)
- [错误处理](#错误处理)
- [示例](#示例)

---

## 概述

aria2 提供了 JSON-RPC 和 XML-RPC 接口，用于远程控制下载任务。支持以下传输方式：

- **JSON-RPC over HTTP**: 请求路径 `/jsonrpc`
- **JSON-RPC over WebSocket**: WebSocket URI `ws://HOST:PORT/jsonrpc` (SSL: `wss://HOST:PORT/jsonrpc`)
- **XML-RPC over HTTP**: 请求路径 `/rpc`

JSON-RPC 基于 [JSON-RPC 2.0 规范](http://jsonrpc.org/specification)，支持 HTTP POST、GET (JSONP) 和 WebSocket。

---

## 术语

### GID (全局标识符)

- **定义**: 每个下载任务的唯一标识符
- **格式**: 16 字符的十六进制字符串 (例如: `2089b05ecca3d829`)
- **生成**: aria2 自动生成，也可通过 `--gid` 选项手动指定
- **查询**: 可以只指定 GID 的前缀，只要它在所有 GID 中是唯一的

### 下载状态

- `active`: 正在下载/做种
- `waiting`: 在队列中等待，尚未开始
- `paused`: 已暂停
- `error`: 因错误停止
- `complete`: 已完成并停止
- `removed`: 被用户移除

---

## RPC 接口类型

### JSON-RPC over HTTP

**端点**: `http://HOST:PORT/jsonrpc`

**请求示例**:

```python
import urllib2, json

jsonreq = json.dumps({
    'jsonrpc': '2.0',
    'id': 'qwer',
    'method': 'aria2.tellStatus',
    'params': ['2089b05ecca3d829']
})
c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
response = json.loads(c.read())
```

### JSON-RPC over WebSocket

**端点**: `ws://HOST:PORT/jsonrpc` (SSL: `wss://HOST:PORT/jsonrpc`)

**特性**:

- 支持服务器主动推送通知
- 使用与 HTTP 相同的方法签名和响应格式
- 通过 Text 帧发送和接收 JSON 字符串

### XML-RPC over HTTP

**端点**: `http://HOST:PORT/rpc`

**请求示例**:

```python
import xmlrpclib

s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
r = s.aria2.tellStatus('2089b05ecca3d829')
```

---

## 认证

### RPC 密钥令牌认证 (推荐)

从 aria2 1.18.4 开始，支持方法级别的认证。

**配置**:

```bash
aria2c --enable-rpc --rpc-secret=$$secret$$
```

**使用**:
每次 RPC 调用时，第一个参数必须是 `token:` 前缀加上密钥。

**示例**:

```python
# JSON-RPC
jsonreq = json.dumps({
    'jsonrpc': '2.0',
    'id': 'qwer',
    'method': 'aria2.addUri',
    'params': ['token:$$secret$$', ['http://example.org/file']]
})

# XML-RPC
s.aria2.addUri('token:$$secret$$', ['http://example.org/file'])
```

**注意**:

- 即使未设置 `--rpc-secret`，如果第一个参数以 `token:` 开头，它会被自动移除
- `system.multicall` 方法中，每个嵌套方法调用都需要提供令牌

### HTTP 基本认证 (已弃用)

使用 `--rpc-user` 和 `--rpc-passwd` 选项。未来版本将移除此功能。

---

## 方法列表

### 1. aria2.addUri

**功能**: 添加新的 HTTP/FTP/SFTP/BitTorrent 下载任务

**参数**:

- `[secret]`: 认证令牌 (可选)
- `uris`: URI 数组，指向同一资源的多个 URI
- `options`: 选项结构体 (可选)
- `position`: 插入队列的位置，从 0 开始 (可选)

**返回**: 新注册下载的 GID

**示例**:

```python
# JSON-RPC
params = [['http://example.org/file']]
# 带选项
params = [
    ['http://example.org/file'],
    {'dir': '/tmp', 'max-download-limit': '1M'}
]
# 插入到队列前端
params = [['http://example.org/file'], {}, 0]
```

**注意**:

- 添加 BitTorrent Magnet URI 时，`uris` 必须只有一个元素
- 混合指向不同资源的 URI 可能导致下载失败或损坏

---

### 2. aria2.addTorrent

**功能**: 通过上传 .torrent 文件添加 BitTorrent 下载

**参数**:

- `[secret]`: 认证令牌 (可选)
- `torrent`: Base64 编码的 .torrent 文件内容
- `uris`: Web-seeding URI 数组 (可选)
- `options`: 选项结构体 (可选)
- `position`: 插入队列的位置 (可选)

**返回**: 新注册下载的 GID

**示例**:

```python
import base64

torrent_content = base64.b64encode(open('file.torrent', 'rb').read())
params = [torrent_content]
# 带 Web-seeding
params = [torrent_content, ['http://example.org/file']]
```

**注意**:

- 如果 `--rpc-save-upload-metadata=true`，上传的数据会保存为 SHA-1 哈希值命名的文件
- 添加 Magnet URI 请使用 `aria2.addUri`

---

### 3. aria2.addMetalink

**功能**: 通过上传 .metalink 文件添加 Metalink 下载

**参数**:

- `[secret]`: 认证令牌 (可选)
- `metalink`: Base64 编码的 .metalink 文件内容
- `options`: 选项结构体 (可选)
- `position`: 插入队列的位置 (可选)

**返回**: 新注册下载的 GID 数组

**示例**:

```python
import base64

metalink_content = base64.b64encode(open('file.meta4', 'rb').read())
params = [metalink_content]
```

---

### 4. aria2.remove

**功能**: 移除指定的下载任务

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: 被移除下载的 GID

**行为**:

- 如果下载正在进行，会先停止
- 下载状态变为 `removed`
- 会执行需要时间的操作（如联系 BitTorrent tracker）

**示例**:

```python
params = ['2089b05ecca3d829']
```

---

### 5. aria2.forceRemove

**功能**: 强制移除下载任务，不执行耗时操作

**参数**: 同 `aria2.remove`

**返回**: 被移除下载的 GID

**区别**: 不会执行如联系 tracker 等耗时操作，立即移除

---

### 6. aria2.pause

**功能**: 暂停下载任务

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: 被暂停下载的 GID

**行为**:

- 下载状态变为 `paused`
- 如果下载正在进行，会被放到等待队列的前端
- 暂停状态下不会开始下载，需要使用 `aria2.unpause` 恢复

---

### 7. aria2.pauseAll

**功能**: 暂停所有活动/等待中的下载

**参数**:

- `[secret]`: 认证令牌 (可选)

**返回**: `OK`

---

### 8. aria2.forcePause

**功能**: 强制暂停下载，不执行耗时操作

**参数**: 同 `aria2.pause`

**返回**: 被暂停下载的 GID

---

### 9. aria2.forcePauseAll

**功能**: 强制暂停所有活动/等待中的下载

**参数**:

- `[secret]`: 认证令牌 (可选)

**返回**: `OK`

---

### 10. aria2.unpause

**功能**: 恢复暂停的下载

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: 被恢复下载的 GID

**行为**: 将下载状态从 `paused` 改为 `waiting`，使其可以重新开始

---

### 11. aria2.unpauseAll

**功能**: 恢复所有暂停的下载

**参数**:

- `[secret]`: 认证令牌 (可选)

**返回**: `OK`

---

### 12. aria2.tellStatus

**功能**: 获取下载任务的详细信息

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID
- `keys`: 要返回的键名数组 (可选)

**返回**: 包含下载信息的结构体

**返回字段**:

- `gid`: 下载的 GID
- `status`: 下载状态 (`active`/`waiting`/`paused`/`error`/`complete`/`removed`)
- `totalLength`: 总大小（字节）
- `completedLength`: 已完成大小（字节）
- `uploadLength`: 已上传大小（字节）
- `bitfield`: 十六进制表示的下载进度
- `downloadSpeed`: 下载速度（字节/秒）
- `uploadSpeed`: 上传速度（字节/秒）
- `infoHash`: InfoHash (仅 BitTorrent)
- `numSeeders`: 已连接的做种者数量 (仅 BitTorrent)
- `seeder`: 本地端点是否为做种者 (仅 BitTorrent)
- `pieceLength`: 分片大小（字节）
- `numPieces`: 分片数量
- `connections`: 已连接的对等端/服务器数量
- `errorCode`: 错误代码（如果有）
- `errorMessage`: 错误消息
- `followedBy`: 由此下载生成的 GID 列表
- `following`: 父下载的 GID
- `belongsTo`: 父下载的 GID
- `dir`: 保存文件的目录
- `files`: 文件列表
- `bittorrent`: BitTorrent 信息结构体
- `verifiedLength`: 已验证的字节数（哈希检查时）
- `verifyIntegrityPending`: 是否在哈希检查队列中

**示例**:

```python
# 获取所有信息
params = ['2089b05ecca3d829']

# 只获取特定字段
params = ['2089b05ecca3d829', ['gid', 'status', 'downloadSpeed']]
```

---

### 13. aria2.getUris

**功能**: 获取下载任务使用的 URI 列表

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: URI 结构体数组

**返回字段**:

- `uri`: URI 地址
- `status`: `used` (正在使用) 或 `waiting` (在队列中等待)

---

### 14. aria2.getFiles

**功能**: 获取下载任务的文件列表

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: 文件结构体数组

**返回字段**:

- `index`: 文件索引，从 1 开始
- `path`: 文件路径
- `length`: 文件大小（字节）
- `completedLength`: 已完成大小（字节）
- `selected`: 是否被 `--select-file` 选项选中
- `uris`: 此文件的 URI 列表

---

### 15. aria2.getPeers

**功能**: 获取 BitTorrent 下载的对等端列表

**参数**:

- `[secret]`: 认证令牌 (可选)
- `gid`: 下载的 GID

**返回**: 对等端结构体数组

**返回字段**:

- `peerId`: 百分号编码的对等端 ID
- `ip`: 对等端 IP 地址
- `port`: 对等端端口号
- `bitfield`: 对等端下载进度的十六进制表示
- `amChoking`: aria2 是否正在阻塞该对等端
- `peerChoking`: 对等端是否正在阻塞 aria2
- `downloadSpeed`: 从该对等端获得的下载速度（字节/秒）
- `uploadSpeed`: 向该对等端上传的速度（字节/秒）
- `seeder`: 该对等端是否为做种者

**注意**: 仅适用于 BitTorrent 下载

---

# aria2 RPC 接口文档

## 概述

aria2 提供 JSON-RPC 和 XML-RPC 两种接口，用于远程控制下载任务。本文档基于 aria2 官方手册，详细介绍 RPC 接口的使用方法。

### 基本信息

- **JSON-RPC 路径**: `/jsonrpc`
- **XML-RPC 路径**: `/rpc`
- **WebSocket URI**: `ws://HOST:PORT/jsonrpc` (或 `wss://` 用于 SSL/TLS)
- **默认端口**: 6800
- **字符编码**: UTF-8

## 核心概念

### GID (下载ID)

- 每个下载都有唯一的 GID (64位二进制值)
- RPC 中表示为 16 字符的十六进制字符串，例如：`2089b05ecca3d829`
- 可以使用 GID 前缀查询，只要前缀唯一即可
- 用户可通过 `--gid` 选项手动指定 GID

### RPC 授权

使用 `--rpc-secret` 选项设置密钥令牌。每个 RPC 方法调用需要在第一个参数前加 `token:` 前缀。

示例：

```
aria2.addUri("token:$$secret$$", ["http://example.org/file"])
```

## 下载管理方法

### aria2.addUri - 添加 HTTP/FTP/SFTP 下载

**参数**:

- `uris` (数组): URI 列表，指向同一资源
- `options` (对象，可选): 下载选项
- `position` (整数，可选): 队列位置，默认追加到末尾

**返回**: 新下载的 GID

**JSON-RPC 示例**:

```json
{
  "jsonrpc": "2.0",
  "id": "qwer",
  "method": "aria2.addUri",
  "params": [["http://example.org/file"]]
}
```

**Python 示例**:

```python
import urllib2, json
jsonreq = json.dumps({
  'jsonrpc': '2.0',
  'id': 'qwer',
  'method': 'aria2.addUri',
  'params': [['http://example.org/file']]
})
c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
print(json.loads(c.read()))
```

### aria2.addTorrent - 添加 BitTorrent 下载

**参数**:

- `torrent` (字符串): Base64 编码的 .torrent 文件内容
- `uris` (数组，可选): Web-seeding 的 URI 列表
- `options` (对象，可选): 下载选项
- `position` (整数，可选): 队列位置

**返回**: 新下载的 GID

### aria2.addMetalink - 添加 Metalink 下载

**参数**:

- `metalink` (字符串): Base64 编码的 .metalink 或 .meta4 文件内容
- `options` (对象，可选): 下载选项
- `position` (整数，可选): 队列位置

**返回**: 新下载的 GID 数组

### aria2.remove - 移除下载

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 被移除下载的 GID

**说明**: 如果下载正在进行，先停止再移除。下载状态变为 `removed`。

### aria2.forceRemove - 强制移除下载

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 被移除下载的 GID

**说明**: 不执行任何耗时操作（如联系 BitTorrent tracker），直接移除。

## 下载控制方法

### aria2.pause - 暂停下载

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 被暂停下载的 GID

**说明**: 下载状态变为 `paused`。如果下载正在进行，移到等待队列前面。

### aria2.pauseAll - 暂停所有下载

**参数**: 无

**返回**: `OK`

### aria2.forcePause - 强制暂停下载

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 被暂停下载的 GID

**说明**: 不执行耗时操作，直接暂停。

### aria2.forcePauseAll - 强制暂停所有下载

**参数**: 无

**返回**: `OK`

### aria2.unpause - 恢复下载

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 被恢复下载的 GID

**说明**: 将下载状态从 `paused` 改为 `waiting`，使其可以重新启动。

### aria2.unpauseAll - 恢复所有下载

**参数**: 无

**返回**: `OK`

## 下载状态查询方法

### aria2.tellStatus - 获取下载进度

**参数**:

- `gid` (字符串): 下载的 GID
- `keys` (数组，可选): 要返回的字段列表，省略则返回所有字段

**返回**: 包含下载信息的对象

**返回字段说明**:

| 字段 | 说明 |
|------|------|
| `gid` | 下载的 GID |
| `status` | 状态：`active`(下载中)、`waiting`(等待中)、`paused`(已暂停)、`error`(错误)、`complete`(已完成)、`removed`(已移除) |
| `totalLength` | 总大小（字节） |
| `completedLength` | 已完成大小（字节） |
| `uploadLength` | 已上传大小（字节） |
| `bitfield` | 十六进制表示的下载进度位图 |
| `downloadSpeed` | 下载速度（字节/秒） |
| `uploadSpeed` | 上传速度（字节/秒） |
| `infoHash` | 信息哈希（仅 BitTorrent） |
| `numSeeders` | 连接的 seeder 数量（仅 BitTorrent） |
| `seeder` | 是否为 seeder（仅 BitTorrent） |
| `pieceLength` | 分片大小（字节） |
| `numPieces` | 分片总数 |
| `connections` | 连接数 |
| `errorCode` | 错误代码（仅停止/完成的下载） |
| `errorMessage` | 错误信息 |
| `followedBy` | 由此下载生成的下载 GID 列表 |
| `following` | 父下载的 GID |
| `belongsTo` | 所属父下载的 GID |
| `dir` | 保存目录 |
| `files` | 文件列表 |
| `bittorrent` | BitTorrent 信息（仅 BitTorrent） |

**Python 示例**:

```python
import urllib2, json
jsonreq = json.dumps({
  'jsonrpc': '2.0',
  'id': 'qwer',
  'method': 'aria2.tellStatus',
  'params': ['2089b05ecca3d829']
})
c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
print(json.loads(c.read()))
```

### aria2.tellActive - 获取活跃下载列表

**参数**:

- `keys` (数组，可选): 要返回的字段列表

**返回**: 活跃下载的对象数组

### aria2.tellWaiting - 获取等待下载列表

**参数**:

- `offset` (整数): 偏移量，可为负数（-1 表示最后一个）
- `num` (整数): 返回的最大下载数
- `keys` (数组，可选): 要返回的字段列表

**返回**: 等待下载的对象数组

### aria2.tellStopped - 获取已停止下载列表

**参数**:

- `offset` (整数): 偏移量
- `num` (整数): 返回的最大下载数
- `keys` (数组，可选): 要返回的字段列表

**返回**: 已停止下载的对象数组

### aria2.getUris - 获取下载的 URI 列表

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: URI 对象数组

**返回字段**:

- `uri`: URI 地址
- `status`: `used`(使用中) 或 `waiting`(等待中)

### aria2.getFiles - 获取下载的文件列表

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 文件对象数组

**返回字段**:

- `index`: 文件索引（从 1 开始）
- `path`: 文件路径
- `length`: 文件大小（字节）
- `completedLength`: 已完成大小（字节）
- `selected`: 是否被选中（`true`/`false`）
- `uris`: 该文件的 URI 列表

### aria2.getPeers - 获取 BitTorrent Peer 列表

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: Peer 对象数组（仅 BitTorrent）

**返回字段**:

- `peerId`: Peer ID（百分比编码）
- `ip`: Peer IP 地址
- `port`: Peer 端口
- `bitfield`: 十六进制表示的 Peer 进度位图
- `amChoking`: 是否在 choke 此 Peer
- `peerChoking`: Peer 是否在 choke 我们
- `downloadSpeed`: 从此 Peer 的下载速度（字节/秒）
- `uploadSpeed`: 上传到此 Peer 的速度（字节/秒）
- `seeder`: 是否为 seeder

### aria2.getServers - 获取 HTTP/FTP/SFTP 服务器列表

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 服务器对象数组

**返回字段**:

- `index`: 文件索引
- `servers`: 服务器列表，包含：
  - `uri`: 原始 URI
  - `currentUri`: 当前使用的 URI（可能因重定向而不同）
  - `downloadSpeed`: 下载速度（字节/秒）

## 下载选项管理方法

### aria2.changeOption - 修改下载选项

**参数**:

- `gid` (字符串): 下载的 GID
- `options` (对象): 要修改的选项

**返回**: `OK`

**说明**: 修改活跃下载的选项会导致下载重启。以下选项修改不会导致重启：

- `bt-max-peers`
- `bt-request-peer-speed-limit`
- `bt-remove-unselected-file`
- `max-download-limit`
- `max-upload-limit`

### aria2.getOption - 获取下载选项

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: 选项对象（键为选项名，值为字符串）

### aria2.changeGlobalOption - 修改全局选项

**参数**:

- `options` (对象): 要修改的全局选项

**返回**: `OK`

**说明**: 可修改的全局选项包括：

- `bt-max-open-files`
- `download-result`
- `keep-unfinished-download-result`
- `log`
- `log-level`
- `max-concurrent-downloads`
- `max-download-limit`
- `max-overall-download-limit`
- `max-overall-upload-limit`
- `max-upload-limit`
- `optimize-concurrent-downloads`
- `save-cookies`
- `save-session`

### aria2.getGlobalOption - 获取全局选项

**参数**: 无

**返回**: 全局选项对象

## 队列管理方法

### aria2.changePosition - 改变下载队列位置

**参数**:

- `gid` (字符串): 下载的 GID
- `pos` (整数): 位置
- `how` (字符串): 位置参考方式
  - `POS_SET`: 相对于队列开始
  - `POS_CUR`: 相对于当前位置
  - `POS_END`: 相对于队列末尾

**返回**: 新的队列位置（整数）

**示例**: 将下载移到队列前面

```json
{
  "jsonrpc": "2.0",
  "id": "qwer",
  "method": "aria2.changePosition",
  "params": ["2089b05ecca3d829", 0, "POS_SET"]
}
```

### aria2.changeUri - 修改下载的 URI

**参数**:

- `gid` (字符串): 下载的 GID
- `fileIndex` (整数): 文件索引（从 1 开始）
- `delUris` (数组): 要删除的 URI 列表
- `addUris` (数组): 要添加的 URI 列表
- `position` (整数，可选): 新 URI 的插入位置（从 0 开始）

**返回**: 数组 `[删除的 URI 数, 添加的 URI 数]`

## 全局统计方法

### aria2.getGlobalStat - 获取全局统计信息

**参数**: 无

**返回**: 统计信息对象

**返回字段**:

- `downloadSpeed`: 总下载速度（字节/秒）
- `uploadSpeed`: 总上传速度（字节/秒）
- `numActive`: 活跃下载数
- `numWaiting`: 等待下载数
- `numStopped`: 已停止下载数（受 `--max-download-result` 限制）
- `numStoppedTotal`: 已停止下载总数（不受限制）

**Python 示例**:

```python
import urllib2, json
jsonreq = json.dumps({
  'jsonrpc': '2.0',
  'id': 'qwer',
  'method': 'aria2.getGlobalStat'
})
c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
print(json.loads(c.read()))
```

## 下载结果管理方法

### aria2.purgeDownloadResult - 清除下载结果

**参数**: 无

**返回**: `OK`

**说明**: 清除已完成/错误/移除的下载以释放内存。

### aria2.removeDownloadResult - 移除单个下载结果

**参数**:

- `gid` (字符串): 下载的 GID

**返回**: `OK`

## 系统信息方法

### aria2.getVersion - 获取 aria2 版本信息

**参数**: 无

**返回**: 版本信息对象

**返回字段**:

- `version`: aria2 版本号
- `enabledFeatures`: 启用的功能列表

**Python 示例**:

```python
import urllib2, json
jsonreq = json.dumps({
  'jsonrpc': '2.0',
  'id': 'qwer',
  'method': 'aria2.getVersion'
})
c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
print(json.loads(c.read()))
```

### aria2.getSessionInfo - 获取会话信息

**参数**: 无

**返回**: 会话信息对象

**返回字段**:

- `sessionId`: 会话 ID（每次 aria2 启动时生成）

### aria2.shutdown - 关闭 aria2

**参数**: 无

**返回**: `OK`

**说明**: 执行必要的清理操作后关闭 aria2。

### aria2.forceShutdown - 强制关闭 aria2

**参数**: 无

**返回**: `OK`

**说明**: 不执行耗时操作，直接关闭。

### aria2.saveSession - 保存会话

**参数**: 无

**返回**: `OK`

**说明**: 将当前会话保存到 `--save-session` 指定的文件。
