# 任务拆分草稿 - UX Enhancement (003)

## 功能 1: BT 种子文件上传

### 后端任务
1. **aria2 客户端扩展**: 在 `client.py` 添加 `add_torrent` 方法
2. **API 路由**: 在 `tasks.py` 添加 `POST /api/tasks/torrent` 接口

### 前端任务
3. **API 封装**: 在 `api.ts` 添加 `uploadTorrent` 函数
4. **UI 组件**: 在任务页面添加种子上传按钮和文件选择器

---

## 功能 2: 浏览器通知

### 前端任务
5. **通知工具模块**: 新建 `lib/notification.ts`
6. **WebSocket 集成**: 在任务页面监听状态变化并发送通知
7. **设置页面**: 添加通知开关设置项

---

## 功能 3: 任务拖拽排序

### 后端任务
8. **aria2 客户端扩展**: 在 `client.py` 添加 `change_position` 方法
9. **API 路由**: 在 `tasks.py` 添加 `PUT /api/tasks/{id}/position` 接口

### 前端任务
10. **依赖安装**: 安装 `@dnd-kit/core` 和 `@dnd-kit/sortable`
11. **API 封装**: 在 `api.ts` 添加 `changeTaskPosition` 函数
12. **拖拽实现**: 在任务列表页面实现拖拽排序功能

---

## 收尾任务
13. **前端构建验证**: 运行 `make build` 确保编译通过
