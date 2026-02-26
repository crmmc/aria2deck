"use client";

import { useEffect, useRef } from "react";
import { taskWsUrl } from "@/lib/api";
import type { Task } from "@/types";

type TaskUpdatePayload = {
  type: "task_update";
  task: Task;
};

type NotificationPayload = {
  type: "notification";
  message: string;
  level?: "info" | "warning" | "error" | string;
};

type ParsedPayload = TaskUpdatePayload | NotificationPayload;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTask(value: unknown): value is Task {
  return (
    isRecord(value) &&
    typeof value.id === "number" &&
    typeof value.status === "string" &&
    typeof value.total_length === "number" &&
    typeof value.completed_length === "number" &&
    typeof value.download_speed === "number" &&
    typeof value.upload_speed === "number" &&
    typeof value.frozen_space === "number" &&
    typeof value.created_at === "string"
  );
}

function parsePayload(rawMessage: unknown): ParsedPayload | null {
  if (typeof rawMessage !== "string") {
    console.warn("[ws] Unexpected message type", rawMessage);
    return null;
  }

  let rawPayload: unknown;
  try {
    rawPayload = JSON.parse(rawMessage);
  } catch (err) {
    console.warn("[ws] Failed to parse message", err, rawMessage);
    return null;
  }

  if (!isRecord(rawPayload) || typeof rawPayload.type !== "string") {
    console.warn("[ws] Invalid payload shape", rawPayload);
    return null;
  }

  if (rawPayload.type === "task_update") {
    if (!isTask(rawPayload.task)) {
      console.warn("[ws] Invalid task_update payload", rawPayload);
      return null;
    }
    return {
      type: "task_update",
      task: rawPayload.task,
    };
  }

  if (rawPayload.type === "notification") {
    if (typeof rawPayload.message !== "string") {
      console.warn("[ws] Invalid notification payload", rawPayload);
      return null;
    }
    return {
      type: "notification",
      message: rawPayload.message,
      level: typeof rawPayload.level === "string" ? rawPayload.level : "info",
    };
  }

  console.warn("[ws] Unknown payload type", rawPayload.type);
  return null;
}

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

        const payload = parsePayload(event.data);
        if (!payload) {
          return;
        }

        if (payload.type === "task_update") {
          callbacksRef.current.onTaskUpdate(payload.task);
          return;
        }

        const level =
          payload.level === "error"
            ? "error"
            : payload.level === "warning"
              ? "warning"
              : "info";
        callbacksRef.current.onNotification(payload.message, level);
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
