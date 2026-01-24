# aria2 RPC 接口文档（官方 RPC INTERFACE 章节整理）

来源：`https://aria2.github.io/manual/en/html/aria2c.html`

> 说明：本文件仅保留官方文档中与 RPC 相关的“RPC INTERFACE”章节内容。
> 代码块/方法名/字段名/选项名/枚举值等保持原样（不翻译、不改动）。
> 其余说明文字提供中文导读（不替换原文英文，避免信息丢失）。

## 概览（中文导读）

- JSON-RPC 接口路径：`/jsonrpc`（HTTP 与 WebSocket 共用）
- XML-RPC 接口路径：`/rpc`
- WebSocket：`ws://HOST:PORT/jsonrpc`（TLS 时 `wss://HOST:PORT/jsonrpc`）
- JSON-RPC 基于 2.0；支持 HTTP POST 与 HTTP GET（含 JSONP）；WebSocket 额外支持服务端通知
- JSON-RPC over HTTP 不支持 notifications（通知仅通过 WebSocket 推送）
- JSON-RPC 不支持浮点数；字符编码必须为 UTF-8

## RPC INTERFACE（原文英文）

aria2 provides JSON-RPC over HTTP and XML-RPC over HTTP interfaces that offer basically the same functionality. aria2 also provides JSON-RPC over WebSocket. JSON-RPC over WebSocket uses the same method signatures and response format as JSON-RPC over HTTP, but additionally provides server-initiated notifications. See JSON-RPC over WebSocket section for more information.

The request path of the JSON-RPC interface (for both over HTTP and over WebSocket) is `/jsonrpc`. The request path of the XML-RPC interface is `/rpc`.

The WebSocket URI for JSON-RPC over WebSocket is `ws://HOST:PORT/jsonrpc`. If you enabled SSL/TLS encryption, use `wss://HOST:PORT/jsonrpc` instead.

The implemented JSON-RPC is based on JSON-RPC 2.0 <http://jsonrpc.org/specification>, and supports HTTP POST and GET (JSONP). The WebSocket transport is an aria2 extension.

The JSON-RPC interface does not support notifications over HTTP, but the RPC server will send notifications over WebSocket. It also does not support floating point numbers. The character encoding must be UTF-8.

When reading the following documentation for JSON-RPC, interpret structs as JSON objects.

## 术语（中文导读）

- `GID`：下载任务标识符（16 位十六进制字符串）。RPC 查询时可使用唯一前缀。

## Terminology（原文英文）

GID

The GID (or gid) is a key to manage each download. Each download will be assigned a unique GID. The GID is stored as 64-bit binary value in aria2. For RPC access, it is represented as a hex string of 16 characters (e.g., `2089b05ecca3d829`). Normally, aria2 generates this GID for each download, but the user can specify GIDs manually using the `--gid` option. When querying downloads by GID, you can specify only the prefix of a GID as long as it is unique among others.

## 鉴权（中文导读）

- 推荐使用 `--rpc-secret` 开启方法级鉴权；调用时把 `"token:..."` 放在参数列表第一个位置。
- `system.multicall` 外层不放 token；但每个内层调用仍需把 token 放在其参数列表第一个参数。
- `system.listMethods` / `system.listNotifications` 可无 token 调用。

## RPC authorization secret token（原文英文）

As of 1.18.4, in addition to HTTP basic authorization, aria2 provides RPC method-level authorization. In a future release, HTTP basic authorization will be removed and RPC method-level authorization will become mandatory.

To use RPC method-level authorization, the user has to specify an RPC secret authorization token using the `--rpc-secret` option. For each RPC method call, the caller has to include the token prefixed with `token:`. Even when the `--rpc-secret` option is not used, if the first parameter in the RPC method is a string and starts with `token:`, it will removed from the parameter list before the request is being processed.

For example, if the RPC secret authorization token is `$$secret$$`, calling `aria2.addUri` RPC method would have to look like this:

The `system.multicall` RPC method is treated specially. Since the XML-RPC specification only allows a single array as a parameter for this method, we don't specify the token in the call. Instead, each nested method call has to provide the token as the first parameter as described above.

The secret token validation in aria2 is designed to take at least a certain amount of time to mitigate brute-force/dictionary attacks against the RPC interface. Therefore it is recommended to prefer Batch or `system.multicall` requests when appropriate.

`system.listMethods` and `system.listNotifications` can be executed without token. Since they just return available methods/notifications, they do not alter anything, they're safe without secret token.

```
aria2.addUri("token:$$secret$$", ["http://example.org/file"])
```

## 方法与通知（中文导读 + 原文英文）

> 提示：下方每个条目先给出简短中文导读（不改动原文），随后保留官方英文原文与示例代码。

### `aria2.addUri([*secret*, ]*uris*[, *options*[, *position*]])`

中文导读：添加一个新的下载任务（HTTP/FTP/SFTP/BT/Magnet），返回新任务的 GID。

原文英文：

This method adds a new download. *uris* is an array of HTTP/FTP/SFTP/BitTorrent URIs (strings) pointing to the same resource. If you mix URIs pointing to different resources, then the download may fail or be corrupted without aria2 complaining. When adding BitTorrent Magnet URIs, *uris* must have only one element and it should be BitTorrent Magnet URI. *options* is a struct and its members are pairs of option name and value. See Options below for more details. If *position* is given, it must be an integer starting from 0. The new download will be inserted at *position* in the waiting queue. If *position* is omitted or *position* is larger than the current size of the queue, the new download is appended to the end of the queue. This method returns the GID of the newly registered download.

**JSON-RPC Example**

The following example adds `http://example.org/file`:

**XML-RPC Example**

The following example adds `http://example.org/file`:

The following example adds a new download with two sources and some options:

The following example adds a download and inserts it to the front of the queue:

```
>>> import urllib2, json
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.addUri',
...                       'params':[['http://example.org/file']]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> c.read()
'{"id":"qwer","jsonrpc":"2.0","result":"2089b05ecca3d829"}'
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.addUri(['http://example.org/file'])
'2089b05ecca3d829'
```

