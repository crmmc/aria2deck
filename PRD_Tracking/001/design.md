# Design: 修复代码审查发现的 Critical 问题

## 📌 功能概述

修复代码审查中发现的 5 个 Critical 级别安全/健壮性问题：

| ID | 问题 | 位置 |
|----|------|------|
| P1-1 | aria2 回调接口无认证 | `hooks.py` |
| P1-2 | SQLite 并发不安全 | `db.py` |
| P1-3 | HTTP 请求无超时 | `aria2/client.py` |
| P1-4 | 时区比较陷阱 | `auth.py` |
| P1-5 | WebSocket 无错误处理/重连 | `tasks/page.tsx` |

---

## 🏗️ 架构设计

### P1-1: hooks.py 无认证

**现状分析**:
- `/api/hooks/aria2` 接口完全公开
- aria2 通过外部脚本调用此接口
- 攻击者可伪造请求修改任务状态

**修复方案**: 添加简单的 Token 认证

```python
# 在 config.py 中添加 hook_secret 配置
ARIA2C_HOOK_SECRET: str = ""  # 环境变量配置

# 在 hooks.py 中验证
from fastapi import Header

@router.post("/aria2")
async def aria2_hook(
    payload: Aria2HookPayload,
    request: Request,
    x_hook_secret: str | None = Header(None)
) -> dict:
    expected = settings.hook_secret
    if expected and x_hook_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid hook secret")
    # ... 原有逻辑
```

**回调脚本更新**: 需在 `aria2_hook.sh` 中添加 Header

---

### P1-2: SQLite 并发不安全

**现状分析**:
- `check_same_thread=False` 允许多线程访问
- SQLite 写操作不是线程安全的
- 多个写操作可能导致 `database is locked` 错误

**修复方案**: 使用连接池 + 写锁

```python
import threading

_db_lock = threading.Lock()

@contextmanager
def db_cursor():
    with _db_lock:  # 串行化所有数据库操作
        conn = get_connection()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()
```

**权衡**: 串行化会降低并发性能，但保证数据安全。对于本项目的使用场景（单机部署、用户量小），可接受。

---

### P1-3: HTTP 请求无超时

**现状分析**:
- aiohttp 请求无超时设置
- aria2 无响应时会永久阻塞

**修复方案**: 添加合理超时

```python
import aiohttp

class Aria2Client:
    DEFAULT_TIMEOUT = 30  # 秒

    async def _call(self, method: str, params: list | None = None) -> dict:
        payload = {...}
        timeout = aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self._rpc_url, json=payload) as resp:
                # ...
```

---

### P1-4: 时区比较陷阱

**现状分析**:
```python
expires_at = datetime.fromisoformat(session["expires_at"])
# expires_at 可能是 naive datetime（无时区信息）
if expires_at < datetime.now(timezone.utc):
    # 比较 naive 和 aware datetime 会报错或产生错误结果
```

**修复方案**: 确保时区一致

```python
def get_user_by_session(session_id: str | None) -> dict | None:
    # ...
    expires_at = datetime.fromisoformat(session["expires_at"])
    # 确保有时区信息
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        clear_session(session_id)
        return None
    # ...
```

---

### P1-5: WebSocket 无错误处理/重连

**现状分析**:
```typescript
useEffect(() => {
    const ws = new WebSocket(taskWsUrl());
    ws.onmessage = (event) => {...};
    // 无 onerror、onclose 处理
    // 连接断开后不会重连
}, []);
```

**修复方案**: 添加错误处理和自动重连

```typescript
useEffect(() => {
  let ws: WebSocket | null = null;
  let reconnectTimeout: NodeJS.Timeout;
  let pingInterval: NodeJS.Timeout;

  function connect() {
    ws = new WebSocket(taskWsUrl());

    ws.onopen = () => {
      // 连接成功，启动心跳
      pingInterval = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
      }, 15000);
    };

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "task_update") {
        setTasks((prev) => {...});
      }
    };

    ws.onerror = () => {
      // 错误时关闭，触发 onclose 重连
      ws?.close();
    };

    ws.onclose = () => {
      clearInterval(pingInterval);
      // 3 秒后重连
      reconnectTimeout = setTimeout(connect, 3000);
    };
  }

  connect();

  return () => {
    clearTimeout(reconnectTimeout);
    clearInterval(pingInterval);
    ws?.close();
  };
}, []);
```

---

## 🔄 业务流程

无变化，仅增强健壮性。

---

## 🎨 设计原则

1. **迭代兼容性**: 所有修改向后兼容，不改变 API 接口
2. **最小改动**: 仅修复问题，不重构
3. **防御性编程**: 处理边界情况

---

## 🚨 风险分析

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| hook_secret 配置不当 | 回调失效 | 提供默认值（空=不验证），文档说明 |
| 数据库锁性能 | 高并发时变慢 | 可接受，用户量小 |
| WebSocket 频繁重连 | 服务器压力 | 重连间隔 3 秒，指数退避可后续优化 |

---

## 🛠️ 技术选型

- 无新增依赖
- 使用 Python 标准库 `threading.Lock`
- 使用 aiohttp 内置 `ClientTimeout`

---

## 📏 验收标准

1. [ ] `hooks.py` 添加 token 验证，无 token 时不验证（兼容现有部署）
2. [ ] `db.py` 添加写锁，并发写入不报错
3. [ ] `aria2/client.py` 添加 30 秒超时
4. [ ] `auth.py` 时区处理正确
5. [ ] `tasks/page.tsx` WebSocket 断开后自动重连
6. [ ] `make build` 编译通过
