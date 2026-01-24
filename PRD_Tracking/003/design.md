# 用户体验增强 设计文档

作者：wwj
版本：v1.0
日期：2026-01-23
状态：草稿

---

## 一、需求背景

参考 AriaNg 项目的功能实现，为 aria2_controler 添加以下用户体验增强功能：

1. **BT 种子文件上传** - 支持上传 .torrent 文件创建下载任务
2. **浏览器通知** - 任务完成或出错时发送桌面通知
3. **任务拖拽排序** - 拖拽调整任务在队列中的优先级

---

## 二、核心原则

1. **最小改动**：复用现有代码结构，不重构已有功能
2. **向后兼容**：新功能默认开启，不影响现有使用方式
3. **渐进增强**：通知功能在用户授权后才启用

---

## 三、功能设计

### 3.1 BT 种子文件上传

#### 3.1.1 功能概述

用户可以上传 `.torrent` 种子文件来创建下载任务，作为现有 URL 输入的补充。

#### 3.1.2 界面设计

| 位置 | 元素 | 说明 |
|------|------|------|
| 任务输入区域 | 📎 按钮 | 点击打开文件选择器，支持 .torrent |
| 批量添加弹窗 | 拖拽区域 | 支持拖拽多个种子文件 |

```
现有输入框布局:
┌──────────────────────────────────────────────┬──────┬────────┬────────┐
│  粘贴磁力链接、HTTP 或 FTP URL...             │ 📎   │ + 添加 │ 批量   │
└──────────────────────────────────────────────┴──────┴────────┴────────┘
                                                 ↑新增
```

#### 3.1.3 处理逻辑

**前端流程**:
```
1. 用户点击 📎 按钮
2. 打开文件选择器（accept=".torrent"）
3. 读取文件为 Base64
4. 调用 POST /api/tasks/torrent 上传
5. 显示创建结果
```

**后端流程**:
```
1. 接收 Base64 编码的种子内容
2. 校验：
   - 文件大小 < 10MB
   - 用户磁盘空间/配额
3. 调用 aria2.addTorrent(torrent_base64, [], options)
4. 创建数据库任务记录
5. 返回任务信息
```

---

### 3.2 浏览器通知

#### 3.2.1 功能概述

当下载任务完成或出错时，向用户发送桌面通知，即使浏览器在后台也能收到提醒。

#### 3.2.2 界面设计

| 位置 | 元素 | 说明 |
|------|------|------|
| 设置页 | 通知开关 | 启用/禁用浏览器通知 |
| 首次启用 | 权限请求 | 浏览器弹窗请求 Notification 权限 |

**通知样式**:
```
┌─────────────────────────────────────┐
│ aria2 下载控制器                    │
├─────────────────────────────────────┤
│ ✅ 下载完成                         │
│ ubuntu-22.04.iso                    │
└─────────────────────────────────────┘
```

#### 3.2.3 处理逻辑

**权限检查流程**:
```
1. 检查 localStorage 中的通知偏好设置
2. 如果 enabled=true 且权限未授权:
   - 调用 Notification.requestPermission()
3. 如果权限被拒绝:
   - 更新设置为 disabled
   - 显示提示信息
```

**通知触发流程**:
```
1. WebSocket 收到 task_update 消息
2. 检查任务状态变化:
   - 旧状态 != complete && 新状态 == complete → 发送完成通知
   - 旧状态 != error && 新状态 == error → 发送错误通知
3. 调用 new Notification(title, options)
```

**本地存储**:
```typescript
interface NotificationSettings {
  enabled: boolean;           // 是否启用
  onComplete: boolean;        // 完成时通知
  onError: boolean;           // 错误时通知
}

// localStorage key: "aria2_notification_settings"
```

---

### 3.3 任务拖拽排序

#### 3.3.1 功能概述

用户可以通过拖拽任务卡片来调整下载队列的优先级顺序。

#### 3.3.2 界面设计

| 元素 | 交互 | 说明 |
|------|------|------|
| 任务卡片左侧 | 拖拽手柄 ⋮⋮ | 鼠标悬停显示可拖拽 |
| 拖拽时 | 占位符 | 半透明显示目标位置 |
| 放置时 | 动画过渡 | 平滑移动到新位置 |