```
>>> s.aria2.addUri(['http://example.org/file', 'http://mirror/file'],
                    dict(dir="/tmp"))
'd2703803b52216d1'
```

```
>>> s.aria2.addUri(['http://example.org/file'], {}, 0)
'ca3d829cee549a4d'
```

### `aria2.addTorrent([*secret*, ]*torrent*[, *uris*[, *options*[, *position*]]])`

中文导读：通过上传 .torrent（base64）添加 BT 下载任务，返回新任务的 GID。

原文英文：

This method adds a BitTorrent download by uploading a ".torrent" file. If you want to add a BitTorrent Magnet URI, use the `aria2.addUri()` method instead. *torrent* must be a base64-encoded string containing the contents of the ".torrent" file. *uris* is an array of URIs (string). *uris* is used for Web-seeding. For single file torrents, the URI can be a complete URI pointing to the resource; if URI ends with /, name in torrent file is added. For multi-file torrents, name and path in torrent are added to form a URI for each file. *options* is a struct and its members are pairs of option name and value. See Options below for more details. If *position* is given, it must be an integer starting from 0. The new download will be inserted at *position* in the waiting queue. If *position* is omitted or *position* is larger than the current size of the queue, the new download is appended to the end of the queue. This method returns the GID of the newly registered download. If `--rpc-save-upload-metadata` is `true`, the uploaded data is saved as a file named as the hex string of SHA-1 hash of data plus ".torrent" in the directory specified by `--dir` option. E.g. a file name might be `0a3893293e27ac0490424c06de4d09242215f0a6.torrent`. If a file with the same name already exists, it is overwritten! If the file cannot be saved successfully or `--rpc-save-upload-metadata` is `false`, the downloads added by this method are not saved by `--save-session`.

The following examples add local file `file.torrent`.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json, base64
>>> torrent = base64.b64encode(open('file.torrent').read())
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'asdf',
...                       'method':'aria2.addTorrent', 'params':[torrent]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> c.read()
'{"id":"asdf","jsonrpc":"2.0","result":"2089b05ecca3d829"}'
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.addTorrent(xmlrpclib.Binary(open('file.torrent', mode='rb').read()))
'2089b05ecca3d829'
```

### `aria2.addMetalink([*secret*, ]*metalink*[, *options*[, *position*]])`

中文导读：通过上传 .metalink（base64）添加 Metalink 下载任务，返回新任务 GID 列表。

原文英文：

This method adds a Metalink download by uploading a ".metalink" file. *metalink* is a base64-encoded string which contains the contents of the ".metalink" file. *options* is a struct and its members are pairs of option name and value. See Options below for more details. If *position* is given, it must be an integer starting from 0. The new download will be inserted at *position* in the waiting queue. If *position* is omitted or *position* is larger than the current size of the queue, the new download is appended to the end of the queue. This method returns an array of GIDs of newly registered downloads. If `--rpc-save-upload-metadata` is `true`, the uploaded data is saved as a file named hex string of SHA-1 hash of data plus ".metalink" in the directory specified by `--dir` option. E.g. a file name might be `0a3893293e27ac0490424c06de4d09242215f0a6.metalink`. If a file with the same name already exists, it is overwritten! If the file cannot be saved successfully or `--rpc-save-upload-metadata` is `false`, the downloads added by this method are not saved by `--save-session`.

The following examples add local file file.meta4.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json, base64
>>> metalink = base64.b64encode(open('file.meta4').read())
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.addMetalink',
...                       'params':[metalink]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> c.read()
'{"id":"qwer","jsonrpc":"2.0","result":["2089b05ecca3d829"]}'
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.addMetalink(xmlrpclib.Binary(open('file.meta4', mode='rb').read()))
['2089b05ecca3d829']
```

### `aria2.remove([*secret*, ]*gid*)`

中文导读：移除指定 GID 的下载任务（进行中的会先停止），返回被移除任务的 GID。

原文英文：

This method removes the download denoted by *gid* (string). If the specified download is in progress, it is first stopped. The status of the removed download becomes `removed`. This method returns GID of removed download.

