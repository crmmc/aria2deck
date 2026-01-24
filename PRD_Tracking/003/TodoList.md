# TodoList - UX Enhancement (003)

**任务编号**: 003
**创建时间**: 2026-01-23
**状态**: 已完成

---

## Phase 1: BT 种子上传 (后端)

1. [x][S] 扩展 aria2 客户端 - 添加 `add_torrent` 方法到 `backend/app/aria2/client.py`
2. [x][S] 添加种子上传 API - 在 `backend/app/routers/tasks.py` 添加 `POST /api/tasks/torrent` 接口

## Phase 2: BT 种子上传 (前端)

3. [x][S] 扩展前端 API - 在 `frontend/lib/api.ts` 添加 `uploadTorrent` 函数
4. [x][S] 实现种子上传 UI - 在 `frontend/app/tasks/page.tsx` 添加上传按钮和文件选择器

## Phase 3: 浏览器通知 (前端)

5. [x][S] 创建通知工具模块 - 新建 `frontend/lib/notification.ts`
6. [x][P] 集成 WebSocket 通知 - 在任务页面监听状态变化并发送通知
7. [x][P] 添加通知设置 - 在设置页面添加通知开关

## Phase 4: 任务拖拽排序 (后端)

8. [x][S] 扩展 aria2 客户端 - 添加 `change_position` 方法
9. [x][S] 添加位置调整 API - 在 `backend/app/routers/tasks.py` 添加 `PUT /api/tasks/{id}/position` 接口

## Phase 5: 任务拖拽排序 (前端)

10. [x][S] 安装拖拽依赖 - 安装 `@dnd-kit/core` 和 `@dnd-kit/sortable`
11. [x][S] 扩展前端 API - 在 `frontend/lib/api.ts` 添加 `changeTaskPosition` 函数
12. [x][S] 实现拖拽排序 - 在任务列表页面实现拖拽功能

## Phase 6: 验证

13. [x][S] 构建验证 - 运行 `make build` 确保编译通过

---

## 执行记录

| 任务 | 开始时间 | 完成时间 | 状态 | 备注 |
|------|---------|---------|------|------|
| 1 | 2026-01-23 | 2026-01-23 | 完成 | 添加 add_torrent 方法 |
| 2 | 2026-01-23 | 2026-01-23 | 完成 | 添加 POST /api/tasks/torrent 接口 |
| 3 | 2026-01-23 | 2026-01-23 | 完成 | 添加 uploadTorrent 函数 |
| 4 | 2026-01-23 | 2026-01-23 | 完成 | 添加种子上传按钮和文件选择器 |
| 5 | 2026-01-23 | 2026-01-23 | 完成 | 创建 notification.ts 通知工具模块 |
| 7 | 2026-01-23 | 2026-01-23 | 完成 | 在设置页面添加浏览器通知开关和选项 |
| 8 | 2026-01-23 | 2026-01-23 | 完成 | 添加 change_position 方法 |
| 9 | 2026-01-23 | 2026-01-23 | 完成 | 添加 PUT /api/tasks/{id}/position 接口 |
| 10 | 2026-01-23 | 2026-01-23 | 完成 | 安装 @dnd-kit/core@6.3.1 和 @dnd-kit/sortable@10.0.0 |
| 11 | 2026-01-23 | 2026-01-23 | 完成 | 添加 changeTaskPosition 函数 |
| 12 | 2026-01-23 | 2026-01-23 | 完成 | 实现拖拽排序功能（DndContext + SortableTaskCard 组件） |
| 13 | 2026-01-23 | 2026-01-23 | 完成 | make build 构建成功，前端编译通过 |
