# 后端逻辑错误与 Bug 清单

**审查方式**: 6 个 subagent 按用户故事并行巡查
**审查时间**: 2026-03-01
**状态**: 部分已修复

---

## 已修复 ✅

| # | 级别 | 问题 | 提交 |
|---|------|------|------|
| 1 | Critical | 孤儿清理扫描层级错误 | d2e9697 |
| 2 | Critical | repair/cleanup 并发竞态 | d2e9697 |
| 3 | Critical | 去重未校验旧文件存在 | d2e9697 |
| 4 | Critical | 文件移动与 DB 非原子 | d2e9697 |
| 5 | High | 可降级最后一个管理员 | d2e9697 |
| 6 | High | 删除用户跨多事务 | d2e9697 |
| 7 | High | 分享码并发竞态 | d2e9697 |
| 8 | High | 下载流程 TOCTOU | d2e9697 |

---

## 待修复

### High (4 个)

### 9. stop/error 并发路径重复写失败历史
- **文件**: `aria2/listener.py:741-777`, `aria2/sync.py:752-785`
- **描述**: WebSocket 监听器与轮询同步都在处理失败时写历史，并发时会重复写入
- **影响**: 历史记录重复，统计不准确
- **建议**: 只对本次实际更新成功的订阅写历史

### 10. 管理员批量删除绕过引用计数
- **文件**: `routers/storage.py:234-246`
- **描述**: 部分删除失败时仍可能直接 `db.delete(StoredFile)` 并删物理文件，绕过引用计数
- **影响**: 悬空 UserFile、文件不一致
- **建议**: 禁止强制删绕过引用计数；先校验引用数为 0 再删

### 11. 打包空间校验未计入进行中任务的预留空间
- **文件**: `routers/files.py:822-834`
- **描述**: 使用 `get_user_space_info()` 校验，但该值不包含 `pack_tasks.reserved_space`
- **影响**: 空间超卖、磁盘打满
- **建议**: 改用 `get_user_available_space_for_pack()`

### 12. 修复任务按文件名匹配，可能错绑
- **文件**: `services/repair.py:165-187`
- **描述**: 通过 `task.name.lower()` 匹配 `StoredFile.original_name`，同名文件会命中第一条
- **影响**: 任务关联错误文件
- **建议**: 优先用 content_hash/size 等可验证标识修复

---

## Medium - 中危 (9 个)

### 13. token 放 URL Query，存在泄露风险
- **文件**: `routers/shares.py:339`
- **描述**: download/browse 使用 token query 参数，URL 会进入浏览器历史、代理日志
- **影响**: 密码分享凭证泄露
- **建议**: 改为 Authorization header 传输

### 14. 分享存在但文件缺失时错误语义不准确
- **文件**: `routers/shares.py:256-268`
- **描述**: 使用内连接查询，文件被删时返回"分享不存在"而非"文件已删除"
- **影响**: 排障困难
- **建议**: 区分返回 404(分享不存在) 与 410(文件已删除)

### 15. RPC 客户端未检查 HTTP 状态码和非 JSON 响应
- **文件**: `aria2/client.py:44-48`
- **描述**: 直接 `await resp.json()`，未检查状态码，非 JSON 响应会抛框架异常
- **影响**: 异常不透明，难以识别暂时不可用 vs 业务错误
- **建议**: 增加 status 校验与 try/except 包装

### 16. 订阅状态判断了不存在的 "active" 分支
- **文件**: `routers/tasks.py:681,1036`
- **描述**: 模型定义为 pending/success/failed，但代码判断了 "active"
- **影响**: 死代码、维护误导
- **建议**: 移除 "active" 分支

### 17. "当前任务"筛选漏掉 waiting/paused
- **文件**: `routers/tasks.py:1219-1228`
- **描述**: 仅包含 queued/active，但同步模块把 waiting/paused 当作活动状态
- **影响**: 任务列表不完整
- **建议**: 筛选条件与同步状态集对齐

### 18. GID 不存在识别条件过严
- **文件**: `aria2/sync.py:95-98`
- **描述**: 要求错误信息同时包含 "gid" 和 "not found"，实际文案可能变化
- **影响**: 僵尸任务不转失败，冻结空间无法释放
- **建议**: 改为更稳健判定，设置重试上限后兜底标记失败

### 19. Range 下载并发删除时缺异常兜底
- **文件**: `routers/files.py:163-233`
- **描述**: 先 stat() 再 open()，文件被并发删除时 FileNotFoundError 未转为业务错误
- **影响**: 返回 500
- **建议**: 捕获 FileNotFoundError 返回 404

### 20. 打包源路径缺白名单校验
- **文件**: `routers/files.py:236-259`, `services/pack.py:179-188`
- **描述**: 直接使用 DB 的 real_path，未校验是否在允许目录内
- **影响**: DB 数据被污染时可能泄露敏感文件
- **建议**: 增加 real_path 白名单校验

### 21. 删除用户未覆盖新架构关联数据
- **文件**: `routers/users.py:250-258`
- **描述**: 仅删除旧表，未显式处理 UserTaskSubscription、TaskHistory、ShareLink 等
- **影响**: 残留脏数据
- **建议**: 按共享架构统一清理策略处理

---

## Low - 低危 (4 个)

### 22. 权限错误信息用英文，与项目约束冲突
- **文件**: `auth.py:87-90`
- **描述**: `require_admin` 返回 "Admin required"，项目要求中文
- **建议**: 改为 "需要管理员权限"

### 23. 用户名/密码仅长度校验，缺格式约束
- **文件**: `schemas.py:13-14,21-22`
- **描述**: 未限制全空白、前后空格、非法字符
- **建议**: 增加 trim 后非空、限定字符集

### 24. create_user 重复调用 _has_any_user()
- **文件**: `routers/users.py:56,69`
- **描述**: 同一请求内两次判断，存在 TOCTOU 窗口
- **建议**: 单次读取状态

### 25. PackTask.status 是裸字符串，缺模型约束
- **文件**: `models.py:93`
- **描述**: 状态值完全靠业务代码约定，无 enum/check constraint
- **建议**: 使用 Enum 或数据库 CHECK 约束

---

## 优先修复建议

**立即修复** (Critical 1-4):
- 孤儿清理扫描层级 - 可能删除所有文件
- 启动时 repair/cleanup 串行化
- 去重时校验旧文件存在性
- 文件移动原子性保证

**尽快修复** (High 5-8):
- 最后一个管理员保护
- 删除用户事务一致性
- 分享码并发竞态处理
- 打包空间预留校验
