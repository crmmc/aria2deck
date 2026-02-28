# 当前任务交接（上下文压缩）

## 目标
做一个“统一安全删除入口”改造（例如 `safe_delete_path(base_dir, target)`），把项目中分散的删除点都收口，并强制路径必须位于白名单根目录下，彻底把误删面再收窄。

## 用户明确要求
- 统一安全删除入口
- 收口分散删除逻辑
- 强制白名单根目录约束
- **特别注意防止路径穿透攻击（Path Traversal）**

## 当前结论（已分析）
- 删除逻辑还没有 100% 统一。
- 主流程已有一定统一（如 `delete_user_file_reference`、`cleanup_task_download_dir`、`cleanup_failed_task_artifacts`），但仍存在分散删除点。

## 重点改造点（按优先级）

### P0（优先）
1. `backend/app/services/storage.py`
   - `_delete_stored_file_by_path(real_path)`
   - 目前按数据库 `real_path` 删除，需增加白名单根目录强校验（必须在 store 根目录内）。
2. `backend/app/routers/users.py:286`
   - 当前 `shutil.rmtree(user_download_dir, ignore_errors=True)`
   - 改为调用统一安全删除入口（含边界校验和日志）。

### P1（建议）
3. `backend/app/services/pack.py`
   - 多处 `output_path.unlink()`（240,249,265,304,317,321）
4. `backend/app/routers/files.py`
   - 清理 pack 失败/取消残留（471,540）
   - 以上都应统一走安全删除入口。

## 建议实现方案

### 1) 在 `backend/app/services/storage.py` 增加统一原语
- `safe_delete_path(base_dir: Path, target: Path, recursive: bool = False, allow_missing: bool = True) -> bool`
- 能力：
  - 规范化路径（`resolve(strict=False)`）
  - 防穿透（`target` 必须 `relative_to(base_dir.resolve())`）
  - 可选递归删除目录 / 删除文件
  - 统一日志输出
  - 对不存在路径按 `allow_missing` 处理

### 2) 防路径穿透细则（必须）
- 禁止删除任何不在白名单根目录下的路径。
- 对符号链接谨慎处理：
  - 删除“链接本身”而非跟随到外部目标。
  - 目录递归删除前要确认真实路径仍在白名单内。
- 拒绝空路径、根路径、白名单根本身（除非显式允许）。

### 3) 白名单根目录建议
- `download_dir/store`
- `download_dir/downloading`
- （如需）`download_dir/<user_id>` 用户目录
- pack 临时输出目录（如果固定可控）

### 4) 收口替换清单
- `_delete_stored_file_by_path` -> `safe_delete_path(...)`
- `cleanup_task_download_dir` -> `safe_delete_path(...)`
- `users.py` 删除用户目录 -> 调用 storage 安全入口
- `pack.py` / `files.py` 对 output_path 删除 -> 调用统一安全入口（或封装 `cleanup_pack_output` 后内部再调 `safe_delete_path`）

## 测试建议
新增测试覆盖：
1. 正常删除（文件/目录）
2. 路径不存在
3. 路径越界（`../`、绝对路径跳出白名单）
4. 符号链接场景（指向白名单外）
5. 并发删除幂等性

## 预期收益
- 降低误删风险
- 防止路径穿透攻击
- 删除逻辑可审计、可维护、可测试