```
拖拽中:
┌──────────────────────────────────────────┐
│ ⋮⋮ [任务 A]                              │  ← 正在拖拽
└──────────────────────────────────────────┘
         ↓
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
│            放置位置指示器                  │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
┌──────────────────────────────────────────┐
│ ⋮⋮ [任务 B]                              │
└──────────────────────────────────────────┘
```

#### 3.3.3 处理逻辑

**限制条件**:
- 只有 `waiting` 状态的任务可以调整位置
- `active` 任务固定在最前面
- 已完成/错误任务不参与排序

**前端流程**:
```
1. 用户拖拽任务卡片
2. 计算目标位置（相对位置）
3. 调用 PUT /api/tasks/{id}/position
4. 乐观更新 UI（先移动，失败回滚）
```

**后端流程**:
```
1. 接收 task_id 和 position 参数
2. 获取任务的 gid
3. 调用 aria2.changePosition(gid, pos, how)
   - how: "POS_SET" | "POS_CUR" | "POS_END"
4. 返回新位置
```

---

## 四、接口设计

### 4.1 上传种子文件

```
POST /api/tasks/torrent

Content-Type: application/json

请求参数：
{
  "torrent": "string - Base64 编码的种子文件内容",
  "options": "object? - aria2 下载选项（可选）"
}

响应：
{
  "id": 123,
  "uri": "[torrent]",
  "status": "queued",
  "name": "Ubuntu 22.04.iso",
  ...
}

错误响应：
- 400: 无效的种子文件
- 403: 空间不足/配额超限
- 413: 文件过大（>10MB）
```

**实现位置**：
- Router: `backend/app/routers/tasks.py`
- aria2 方法: `aria2.addTorrent`

### 4.2 调整任务位置

```
PUT /api/tasks/{task_id}/position

请求参数：
{
  "position": "number - 目标位置（0-based）",
  "how": "string - 定位方式: 'set' | 'cur' | 'end'"
}

响应：
{
  "ok": true,
  "new_position": 0
}

错误响应：
- 400: 任务不可移动（非 waiting 状态）
- 404: 任务不存在
```

**实现位置**：
- Router: `backend/app/routers/tasks.py`
- aria2 方法: `aria2.changePosition`

---

## 五、前端设计

### 5.1 新增/修改文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `frontend/app/tasks/page.tsx` | 修改 | 添加上传按钮、拖拽功能、通知逻辑 |
| `frontend/lib/api.ts` | 修改 | 添加 uploadTorrent、changePosition 接口 |
| `frontend/lib/notification.ts` | 新增 | 通知工具函数 |
| `frontend/app/(authenticated)/settings/page.tsx` | 修改 | 添加通知设置开关 |

### 5.2 依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| `@dnd-kit/core` | ^6.0.0 | 拖拽核心库 |
| `@dnd-kit/sortable` | ^8.0.0 | 列表排序 |

**安装命令**:
```bash
cd frontend && bun add @dnd-kit/core @dnd-kit/sortable
```

### 5.3 关键代码

**通知工具函数** (`lib/notification.ts`):
```typescript
const STORAGE_KEY = "aria2_notification_settings";

interface NotificationSettings {
  enabled: boolean;
  onComplete: boolean;
  onError: boolean;
}

export function getNotificationSettings(): NotificationSettings {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return JSON.parse(stored);
  return { enabled: false, onComplete: true, onError: true };
}

export function saveNotificationSettings(settings: NotificationSettings) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
}

export async function requestNotificationPermission(): Promise<boolean> {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  const result = await Notification.requestPermission();
  return result === "granted";
}

export function sendNotification(title: string, body: string, icon?: string) {
  const settings = getNotificationSettings();
  if (!settings.enabled) return;
  if (Notification.permission !== "granted") return;

  new Notification(title, {
    body,
    icon: icon || "/favicon.ico",
    tag: "aria2-task",
  });
}
```

---

## 六、后端设计

### 6.1 aria2 客户端扩展

**文件**: `backend/app/aria2/client.py`

