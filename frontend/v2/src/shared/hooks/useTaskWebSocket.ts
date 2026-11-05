import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/shared/api/client";
import type { PublicSiteInfoResponse, TaskResponse } from "@/shared/api/types";

/** WS-05 前端侧：单连接、指数退避+jitter、隐藏时 5s 轮询补偿、过期消息丢弃、4401 登出。 */

export interface WebSocketState {
  connected: boolean;
}

const DEFAULT_BASE_DELAY_MS = 2000;
const DEFAULT_MAX_DELAY_MS = 30000;
const DEFAULT_JITTER_MS = 1000;
const DEFAULT_FACTOR = 2;
const POLL_WHEN_HIDDEN_MS = 5000;

export interface ReconnectPolicy {
  baseDelayMs: number;
  maxDelayMs: number;
  jitterMs: number;
  factor: number;
}

export const DEFAULT_POLICY: ReconnectPolicy = {
  baseDelayMs: DEFAULT_BASE_DELAY_MS,
  maxDelayMs: DEFAULT_MAX_DELAY_MS,
  jitterMs: DEFAULT_JITTER_MS,
  factor: DEFAULT_FACTOR,
};

function computeBackoff(attempt: number, policy: ReconnectPolicy): number {
  const exponential = Math.min(
    policy.baseDelayMs * policy.factor ** attempt,
    policy.maxDelayMs,
  );
  return exponential + Math.random() * policy.jitterMs;
}

/** 重连参数来自公开站点信息（CFG-01）；任何失败或非法值回退默认策略。 */
async function resolvePolicy(): Promise<ReconnectPolicy> {
  try {
    const info = await api.get<PublicSiteInfoResponse>("/api/v2/public/site-info");
    const maxDelayMs = info.ws_reconnect_max_delay_seconds * 1000;
    const jitterMs = info.ws_reconnect_jitter_seconds * 1000;
    const factor = info.ws_reconnect_factor;
    if (
      !Number.isFinite(maxDelayMs) ||
      maxDelayMs < 1000 ||
      !Number.isFinite(jitterMs) ||
      jitterMs < 0 ||
      !Number.isFinite(factor) ||
      factor < 1
    ) {
      return DEFAULT_POLICY;
    }
    return { ...DEFAULT_POLICY, maxDelayMs, jitterMs, factor };
  } catch {
    return DEFAULT_POLICY;
  }
}

export interface TaskUpdateFrame {
  type: string;
  task: TaskResponse;
}

export function useTaskWebSocket(
  onMessage: (frame: TaskUpdateFrame) => void,
  options: {
    enabled?: boolean;
    onUnauthorized?: () => void;
    pollFallback?: () => void;
  } = {},
): WebSocketState {
  const { enabled = true, onUnauthorized, pollFallback } = options;
  const [connected, setConnected] = useState(false);
  const attemptRef = useRef(0);
  const seenUpdatedRef = useRef(new Map<number, number>());
  const messageRef = useRef(onMessage);
  const unauthorizedRef = useRef(onUnauthorized);
  const pollRef = useRef(pollFallback);

  messageRef.current = onMessage;
  unauthorizedRef.current = onUnauthorized;
  pollRef.current = pollFallback;

  const handleFrame = useCallback((frame: TaskUpdateFrame) => {
    if (frame?.type !== "task_update" || frame.task === undefined) return;
    const task = frame.task;
    const seen = seenUpdatedRef.current.get(task.id) ?? 0;
    if (task.updated_at_ms <= seen) return; // 过期/重复消息丢弃。
    seenUpdatedRef.current.set(task.id, task.updated_at_ms);
    messageRef.current(frame);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setConnected(false);
      return;
    }
    let closed = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | undefined;
    let pollTimer: number | undefined;
    let visibilityBound = false;

    const scheduleReconnect = (policy: ReconnectPolicy) => {
      if (closed) return;
      const delay = computeBackoff(attemptRef.current, policy);
      attemptRef.current += 1;
      retryTimer = window.setTimeout(() => connect(policy), delay);
    };

    const connect = (policy: ReconnectPolicy) => {
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${protocol}://${window.location.host}/ws/tasks`);
      socket.onopen = () => {
        attemptRef.current = 0;
        setConnected(true);
        // 重连后重读 current（服务端权威）。
        pollRef.current?.();
      };
      socket.onmessage = (event) => {
        try {
          handleFrame(JSON.parse(event.data) as TaskUpdateFrame);
        } catch {
          /* 坏消息忽略 */
        }
      };
      socket.onclose = (event) => {
        setConnected(false);
        socket = null;
        if (event.code === 4401) {
          unauthorizedRef.current?.();
          return; // 会话失效：不再重连，等登录后重新挂载。
        }
        scheduleReconnect(policy);
      };
      socket.onerror = () => {
        socket?.close();
      };
    };

    const onVisibility = () => {
      if (document.hidden) {
        pollTimer = window.setInterval(() => pollRef.current?.(), POLL_WHEN_HIDDEN_MS);
      } else {
        window.clearInterval(pollTimer);
        pollTimer = undefined;
      }
    };

    const start = async () => {
      // 先取一次站点重连参数再建连，避免建连后策略变化导致断线重连。
      const policy = await resolvePolicy();
      if (closed) return;
      connect(policy);
      document.addEventListener("visibilitychange", onVisibility);
      visibilityBound = true;
    };
    void start();

    return () => {
      closed = true;
      if (visibilityBound) document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(retryTimer);
      window.clearInterval(pollTimer);
      socket?.close();
      setConnected(false);
    };
  }, [enabled, handleFrame]);

  return { connected };
}