The following examples remove a download with GID#2089b05ecca3d829.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.remove',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> c.read()
'{"id":"qwer","jsonrpc":"2.0","result":"2089b05ecca3d829"}'
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.remove('2089b05ecca3d829')
'2089b05ecca3d829'
```

### `aria2.forceRemove([*secret*, ]*gid*)`

中文导读：强制移除指定 GID 的下载任务（不做耗时清理），返回被移除任务的 GID。

原文英文：

This method removes the download denoted by *gid*. This method behaves just like `aria2.remove()` except that this method removes the download without performing any actions which take time, such as contacting BitTorrent trackers to unregister the download first.

### `aria2.pause([*secret*, ]*gid*)`

中文导读：暂停指定 GID 的下载任务，返回被暂停任务的 GID。

原文英文：

This method pauses the download denoted by *gid* (string). The status of paused download becomes `paused`. If the download was active, the download is placed in the front of waiting queue. While the status is `paused`, the download is not started. To change status to `waiting`, use the `aria2.unpause()` method. This method returns GID of paused download.

### `aria2.pauseAll([*secret*])`

中文导读：暂停所有 active/waiting 的下载任务，返回 OK。

原文英文：

This method is equal to calling `aria2.pause()` for every active/waiting download. This methods returns `OK`.

### `aria2.forcePause([*secret*, ]*gid*)`

中文导读：强制暂停指定 GID 的下载任务（不做耗时清理），返回被暂停任务的 GID。

原文英文：

This method pauses the download denoted by *gid*. This method behaves just like `aria2.pause()` except that this method pauses downloads without performing any actions which take time, such as contacting BitTorrent trackers to unregister the download first.

### `aria2.forcePauseAll([*secret*])`

中文导读：强制暂停所有 active/waiting 的下载任务，返回 OK。

原文英文：

This method is equal to calling `aria2.forcePause()` for every active/waiting download. This methods returns `OK`.

### `aria2.unpause([*secret*, ]*gid*)`

中文导读：将指定 GID 的任务从 paused 变为 waiting，返回该任务的 GID。

原文英文：

This method changes the status of the download denoted by *gid* (string) from `paused` to `waiting`, making the download eligible to be restarted. This method returns the GID of the unpaused download.

### `aria2.unpauseAll([*secret*])`

中文导读：将所有 paused 的任务变为 waiting，返回 OK。

原文英文：

This method is equal to calling `aria2.unpause()` for every paused download. This methods returns `OK`.

### `aria2.tellStatus([*secret*, ]*gid*[, *keys*])`

中文导读：查询指定 GID 的状态/进度信息（可用 keys 过滤返回字段）。

原文英文：

This method returns the progress of the download denoted by *gid* (string). *keys* is an array of strings. If specified, the response contains only keys in the *keys* array. If *keys* is empty or omitted, the response contains all keys. This is useful when you just want specific keys and avoid unnecessary transfers. For example, `aria2.tellStatus("2089b05ecca3d829", ["gid", "status"])` returns the *gid* and *status* keys only. The response is a struct and contains following keys. Values are strings.

The number of verified number of bytes while the files are being hash checked. This key exists only when this download is being hash checked.

`true` if this download is waiting for the hash check in a queue. This key exists only when this download is in the queue.

**JSON-RPC Example**

The following example gets information about a download with GID#2089b05ecca3d829:

The following example gets only specific keys:

**XML-RPC Example**

The following example gets information about a download with GID#2089b05ecca3d829:

The following example gets only specific keys:

字段/结构（原文英文，未改动）：

- **`gid`**: GID of the download.
- **`status`**: `active` for currently downloading/seeding downloads. `waiting` for downloads in the queue; download is not started. `paused` for paused downloads. `error` for downloads that were stopped because of error. `complete` for stopped and completed downloads. `removed` for the downloads removed by user.
- **`totalLength`**: Total length of the download in bytes.
- **`completedLength`**: Completed length of the download in bytes.
- **`uploadLength`**: Uploaded length of the download in bytes.
- **`bitfield`**: Hexadecimal representation of the download progress. The highest bit corresponds to the piece at index 0. Any set bits indicate loaded pieces, while unset bits indicate not yet loaded and/or missing pieces. Any overflow bits at the end are set to zero. When the download was not started yet, this key will not be included in the response.
- **`downloadSpeed`**: Download speed of this download measured in bytes/sec.
- **`uploadSpeed`**: Upload speed of this download measured in bytes/sec.
- **`infoHash`**: InfoHash. BitTorrent only.
- **`numSeeders`**: The number of seeders aria2 has connected to. BitTorrent only.
- **`seeder`**: `true` if the local endpoint is a seeder. Otherwise `false`. BitTorrent only.
- **`pieceLength`**: Piece length in bytes.
- **`numPieces`**: The number of pieces.
- **`connections`**: The number of peers/servers aria2 has connected to.
- **`errorCode`**: The code of the last error for this item, if any. The value is a string. The error codes are defined in the EXIT STATUS section. This value is only available for stopped/completed downloads.
- **`errorMessage`**: The (hopefully) human readable error message associated to `errorCode`.
- **`followedBy`**: List of GIDs which are generated as the result of this download. For example, when aria2 downloads a Metalink file, it generates downloads described in the Metalink (see the `--follow-metalink` option). This value is useful to track auto-generated downloads. If there are no such downloads, this key will not be included in the response.
- **`following`**: The reverse link for `followedBy`. A download included in `followedBy` has this object's GID in its `following` value.
- **`belongsTo`**: GID of a parent download. Some downloads are a part of another download. For example, if a file in a Metalink has BitTorrent resources, the downloads of ".torrent" files are parts of that parent. If this download has no parent, this key will not be included in the response.
- **`dir`**: Directory to save files.
- **`files`**: Returns the list of files. The elements of this list are the same structs used in `aria2.getFiles()` method.
- **`bittorrent`**: Struct which contains information retrieved from the .torrent (file). BitTorrent only. It contains following keys.
  - **`announceList`**: List of lists of announce URIs. If the torrent contains `announce` and no `announce-list`, `announce` is converted to the `announce-list` format.
  - **`comment`**: The comment of the torrent. `comment.utf-8` is used if available.
  - **`creationDate`**: The creation time of the torrent. The value is an integer since the epoch, measured in seconds.
  - **`mode`**: File mode of the torrent. The value is either `single` or `multi`.
  - **`info`**: Struct which contains data from Info dictionary. It contains following keys.
    - **`name`**: name in info dictionary. `name.utf-8` is used if available.
- **`verifiedLength`**: The number of verified number of bytes while the files are being hash checked. This key exists only when this download is being hash checked.
- **`verifyIntegrityPending`**: `true` if this download is waiting for the hash check in a queue. This key exists only when this download is in the queue.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.tellStatus',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'bitfield': u'0000000000',
             u'completedLength': u'901120',
             u'connections': u'1',
             u'dir': u'/downloads',
             u'downloadSpeed': u'15158',
             u'files': [{u'index': u'1',
                         u'length': u'34896138',
                         u'completedLength': u'34896138',
                         u'path': u'/downloads/file',
                         u'selected': u'true',
                         u'uris': [{u'status': u'used',
                                    u'uri': u'http://example.org/file'}]}],
             u'gid': u'2089b05ecca3d829',
             u'numPieces': u'34',
             u'pieceLength': u'1048576',
             u'status': u'active',
             u'totalLength': u'34896138',
             u'uploadLength': u'0',
             u'uploadSpeed': u'0'}}
```

```
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.tellStatus',
...                       'params':['2089b05ecca3d829',
...                                 ['gid',
...                                  'totalLength',
...                                  'completedLength']]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'completedLength': u'5701632',
             u'gid': u'2089b05ecca3d829',
             u'totalLength': u'34896138'}}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.tellStatus('2089b05ecca3d829')
>>> pprint(r)
{'bitfield': 'ffff80',
 'completedLength': '34896138',
 'connections': '0',
 'dir': '/downloads',
 'downloadSpeed': '0',
 'errorCode': '0',
 'files': [{'index': '1',
            'length': '34896138',
            'completedLength': '34896138',
            'path': '/downloads/file',
            'selected': 'true',
            'uris': [{'status': 'used',
                      'uri': 'http://example.org/file'}]}],
 'gid': '2089b05ecca3d829',
 'numPieces': '17',
 'pieceLength': '2097152',
 'status': 'complete',
 'totalLength': '34896138',
 'uploadLength': '0',
 'uploadSpeed': '0'}
```