```python
async def add_torrent(
    self,
    torrent: str,  # Base64 encoded
    uris: list[str] | None = None,
    options: dict | None = None
) -> str:
    """添加种子任务"""
    params = [torrent, uris or []]
    if options:
        params.append(options)
    return await self._call("aria2.addTorrent", params)

async def change_position(
    self,
    gid: str,
    pos: int,
    how: str  # POS_SET, POS_CUR, POS_END
) -> int:
    """调整任务位置"""
    return await self._call("aria2.changePosition", [gid, pos, how])
```

### 6.2 新增路由

**文件**: `backend/app/routers/tasks.py`

```python
class TorrentCreate(BaseModel):
    """上传种子请求体"""
    torrent: str  # Base64 encoded
    options: dict | None = None

class PositionUpdate(BaseModel):
    """调整位置请求体"""
    position: int
    how: str = "set"  # set, cur, end

@router.post("/torrent", status_code=status.HTTP_201_CREATED)
async def create_torrent_task(
    payload: TorrentCreate,
    request: Request,
    user: dict = Depends(require_user)
) -> dict:
    """通过种子文件创建任务"""
    # 校验 Base64 大小（约 10MB 限制）
    # 检查磁盘空间和用户配额
    # 调用 aria2.addTorrent
    # 创建数据库记录
    pass

@router.put("/{task_id}/position")
async def change_task_position(
    task_id: int,
    payload: PositionUpdate,
    request: Request,
    user: dict = Depends(require_user)
) -> dict:
    """调整任务在队列中的位置"""
    # 校验任务状态（必须是 waiting）
    # 调用 aria2.changePosition
    pass
```

---

## 七、影响范围评估

### 7.1 代码改动范围

| 模块 | 文件 | 改动类型 | 说明 |
|------|------|---------|------|
| 后端 | `aria2/client.py` | 修改 | 添加 2 个方法 |
| 后端 | `routers/tasks.py` | 修改 | 添加 2 个路由 |
| 前端 | `lib/api.ts` | 修改 | 添加 2 个 API 函数 |
| 前端 | `lib/notification.ts` | 新增 | 通知工具模块 |
| 前端 | `app/tasks/page.tsx` | 修改 | UI 交互增强 |
| 前端 | `app/settings/page.tsx` | 修改 | 通知设置 |
| 前端 | `package.json` | 修改 | 添加 dnd-kit 依赖 |

### 7.2 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 种子文件解析失败 | 任务创建失败 | aria2 内部处理，返回明确错误 |
| 通知权限被拒绝 | 功能不可用 | 优雅降级，显示提示 |
| 拖拽时网络延迟 | UI 闪烁 | 乐观更新 + 失败回滚 |

---

## 八、测试要点

### 8.1 功能测试清单

**种子上传**:
- [ ] 上传有效 .torrent 文件成功创建任务
- [ ] 上传无效文件返回错误提示
- [ ] 上传超大文件（>10MB）被拒绝
- [ ] 空间不足时拒绝上传

**浏览器通知**:
- [ ] 首次启用时请求权限
- [ ] 任务完成时发送通知
- [ ] 任务出错时发送通知
- [ ] 关闭设置后不发送通知
- [ ] 点击通知跳转到任务详情

**拖拽排序**:
- [ ] waiting 任务可拖拽
- [ ] active 任务不可拖拽
- [ ] 拖拽后位置立即更新
- [ ] 网络失败时回滚位置

### 8.2 兼容性测试

| 浏览器 | 通知 | 拖拽 |
|--------|------|------|
| Chrome | ✅ | ✅ |
| Firefox | ✅ | ✅ |
| Safari | ✅ | ✅ |
| Edge | ✅ | ✅ |

---

## 九、实现顺序建议

```
Phase 1: BT 种子上传（后端 + 前端）
    ↓
Phase 2: 浏览器通知（纯前端）
    ↓
Phase 3: 拖拽排序（后端 + 前端）
```

**预计改动文件数**: 7 个

---

**文档版本历史**

| 版本 | 日期 | 作者 | 说明 |
|------|------|------|------|
| v1.0 | 2026-01-23 | wwj | 初稿 |
