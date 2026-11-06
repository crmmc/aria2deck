/** 任务活动事件（G11）：持有 WS 连接的页面发布，布局统计等订阅后立即刷新，避免重复长连接。 */

const TASK_ACTIVITY_EVENT = "aria2deck:task-activity";

export function emitTaskActivity(): void {
  window.dispatchEvent(new CustomEvent(TASK_ACTIVITY_EVENT));
}

export function onTaskActivity(listener: () => void): () => void {
  const handler = () => listener();
  window.addEventListener(TASK_ACTIVITY_EVENT, handler);
  return () => window.removeEventListener(TASK_ACTIVITY_EVENT, handler);
}