```
>>> r = s.aria2.tellStatus('2089b05ecca3d829', ['gid', 'totalLength', 'completedLength'])
>>> pprint(r)
{'completedLength': '34896138', 'gid': '2089b05ecca3d829', 'totalLength': '34896138'}
```

### `aria2.getUris([*secret*, ]*gid*)`

中文导读：获取指定下载任务当前使用/等待的 URI 列表。

原文英文：

This method returns the URIs used in the download denoted by *gid* (string). The response is an array of structs and it contains following keys. Values are string.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`uri`**: URI
- **`status`**: 'used' if the URI is in use. 'waiting' if the URI is still waiting in the queue.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getUris',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [{u'status': u'used',
              u'uri': u'http://example.org/file'}]}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getUris('2089b05ecca3d829')
>>> pprint(r)
[{'status': 'used', 'uri': 'http://example.org/file'}]
```

### `aria2.getFiles([*secret*, ]*gid*)`

中文导读：获取指定下载任务的文件列表（含每个文件的完成情况与 URI 列表）。

原文英文：

This method returns the file list of the download denoted by *gid* (string). The response is an array of structs which contain following keys. Values are strings.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`index`**: Index of the file, starting at 1, in the same order as files appear in the multi-file torrent.
- **`path`**: File path.
- **`length`**: File size in bytes.
- **`completedLength`**: Completed length of this file in bytes. Please note that it is possible that sum of `completedLength` is less than the `completedLength` returned by the `aria2.tellStatus()` method. This is because `completedLength` in `aria2.getFiles()` only includes completed pieces. On the other hand, `completedLength` in `aria2.tellStatus()` also includes partially completed pieces.
- **`selected`**: `true` if this file is selected by `--select-file` option. If `--select-file` is not specified or this is single-file torrent or not a torrent download at all, this value is always `true`. Otherwise `false`.
- **`uris`**: Returns a list of URIs for this file. The element type is the same struct used in the `aria2.getUris()` method.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getFiles',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [{u'index': u'1',
              u'length': u'34896138',
              u'completedLength': u'34896138',
              u'path': u'/downloads/file',
              u'selected': u'true',
              u'uris': [{u'status': u'used',
                         u'uri': u'http://example.org/file'}]}]}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getFiles('2089b05ecca3d829')
>>> pprint(r)
[{'index': '1',
  'length': '34896138',
  'completedLength': '34896138',
  'path': '/downloads/file',
  'selected': 'true',
  'uris': [{'status': 'used',
            'uri': 'http://example.org/file'}]}]
```

### `aria2.getPeers([*secret*, ]*gid*)`

中文导读：获取 BT 任务的 peer 列表（仅 BitTorrent）。

原文英文：

This method returns a list peers of the download denoted by *gid* (string). This method is for BitTorrent only. The response is an array of structs and contains the following keys. Values are strings.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`peerId`**: Percent-encoded peer ID.
- **`ip`**: IP address of the peer.
- **`port`**: Port number of the peer.
- **`bitfield`**: Hexadecimal representation of the download progress of the peer. The highest bit corresponds to the piece at index 0. Set bits indicate the piece is available and unset bits indicate the piece is missing. Any spare bits at the end are set to zero.
- **`amChoking`**: `true` if aria2 is choking the peer. Otherwise `false`.
- **`peerChoking`**: `true` if the peer is choking aria2. Otherwise `false`.
- **`downloadSpeed`**: Download speed (byte/sec) that this client obtains from the peer.
- **`uploadSpeed`**: Upload speed(byte/sec) that this client uploads to the peer.
- **`seeder`**: `true` if this peer is a seeder. Otherwise `false`.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getPeers',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [{u'amChoking': u'true',
              u'bitfield': u'ffffffffffffffffffffffffffffffffffffffff',
              u'downloadSpeed': u'10602',
              u'ip': u'10.0.0.9',
              u'peerChoking': u'false',
              u'peerId': u'aria2%2F1%2E10%2E5%2D%87%2A%EDz%2F%F7%E6',
              u'port': u'6881',
              u'seeder': u'true',
              u'uploadSpeed': u'0'},
             {u'amChoking': u'false',
              u'bitfield': u'ffffeff0fffffffbfffffff9fffffcfff7f4ffff',
              u'downloadSpeed': u'8654',
              u'ip': u'10.0.0.30',
              u'peerChoking': u'false',
              u'peerId': u'bittorrent client758',
              u'port': u'37842',
              u'seeder': u'false',
              u'uploadSpeed': u'6890'}]}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getPeers('2089b05ecca3d829')
>>> pprint(r)
[{'amChoking': 'true',
  'bitfield': 'ffffffffffffffffffffffffffffffffffffffff',
  'downloadSpeed': '10602',
  'ip': '10.0.0.9',
  'peerChoking': 'false',
  'peerId': 'aria2%2F1%2E10%2E5%2D%87%2A%EDz%2F%F7%E6',
  'port': '6881',
  'seeder': 'true',
  'uploadSpeed': '0'},
 {'amChoking': 'false',
  'bitfield': 'ffffeff0fffffffbfffffff9fffffcfff7f4ffff',
  'downloadSpeed': '8654',
  'ip': '10.0.0.30',
  'peerChoking': 'false',
  'peerId': 'bittorrent client758',
  'port': '37842',
  'seeder': 'false,
  'uploadSpeed': '6890'}]
