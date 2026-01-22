# Design: 修复 P2/P3 级别问题

## 📌 功能概述

修复代码审查中发现的 P2/P3 级别问题，提升安全性、健壮性和代码质量。

### P2 (Important) - 7 项
| ID | 问题 | 修复方案 |
|----|------|---------|
| P2-1 | 登录无速率限制 | 内存限速器，5分钟内最多5次失败 |
| P2-2 | 输入验证缺失 | Pydantic Field 约束 |
| P2-3 | 配置查询无缓存 | 模块级缓存 + TTL |
| P2-4 | 同步任务顺序执行 | asyncio.gather 并发 |
| P2-5 | CORS 配置过于宽松 | 已修复（仅开发环境域名），跳过 |
| P2-6 | 路径验证未检查符号链接 | 添加 symlink 检查 |
| P2-7 | 数值输入无验证 | 前端添加 min/max 属性 |

### P3 (Nit) - 5 项
| ID | 问题 | 修复方案 |
|----|------|---------|
| P3-1 | PBKDF2 轮数偏低 | 120000 已符合 OWASP 2023，跳过 |
| P3-2 | aria2_rpc_secret 建议 SecretStr | 影响范围大，跳过 |
| P3-3 | 菜单活跃链接判断 | 修复为精确匹配 + 子路径 |
| P3-4 | 配额单位转换重复 | 提取工具函数 |
| P3-5 | 磁盘空间计算性能 | 添加计算结果缓存 |

---

## 🏗️ 架构设计

### P2-1: 登录速率限制

**方案**: 基于 IP 的内存限速器

```python
# 新建 app/core/rate_limit.py
from collections import defaultdict
from time import time

class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def is_blocked(self, key: str) -> bool:
        now = time()
        # 清理过期记录
        self._attempts[key] = [t for t in self._attempts[key] if now - t < self.window]
        return len(self._attempts[key]) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        self._attempts[key].append(time())

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)

# 全局实例
login_limiter = LoginRateLimiter()
```

**使用位置**: `routers/auth.py` 的 login 函数

---

### P2-2: 输入验证

**方案**: 使用 Pydantic Field 约束

```python
# schemas.py
from pydantic import Field

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=100)

class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=6, max_length=100)
    is_admin: bool = False
    quota: int | None = Field(default=None, ge=0, le=10 * 1024 * 1024 * 1024 * 1024)  # 最大 10TB
```

---

### P2-3: 配置缓存

**方案**: 模块级缓存 + 60 秒 TTL

```python
# routers/config.py
_config_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 60.0  # 秒

def get_config_value(key: str) -> str | None:
    now = time()
    if key in _config_cache:
        value, ts = _config_cache[key]
        if now - ts < _CACHE_TTL:
            return value
    row = fetch_one("SELECT value FROM config WHERE key = ?", [key])
    value = row["value"] if row else None
    _config_cache[key] = (value, now)
    return value

def set_config_value(key: str, value: str) -> None:
    execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", [key, value])
    _config_cache[key] = (value, time())  # 更新缓存
```

---

### P2-4: 同步任务并发

**方案**: 使用 asyncio.gather 并发查询

```python
# aria2/sync.py
async def sync_tasks(...):
    while True:
        tasks = fetch_all(...)
        # 并发查询所有任务状态
        async def fetch_status(task):
            try:
                return task, await client.tell_status(task["gid"])
            except Exception as exc:
                return task, exc

        results = await asyncio.gather(*[fetch_status(t) for t in tasks if t["gid"]])

        for task, result in results:
            if isinstance(result, Exception):
                _update_task(task["id"], {"status": "error", "error": str(result)})
            else:
                # 处理正常结果...
```

---

### P2-6: 符号链接检查

**方案**: 在路径验证中添加 symlink 检查

```python
# routers/files.py
def _validate_path(user_dir: Path, relative_path: str) -> Path:
    if not relative_path:
        return user_dir

    target = (user_dir / relative_path).resolve()

    # 确保目标路径在用户目录内
    try:
        target.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="无权访问此路径")

    # 检查是否为符号链接且指向用户目录外
    if target.is_symlink():
        real_target = target.resolve()
        try:
            real_target.relative_to(user_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="无权访问此路径")

    return target
```

---

### P2-7: 前端数值验证

**方案**: 为数值输入添加 min/max 属性

```typescript
// settings/page.tsx
<input
  type="number"
  step="0.1"
  min="0"
  max="10240"  // 10TB
  value={maxTaskSize}
  onChange={(e) => setMaxTaskSize(e.target.value)}
/>
```

---

### P3-3: 菜单活跃链接

**现状**: `pathname?.startsWith(item.href)` 会导致 `/tasks` 匹配 `/tasks/detail`

**修复**: 精确匹配或子路径匹配

```typescript
// Sidebar.tsx
const isActive = (href: string) => {
  if (!pathname) return false;
  if (href === "/tasks") {
    return pathname === "/tasks" || pathname.startsWith("/tasks/");
  }
  return pathname === href;
};

// 使用
className={`nav-item ${isActive(item.href) ? "active" : ""}`}
```

---

### P3-4: 配额单位转换

**方案**: 提取工具函数到 `lib/utils.ts`

```typescript
// lib/utils.ts
export function bytesToGB(bytes: number): string {
  return (bytes / 1024 / 1024 / 1024).toFixed(2);
}

export function gbToBytes(gb: number): number {
  return gb * 1024 * 1024 * 1024;
}
```

---

### P3-5: 磁盘空间缓存

**方案**: 缓存目录大小计算结果，30 秒 TTL

```python
# routers/files.py
_dir_size_cache: dict[str, tuple[int, float]] = {}
_DIR_SIZE_CACHE_TTL = 30.0

def _calculate_dir_size(path: Path) -> int:
    key = str(path)
    now = time()
    if key in _dir_size_cache:
        size, ts = _dir_size_cache[key]
        if now - ts < _DIR_SIZE_CACHE_TTL:
            return size

    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass

    _dir_size_cache[key] = (total, now)
    return total
```

---

## 🔄 业务流程

无变化，仅增强安全性和性能。

---

## 🎨 设计原则

1. **迭代兼容性**: 所有修改向后兼容
2. **最小改动**: 仅修复问题，不重构
3. **无新依赖**: 使用标准库实现

---

## 🚨 风险分析

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 速率限制误伤 | 合法用户被锁 | 5分钟自动解锁 |
| 缓存不一致 | 配置更新延迟 | TTL 60秒可接受 |
| 并发查询压力 | aria2 负载增加 | 任务数量有限，可接受 |

---

## 📏 验收标准

1. [ ] 登录失败 5 次后被限制，5 分钟后恢复
2. [ ] 用户名/密码长度验证生效
3. [ ] 配置查询有缓存
4. [ ] 同步任务并发执行
5. [ ] 符号链接路径被拒绝
6. [ ] 前端数值输入有范围限制
7. [ ] 菜单活跃状态正确
8. [ ] `make build` 编译通过
