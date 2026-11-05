import { act, renderHook } from "@testing-library/react";

import { useTaskWebSocket, type TaskUpdateFrame } from "@/shared/hooks/useTaskWebSocket";

type SocketState = {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  close: jest.Mock;
};

let sockets: SocketState[] = [];

class FakeWebSocket {
  static OPEN = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  close = jest.fn(() => {
    this.onclose?.({ code: 1000 });
  });

  constructor() {
    sockets.push(this);
  }
}

function frame(id: number, updatedAt: number): string {
  return JSON.stringify({
    type: "task_update",
    task: {
      id,
      updated_at_ms: updatedAt,
      status: "active",
      status_label: "下载中",
      name: "t",
      uri: null,
      total_bytes: 1,
      completed_bytes: 0,
      download_speed: 0,
      upload_speed: 0,
      peer_count: 0,
      connection_count: 0,
      files: [],
      observation_fresh: false,
      reserved_bytes: 0,
      error_code: null,
      retryable: false,
      created_at_ms: 1,
      finished_at_ms: null,
    },
  });
}

describe("useTaskWebSocket", () => {
  beforeEach(() => {
    sockets = [];
    jest.useFakeTimers();
    (global as Record<string, unknown>).WebSocket = FakeWebSocket;
  });

  afterEach(() => {
    jest.useRealTimers();
    delete (global as Record<string, unknown>).WebSocket;
  });

  /** 站点信息取回（或失败回退）后才会建连：先冲刷微任务再操作伪 socket。 */
  async function settle() {
    await act(async () => {
      await Promise.resolve();
    });
  }

  function connect() {
    const socket = sockets[sockets.length - 1];
    act(() => socket.onopen?.());
    return socket;
  }

  it("connects, drops stale messages, and keeps newer frames", async () => {
    const received: TaskUpdateFrame[] = [];
    const { result } = renderHook(() =>
      useTaskWebSocket((frame: TaskUpdateFrame) => received.push(frame)),
    );
    await settle();
    expect(result.current.connected).toBe(false);
    const socket = connect();
    expect(result.current.connected).toBe(true);

    act(() => socket.onmessage?.({ data: frame(1, 100) }));
    act(() => socket.onmessage?.({ data: frame(1, 50) })); // 过期丢弃
    act(() => socket.onmessage?.({ data: frame(1, 100) })); // 重复丢弃
    act(() => socket.onmessage?.({ data: frame(2, 300) }));
    act(() => socket.onmessage?.({ data: "{bad json" })); // 坏消息忽略
    act(() => socket.onmessage?.({ data: JSON.stringify({ type: "other" }) })); // 非 task_update 忽略
    expect(received).toHaveLength(2);
  });

  it("reconnects with exponential backoff after abnormal close", async () => {
    renderHook(() => useTaskWebSocket(() => undefined));
    await settle();
    connect();
    act(() => sockets[0].onclose?.({ code: 1006 }));
    expect(sockets).toHaveLength(1);
    act(() => jest.advanceTimersByTime(3000));
    expect(sockets).toHaveLength(2);
  });

  it("does not reconnect on 4401 and calls onUnauthorized", async () => {
    const onUnauthorized = jest.fn();
    renderHook(() =>
      useTaskWebSocket(() => undefined, { onUnauthorized }),
    );
    await settle();
    connect();
    act(() => sockets[0].onclose?.({ code: 4401 }));
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    act(() => jest.advanceTimersByTime(60000));
    expect(sockets).toHaveLength(1);
  });

  it("polls every 5s while document hidden and stops on visible", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();
    act(() => {
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => jest.advanceTimersByTime(10000));
    expect(pollFallback.mock.calls.length).toBeGreaterThanOrEqual(2);
    act(() => {
      Object.defineProperty(document, "hidden", { value: false, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const count = pollFallback.mock.calls.length;
    act(() => jest.advanceTimersByTime(10000));
    expect(pollFallback.mock.calls.length).toBe(count);
  });

  it("calls pollFallback once after reconnect", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(1);
    act(() => sockets[0].onclose?.({ code: 1006 }));
    act(() => jest.advanceTimersByTime(3000));
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(2);
  });

  it("computes reconnect backoff from site-info ws_reconnect settings", async () => {
    const siteInfoFetch = jest.fn(async () => ({
      status: 200,
      ok: true,
      json: async () => ({
        site_title: "Aria2Deck",
        ws_reconnect_max_delay_seconds: 10,
        ws_reconnect_jitter_seconds: 0,
        ws_reconnect_factor: 5,
      }),
    }));
    (global as Record<string, unknown>).fetch = siteInfoFetch;
    try {
      renderHook(() => useTaskWebSocket(() => undefined));
      await settle();
      expect(siteInfoFetch).toHaveBeenCalledTimes(1); // 只在初始化取一次。
      connect();
      act(() => sockets[0].onclose?.({ code: 1006 }));

      act(() => jest.advanceTimersByTime(1999));
      expect(sockets).toHaveLength(1); // 2000ms 基础延迟未到。
      act(() => jest.advanceTimersByTime(1));
      expect(sockets).toHaveLength(2);

      // 第二次失败不触发 onopen（attempt 保持为 1）：退避 2000 * 5 = 10000ms。
      act(() => sockets[1].onclose?.({ code: 1006 }));
      act(() => jest.advanceTimersByTime(9999));
      expect(sockets).toHaveLength(2);
      act(() => jest.advanceTimersByTime(1));
      expect(sockets).toHaveLength(3);
      expect(siteInfoFetch).toHaveBeenCalledTimes(1); // 重连不再请求站点信息。
    } finally {
      delete (global as Record<string, unknown>).fetch;
    }
  });

  it("falls back to default policy when site info is unavailable", async () => {
    const siteInfoFetch = jest.fn(async () => ({
      status: 500,
      ok: false,
      json: async () => ({ error: { code: "internal", message: "服务器错误" } }),
    }));
    (global as Record<string, unknown>).fetch = siteInfoFetch;
    try {
      renderHook(() => useTaskWebSocket(() => undefined));
      await settle();
      connect();
      act(() => sockets[0].onclose?.({ code: 1006 }));
      act(() => jest.advanceTimersByTime(3000)); // 默认策略：2000ms + 最多 1000ms jitter。
      expect(sockets).toHaveLength(2);
    } finally {
      delete (global as Record<string, unknown>).fetch;
    }
  });
});