```

### `aria2.getServers([*secret*, ]*gid*)`

中文导读：获取当前连接的 HTTP(S)/FTP/SFTP 服务器信息。

原文英文：

This method returns currently connected HTTP(S)/FTP/SFTP servers of the download denoted by *gid* (string). The response is an array of structs and contains the following keys. Values are strings.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`index`**: Index of the file, starting at 1, in the same order as files appear in the multi-file metalink.
- **`servers`**: A list of structs which contain the following keys.
  - **`uri`**: Original URI.
  - **`currentUri`**: This is the URI currently used for downloading. If redirection is involved, currentUri and uri may differ.
  - **`downloadSpeed`**: Download speed (byte/sec)

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getServers',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [{u'index': u'1',
              u'servers': [{u'currentUri': u'http://example.org/file',
                            u'downloadSpeed': u'10467',
                            u'uri': u'http://example.org/file'}]}]}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getServers('2089b05ecca3d829')
>>> pprint(r)
[{'index': '1',
  'servers': [{'currentUri': 'http://example.org/dl/file',
               'downloadSpeed': '20285',
               'uri': 'http://example.org/file'}]}]
```

### `aria2.tellActive([*secret*][, *keys*])`

中文导读：获取所有 active 任务列表（返回结构同 tellStatus，可用 keys 过滤）。

原文英文：

This method returns a list of active downloads. The response is an array of the same structs as returned by the `aria2.tellStatus()` method. For the *keys* parameter, please refer to the `aria2.tellStatus()` method.

### `aria2.tellWaiting([*secret*, ]*offset*, *num*[, *keys*])`

中文导读：获取 waiting（含 paused）任务列表（支持 offset/num/keys）。

原文英文：

This method returns a list of waiting downloads, including paused ones. *offset* is an integer and specifies the offset from the download waiting at the front. *num* is an integer and specifies the max. number of downloads to be returned. For the *keys* parameter, please refer to the `aria2.tellStatus()` method.

If *offset* is a positive integer, this method returns downloads in the range of [*offset*, *offset* + *num*).

*offset* can be a negative integer. *offset* == -1 points last download in the waiting queue and *offset* == -2 points the download before the last download, and so on. Downloads in the response are in reversed order then.

For example, imagine three downloads "A","B" and "C" are waiting in this order. `aria2.tellWaiting(0, 1)` returns `["A"]`. `aria2.tellWaiting(1, 2)` returns `["B", "C"]`. `aria2.tellWaiting(-1, 2)` returns `["C", "B"]`.

The response is an array of the same structs as returned by `aria2.tellStatus()` method.

### `aria2.tellStopped([*secret*, ]*offset*, *num*[, *keys*])`

中文导读：获取 stopped（complete/error/removed）任务列表（支持 offset/num/keys）。

原文英文：

This method returns a list of stopped downloads. *offset* is an integer and specifies the offset from the least recently stopped download. *num* is an integer and specifies the max. number of downloads to be returned. For the *keys* parameter, please refer to the `aria2.tellStatus()` method.

*offset* and *num* have the same semantics as described in the `aria2.tellWaiting()` method.

The response is an array of the same structs as returned by the `aria2.tellStatus()` method.

### `aria2.changePosition([*secret*, ]*gid*, *pos*, *how*)`

中文导读：修改任务在队列中的位置，返回调整后的队列位置（整数）。

原文英文：

This method changes the position of the download denoted by *gid* in the queue. *pos* is an integer. *how* is a string. If *how* is `POS_SET`, it moves the download to a position relative to the beginning of the queue. If *how* is `POS_CUR`, it moves the download to a position relative to the current position. If *how* is `POS_END`, it moves the download to a position relative to the end of the queue. If the destination position is less than 0 or beyond the end of the queue, it moves the download to the beginning or the end of the queue respectively. The response is an integer denoting the resulting position.

For example, if GID#2089b05ecca3d829 is currently in position 3, `aria2.changePosition('2089b05ecca3d829', -1, 'POS_CUR')` will change its position to 2. Additionally `aria2.changePosition('2089b05ecca3d829', 0, 'POS_SET')` will change its position to 0 (the beginning of the queue).

The following examples move the download GID#2089b05ecca3d829 to the front of the queue.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.changePosition',
...                       'params':['2089b05ecca3d829', 0, 'POS_SET']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': 0}
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.changePosition('2089b05ecca3d829', 0, 'POS_SET')
0
```

### `aria2.changeUri([*secret*, ]*gid*, *fileIndex*, *delUris*, *addUris*[, *position*])`

中文导读：为指定任务的指定文件删除/新增 URI，返回 [删除数, 新增数]。

原文英文：

This method removes the URIs in *delUris* from and appends the URIs in *addUris* to download denoted by *gid*. *delUris* and *addUris* are lists of strings. A download can contain multiple files and URIs are attached to each file. *fileIndex* is used to select which file to remove/attach given URIs. *fileIndex* is 1-based. *position* is used to specify where URIs are inserted in the existing waiting URI list. *position* is 0-based. When *position* is omitted, URIs are appended to the back of the list. This method first executes the removal and then the addition. *position* is the position after URIs are removed, not the position when this method is called. When removing an URI, if the same URIs exist in download, only one of them is removed for each URI in *delUris*. In other words, if there are three URIs `http://example.org/aria2` and you want remove them all, you have to specify (at least) 3 `http://example.org/aria2` in *delUris*. This method returns a list which contains two integers. The first integer is the number of URIs deleted. The second integer is the number of URIs added.

The following examples add the URI `http://example.org/file` to the file whose index is `1` and belongs to the download GID#2089b05ecca3d829.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.changeUri',
...                       'params':['2089b05ecca3d829', 1, [],
                                    ['http://example.org/file']]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': [0, 1]}
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.changeUri('2089b05ecca3d829', 1, [],
                      ['http://example.org/file'])
[0, 1]
```

### `aria2.getOption([*secret*, ]*gid*)`

中文导读：获取指定任务的当前选项（optionName -> string）。

原文英文：

This method returns options of the download denoted by *gid*. The response is a struct where keys are the names of options. The values are strings. Note that this method does not return options which have no default value and have not been set on the command-line, in configuration files or RPC methods.

The following examples get options of the download GID#2089b05ecca3d829.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getOption',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'allow-overwrite': u'false',
             u'allow-piece-length-change': u'false',
             u'always-resume': u'true',
             u'async-dns': u'true',
 ...
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getOption('2089b05ecca3d829')
>>> pprint(r)
{'allow-overwrite': 'false',
 'allow-piece-length-change': 'false',
 'always-resume': 'true',
 'async-dns': 'true',
 ....
```

