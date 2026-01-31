"use client";

import { useEffect, useRef } from "react";
import { taskWsUrl } from "@/lib/api";
import type { Task } from "@/types";

export interface TaskWebSocketCallbacks {
  onTaskUpdate: (task: Task) => void;
  onNotification: (message: string, level: "info" | "warning" | "error") => void;
  onConnected?: () => void;
  onDisconnected?: () => void;
}

export function useTaskWebSocket(callbacks: TaskWebSocketCallbacks) {
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let pingInterval: ReturnType<typeof setInterval>;
    let retryCount = 0;
    let lastPongTime = Date.now();

    function getReconnectDelay(): number {
      const base = Math.min(1000 * Math.pow(2, retryCount), 30000);
      const jitter = Math.random() * 1000;
      return base + jitter;
    }

    function connect() {
      ws = new WebSocket(taskWsUrl());

      ws.onopen = () => {
        retryCount = 0;
        lastPongTime = Date.now();
        callbacksRef.current.onConnected?.();

        pingInterval = setInterval(() => {
          if (!ws || ws.readyState !== WebSocket.OPEN) return;

          if (Date.now() - lastPongTime > 45000) {
            ws.close();
            return;
          }
          ws.send("ping");
        }, 15000);
      };

      ws.onmessage = (event) => {
        if (event.data === "pong") {
          lastPongTime = Date.now();
          return;
        }

        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "task_update") {
            callbacksRef.current.onTaskUpdate(payload.task);
          } else if (payload.type === "notification") {
            const level =
              payload.level === "error"
                ? "error"
                : payload.level === "warning"
                  ? "warning"
                  : "info";
            callbacksRef.current.onNotification(payload.message, level);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onerror = () => {
        ws?.close();
      };

      ws.onclose = () => {
        clearInterval(pingInterval);
        callbacksRef.current.onDisconnected?.();
        retryCount++;
        reconnectTimeout = setTimeout(connect, getReconnectDelay());
      };
    }

    connect();

    return () => {
      clearTimeout(reconnectTimeout);
      clearInterval(pingInterval);
      ws?.close();
    };
  }, []);
}
