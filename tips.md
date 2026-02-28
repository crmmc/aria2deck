# 后端逻辑错误与 Bug 清单

**审查方式**: 6 个 subagent 按用户故事并行巡查
**审查时间**: 2026-03-01

---

## Critical - 严重 (4 个)

### 1. 孤儿清理扫描层级错误，会删除整个 prefix 目录
- **文件**: `services/orphan_cleanup.py:34-45`
- **描述**: 只遍历 `store_dir.iterdir()` 顶层，而实际结构是 `/store/{prefix}/{hash}`。顶层 `ab/` 目录与 DB 的 `real_path` 不匹配，会被当作孤儿递归删除
- **影响**: 灾难性误删所有文件
- **建议**: 扫描到 `prefix/hash` 层级再判定；仅删 hash 目录而非 prefix 目录

### 2. 启动时 repair 和 cleanup 并发执行，竞态误删
- **文件**: `main.py:141-142`
- **描述**: `run_startup_repair()` 与 `cleanup_orphan_files()` 被 `asyncio.create_task` 并发启动，一个在扫描补库，另一个在删文件
- **影响**: 边修边删导致数据丢失
- **建议**: 串行执行（先 repair 再 cleanup）

### 3. 去重时未校验旧文件是否存在，直接删新副本
- **文件**: `services/storage.py:254-270`
- **描述**: 查到同 hash 的 `StoredFile` 后直接删除新下载副本，未验证旧记录 `real_path` 是否存在
- **影响**: 误删唯一可用副本
- **建议**: 命中去重后校验 `real_path` 存在；校验失败时用新副本替换

### 4. 文件移动与 DB 写入非原子，失败产生孤儿
- **文件**: `services/storage.py:284-358`
- **描述**: 先 `shutil.move()`，后创建 `StoredFile` 记录。DB 写入失败时文件已移动但记录不存在
- **影响**: 有文件无记录，形成隐式孤儿
- **建议**: 使用临时目标 + DB 成功后 rename；或 DB 失败时回滚文件移动

---

## High - 高危 (8 个)

### 5. 可降级最后一个管理员，系统进入无管理员状态
- **文件**: `routers/users.py:356-363`
- **描述**: `update_user` 只禁止管理员取消自己的权限，没有禁止把最后一个管理员降级
- **影响**: 系统进入无管理员状态，管理功能锁死
- **建议**: 降级前查询管理员总数，若 <=1 则拒绝

### 6. 删除用户跨多事务，可能半删除
- **文件**: `routers/users.py:230-276`
- **描述**: 删除流程拆成多个独立事务，中间任一步失败会留下部分已删、部分未删的状态
- **影响**: 数据不一致，增加修复成本
- **建议**: 将 DB 级动作统一到同一事务

### 7. 创建分享码并发竞态，UNIQUE 冲突返回 500
- **文件**: `routers/shares.py:141-167`
- **描述**: 先 select 检查分享码是否存在，再 insert。并发下可能触发 UNIQUE 冲突
- **影响**: 用户随机遇到服务器错误
- **建议**: 捕获 `IntegrityError` 并重试生成新 code

### 8. 下载流程 TOCTOU，撤销/过期后仍可下载
- **文件**: `routers/shares.py:343-387`
- **描述**: 先 `_check_share_access()` 判断有效，再单独 update。update 条件未再次校验 status/expires_at
- **影响**: 分享失效状态与实际访问控制不一致
- **建议**: 将有效性判断 + 次数扣减合并成单条原子 SQL

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