### `aria2.changeOption([*secret*, ]*gid*, *options*)`

中文导读：动态修改指定任务的选项，成功返回 OK。

原文英文：

This method changes options of the download denoted by *gid* (string) dynamically. *options* is a struct. The options listed in Input File subsection are available, **except** for following options:

`dry-run`

`metalink-base-uri`

`parameterized-uri`

`pause`

`piece-length`

`rpc-save-upload-metadata`

Except for the following options, changing the other options of active download makes it restart (restart itself is managed by aria2, and no user intervention is required):

`bt-max-peers`

`bt-request-peer-speed-limit`

`bt-remove-unselected-file`

`force-save`

`max-download-limit`

`max-upload-limit`

This method returns `OK` for success.

The following examples set the `max-download-limit` option to `20K` for the download GID#2089b05ecca3d829.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.changeOption',
...                       'params':['2089b05ecca3d829',
...                                 {'max-download-limit':'10K'}]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': u'OK'}
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.changeOption('2089b05ecca3d829', {'max-download-limit':'20K'})
'OK'
```

### `aria2.getGlobalOption([*secret*])`

中文导读：获取全局选项（optionName -> string）。

原文英文：

This method returns the global options. The response is a struct. Its keys are the names of options. Values are strings. Note that this method does not return options which have no default value and have not been set on the command-line, in configuration files or RPC methods. Because global options are used as a template for the options of newly added downloads, the response contains keys returned by the `aria2.getOption()` method.

### `aria2.changeGlobalOption([*secret*, ]*options*)`

中文导读：动态修改全局选项，成功返回 OK。

原文英文：

This method changes global options dynamically. *options* is a struct. The following options are available:

`bt-max-open-files`

`download-result`

`keep-unfinished-download-result`

`log`

`log-level`

`max-concurrent-downloads`

`max-download-result`

`max-overall-download-limit`

`max-overall-upload-limit`

`optimize-concurrent-downloads`

`save-cookies`

`save-session`

`server-stat-of`

In addition, options listed in the Input File subsection are available, **except** for following options: `checksum`, `index-out`, `out`, `pause` and `select-file`.

With the `log` option, you can dynamically start logging or change log file. To stop logging, specify an empty string("") as the parameter value. Note that log file is always opened in append mode. This method returns `OK` for success.

### `aria2.getGlobalStat([*secret*])`

中文导读：获取全局统计信息（总上下行速度、任务数量等）。

原文英文：

This method returns global statistics such as the overall download and upload speeds. The response is a struct and contains the following keys. Values are strings.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`downloadSpeed`**: Overall download speed (byte/sec).
- **`uploadSpeed`**: Overall upload speed(byte/sec).
- **`numActive`**: The number of active downloads.
- **`numWaiting`**: The number of waiting downloads.
- **`numStopped`**: The number of stopped downloads in the current session. This value is capped by the `--max-download-result` option.
- **`numStoppedTotal`**: The number of stopped downloads in the current session and *not* capped by the `--max-download-result` option.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getGlobalStat'})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'downloadSpeed': u'21846',
             u'numActive': u'2',
             u'numStopped': u'0',
             u'numWaiting': u'0',
             u'uploadSpeed': u'0'}}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getGlobalStat()
>>> pprint(r)
{'downloadSpeed': '23136',
 'numActive': '2',
 'numStopped': '0',
 'numWaiting': '0',
 'uploadSpeed': '0'}
```

### `aria2.purgeDownloadResult([*secret*])`

中文导读：清理已完成/错误/移除的任务结果以释放内存，返回 OK。

原文英文：

This method purges completed/error/removed downloads to free memory. This method returns `OK`.

### `aria2.removeDownloadResult([*secret*, ]*gid*)`

中文导读：从内存移除指定 GID 的（已完成/错误/移除）结果，成功返回 OK。

原文英文：

This method removes a completed/error/removed download denoted by *gid* from memory. This method returns `OK` for success.

The following examples remove the download result of the download GID#2089b05ecca3d829.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.removeDownloadResult',
...                       'params':['2089b05ecca3d829']})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': u'OK'}
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.removeDownloadResult('2089b05ecca3d829')
'OK'
```

### `aria2.getVersion([*secret*])`

中文导读：获取 aria2 版本与启用特性列表。

原文英文：

This method returns the version of aria2 and the list of enabled features. The response is a struct and contains following keys.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`version`**: Version number of aria2 as a string.
- **`enabledFeatures`**: List of enabled features. Each feature is given as a string.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getVersion'})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'enabledFeatures': [u'Async DNS',
                                  u'BitTorrent',
                                  u'Firefox3 Cookie',
                                  u'GZip',
                                  u'HTTPS',
                                  u'Message Digest',
                                  u'Metalink',
                                  u'XML-RPC'],
             u'version': u'1.11.0'}}
```

```
>>> import xmlrpclib
>>> from pprint import pprint
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> r = s.aria2.getVersion()
>>> pprint(r)
{'enabledFeatures': ['Async DNS',
                     'BitTorrent',
                     'Firefox3 Cookie',
                     'GZip',
                     'HTTPS',
                     'Message Digest',
                     'Metalink',
                     'XML-RPC'],
 'version': '1.11.0'}
```

### `aria2.getSessionInfo([*secret*])`

中文导读：获取当前 session 信息（sessionId）。

原文英文：

This method returns session information. The response is a struct and contains following key.

**JSON-RPC Example**

**XML-RPC Example**

字段/结构（原文英文，未改动）：

- **`sessionId`**: Session ID, which is generated each time when aria2 is invoked.

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'aria2.getSessionInfo'})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': {u'sessionId': u'cd6a3bc6a1de28eb5bfa181e5f6b916d44af31a9'}}
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.aria2.getSessionInfo()
{'sessionId': 'cd6a3bc6a1de28eb5bfa181e5f6b916d44af31a9'}
```

### `aria2.shutdown([*secret*])`

中文导读：正常关闭 aria2，返回 OK。

原文英文：

This method shuts down aria2. This method returns `OK`.

### `aria2.forceShutdown([*secret*])`

中文导读：强制关闭 aria2（不做耗时清理），返回 OK。

原文英文：

This method shuts down `aria2()`. This method behaves like :func:'aria2.shutdown` without performing any actions which take time, such as contacting BitTorrent trackers to unregister downloads first. This method returns `OK`.

### `aria2.saveSession([*secret*])`

中文导读：将当前 session 保存到 --save-session 指定文件，成功返回 OK。

原文英文：

This method saves the current session to a file specified by the `--save-session` option. This method returns `OK` if it succeeds.

### `system.multicall(*methods*)`

中文导读：在一次请求中封装多个 RPC 调用，返回每个子调用的结果/错误。

原文英文：

This methods encapsulates multiple method calls in a single request. *methods* is an array of structs. The structs contain two keys: `methodName` and `params`. `methodName` is the method name to call and `params` is array containing parameters to the method call. This method returns an array of responses. The elements will be either a one-item array containing the return value of the method call or a struct of fault element if an encapsulated method call fails.

In the following examples, we add 2 downloads. The first one is `http://example.org/file` and the second one is `file.torrent`.

**JSON-RPC Example**

JSON-RPC additionally supports Batch requests as described in the JSON-RPC 2.0 Specification:

**XML-RPC Example**

```
>>> import urllib2, json, base64
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'system.multicall',
...                       'params':[[{'methodName':'aria2.addUri',
...                                   'params':[['http://example.org']]},
...                                  {'methodName':'aria2.addTorrent',
...                                   'params':[base64.b64encode(open('file.torrent').read())]}]]})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': [[u'2089b05ecca3d829'], [u'd2703803b52216d1']]}
```

```
>>> jsonreq = json.dumps([{'jsonrpc':'2.0', 'id':'qwer',
...                        'method':'aria2.addUri',
...                        'params':[['http://example.org']]},
...                       {'jsonrpc':'2.0', 'id':'asdf',
...                        'method':'aria2.addTorrent',
...                        'params':[base64.b64encode(open('file.torrent').read())]}])
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
[{u'id': u'qwer', u'jsonrpc': u'2.0', u'result': u'2089b05ecca3d829'},
 {u'id': u'asdf', u'jsonrpc': u'2.0', u'result': u'd2703803b52216d1'}]
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> mc = xmlrpclib.MultiCall(s)
>>> mc.aria2.addUri(['http://example.org/file'])
>>> mc.aria2.addTorrent(xmlrpclib.Binary(open('file.torrent', mode='rb').read()))
>>> r = mc()
>>> tuple(r)
('2089b05ecca3d829', 'd2703803b52216d1')
```

### `system.listMethods()`

中文导读：返回所有可用 RPC 方法名列表（可无 token 调用）。

原文英文：

This method returns all the available RPC methods in an array of string. Unlike other methods, this method does not require secret token. This is safe because this method just returns the available method names.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'system.listMethods'})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [u'aria2.addUri',
             u'aria2.addTorrent',
...
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.system.listMethods()
['aria2.addUri', 'aria2.addTorrent', ...
```

### `system.listNotifications()`

中文导读：返回所有可用 RPC 通知名列表（可无 token 调用）。

原文英文：

This method returns all the available RPC notifications in an array of string. Unlike other methods, this method does not require secret token. This is safe because this method just returns the available notifications names.

**JSON-RPC Example**

**XML-RPC Example**

```
>>> import urllib2, json
>>> from pprint import pprint
>>> jsonreq = json.dumps({'jsonrpc':'2.0', 'id':'qwer',
...                       'method':'system.listNotifications'})
>>> c = urllib2.urlopen('http://localhost:6800/jsonrpc', jsonreq)
>>> pprint(json.loads(c.read()))
{u'id': u'qwer',
 u'jsonrpc': u'2.0',
 u'result': [u'aria2.onDownloadStart',
             u'aria2.onDownloadPause',
...
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> s.system.listNotifications()
['aria2.onDownloadStart', 'aria2.onDownloadPause', ...
```

### `aria2.onDownloadStart(*event*)`

中文导读：通知：任务开始下载时触发。

原文英文：

This notification will be sent when a download is started. The *event* is of type struct and it contains following keys. The value type is string.

字段/结构（原文英文，未改动）：

- **`gid`**: GID of the download.

### `aria2.onDownloadPause(*event*)`

中文导读：通知：任务暂停时触发。

原文英文：

This notification will be sent when a download is paused. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

### `aria2.onDownloadStop(*event*)`

中文导读：通知：任务被用户停止时触发。

原文英文：

This notification will be sent when a download is stopped by the user. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

### `aria2.onDownloadComplete(*event*)`

中文导读：通知：任务完成时触发（BT: 做种结束后）。

原文英文：

This notification will be sent when a download is complete. For BitTorrent downloads, this notification is sent when the download is complete and seeding is over. The *event* is the same struct of the *event* argument of `aria2.onDownloadStart()` method.

### `aria2.onDownloadError(*event*)`

中文导读：通知：任务因错误停止时触发。

原文英文：

This notification will be sent when a download is stopped due to an error. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

### `aria2.onBtDownloadComplete(*event*)`

中文导读：通知：BT 任务下载完成但仍在做种时触发。

原文英文：

This notification will be sent when a torrent download is complete but seeding is still going on. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

## JSON-RPC / XML-RPC 通用示例与补充说明（原文英文）

中文导读：以下为官方在方法列表后给出的通用请求示例、Batch、错误对象、以及 XML-RPC struct 参数表示的补充说明（保持原文与示例不变）。

#### JSON-RPC Example

#### XML-RPC Example

#### JSON-RPC Example

#### XML-RPC Example

Over JSON-RPC, aria2 returns a JSON object which contains an error code in `code` and the error message in `message`.

Over XML-RPC, aria2 returns `faultCode=1` and the error message in `faultString`.

The same options as for `--input-file` are available. See the Input File subsection for a complete list of options.

In the option struct, the name element is the option name (without the preceding `--`) and the value element is the argument as a string.

The `header` and `index-out` options are allowed multiple times on the command-line. Since the name should be unique in a struct (many XML-RPC library implementations use a hash or dict for struct), a single string is not enough. To overcome this limitation, you may use an array as the value as well as a string.

The following example adds a download with two options: `dir` and `header`. The `header` option requires two values, so it uses a list:

```
{'split':'1', 'http-proxy':'http://proxy/'}
```

```xml
<struct>
  <member>
    <name>split</name>
    <value><string>1</string></value>
  </member>
  <member>
    <name>http-proxy</name>
    <value><string>http://proxy/</string></value>
  </member>
</struct>
```

```
{'header':['Accept-Language: ja', 'Accept-Charset: utf-8']}
```

```xml
<struct>
  <member>
    <name>header</name>
    <value>
      <array>
        <data>
          <value><string>Accept-Language: ja</string></value>
          <value><string>Accept-Charset: utf-8</string></value>
        </data>
      </array>
    </value>
  </member>
</struct>
```

```
>>> import xmlrpclib
>>> s = xmlrpclib.ServerProxy('http://localhost:6800/rpc')
>>> opts = dict(dir='/tmp',
...             header=['Accept-Language: ja',
...                     'Accept-Charset: utf-8'])
>>> s.aria2.addUri(['http://example.org/file'], opts)
'1'
```

## JSON-RPC via HTTP GET / JSONP（中文导读 + 原文英文）

中文导读：GET 方式把 `method`/`id` 作为 UTF-8 JSON string 参数；`params` 是 Base64 编码后的 JSON 数组；支持 JSONP；Batch 请求时不指定 `method`/`id`，把整个请求数组放进 `params`。

原文英文：

The JSON-RPC interface also supports requests via HTTP GET. The encoding scheme in GET parameters is based on JSON-RPC over HTTP Specification [2008-1-15(RC1)]. The encoding of GET parameters are follows:

The `method` and `id` are always treated as JSON string and their encoding must be UTF-8.

For example, The encoded string of `aria2.tellStatus('2089b05ecca3d829')` with `id='foo'` looks like this:

The `params` parameter is Base64-encoded JSON array which usually appears in `params` attribute in JSON-RPC request object. In the above example, the params is `["2089b05ecca3d829"]`, therefore:

The JSON-RPC interface also supports JSONP. You can specify the callback function in the `jsoncallback` parameter:

For Batch requests, the `method` and `id` parameters must not be specified. The whole request must be specified in the `params` parameter. For example, a Batch request:

must be encoded like this:

```
/jsonrpc?method=METHOD_NAME&id=ID&params=BASE64_ENCODED_PARAMS
```

```
/jsonrpc?method=aria2.tellStatus&id=foo&params=WyIyMDg5YjA1ZWNjYTNkODI5Il0%3D
```

```
["2089b05ecca3d829"] --(Base64)--> WyIyMDg5YjA1ZWNjYTNkODI5Il0=
             --(Percent Encode)--> WyIyMDg5YjA1ZWNjYTNkODI5Il0%3D
```

```
/jsonrpc?method=aria2.tellStatus&id=foo&params=WyIyMDg5YjA1ZWNjYTNkODI5Il0%3D&jsoncallback=cb
```

```
[{'jsonrpc':'2.0', 'id':'qwer', 'method':'aria2.getVersion'},
 {'jsonrpc':'2.0', 'id':'asdf', 'method':'aria2.tellActive'}]
```

```
/jsonrpc?params=W3sianNvbnJwYyI6ICIyLjAiLCAiaWQiOiAicXdlciIsICJtZXRob2QiOiAiYXJpYTIuZ2V0VmVyc2lvbiJ9LCB7Impzb25ycGMiOiAiMi4wIiwgImlkIjogImFzZGYiLCAibWV0aG9kIjogImFyaWEyLnRlbGxBY3RpdmUifV0%3D
```

## JSON-RPC over WebSocket（中文导读 + 原文英文）

中文导读：WebSocket 传输与 HTTP 的方法签名/响应格式一致，但额外支持服务端主动推送通知（notifications）。

原文英文：

JSON-RPC over WebSocket uses same method signatures and response format as JSON-RPC over HTTP. The supported WebSocket version is 13 which is detailed in **RFC 6455**.

To send a RPC request to the RPC server, send a serialized JSON string in a Text frame. The response from the RPC server is delivered also in a Text frame.

The RPC server might send notifications to the client. Notifications is unidirectional, therefore the client which receives the notification must not respond to it. The method signature of a notification is much like a normal method request but lacks the id key. The value of the params key is the data which this notification carries. The format of the value varies depending on the notification method. Following notification methods are defined.

This notification will be sent when a download is started. The *event* is of type struct and it contains following keys. The value type is string.

GID of the download.

This notification will be sent when a download is paused. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

This notification will be sent when a download is stopped by the user. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

This notification will be sent when a download is complete. For BitTorrent downloads, this notification is sent when the download is complete and seeding is over. The *event* is the same struct of the *event* argument of `aria2.onDownloadStart()` method.

This notification will be sent when a download is stopped due to an error. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

This notification will be sent when a torrent download is complete but seeding is still going on. The *event* is the same struct as the *event* argument of `aria2.onDownloadStart()` method.

The following Ruby script adds `http://localhost/aria2.tar.bz2` to aria2c (running on localhost) with option `--dir=/downloads` and prints the RPC response:

If you are a Python lover, you can use xmlrpclib (Python3 uses xmlrpc.client instead) to interact with aria2:

```
#!/usr/bin/env ruby

require 'xmlrpc/client'
require 'pp'

client=XMLRPC::Client.new2("http://localhost:6800/rpc")

options={ "dir" => "/downloads" }
result=client.call("aria2.addUri", [ "http://localhost/aria2.tar.bz2" ], options)

pp result
```

```
import xmlrpclib
from pprint import pprint

s = xmlrpclib.ServerProxy("http://localhost:6800/rpc")
r = s.aria2.addUri(["http://localhost/aria2.tar.bz2"], {"dir":"/downloads"})
pprint(r)
```
