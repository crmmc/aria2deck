import { act, renderHook } from "@testing-library/react";

import { useTaskWebSocket, type TaskUpdateFrame } from "@/shared/hooks/useTaskWebSocket";

type SocketState = {
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
  readyState: number;
  send: jest.Mock;
  close: jest.Mock;
};

let sockets: SocketState[] = [];

class FakeWebSocket {
  static OPEN = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  send = jest.fn();
  close = jest.fn(() => {
    this.readyState = 3;
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
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
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
    socket.readyState = FakeWebSocket.OPEN;
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

  it("does not reconnect or keep timers on 4401 and calls onUnauthorized", async () => {
    const onUnauthorized = jest.fn();
    const pollFallback = jest.fn();
    renderHook(() =>
      useTaskWebSocket(() => undefined, { onUnauthorized, pollFallback }),
    );
    await settle();
    const socket = connect();
    act(() => socket.onclose?.({ code: 4401 }));
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    act(() => jest.advanceTimersByTime(60000));
    expect(sockets).toHaveLength(1);
    expect(pollFallback).toHaveBeenCalledTimes(1);
    expect(socket.send).not.toHaveBeenCalled();
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

  it("reloads on open and immediately after disconnect", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(1);
    act(() => sockets[0].onclose?.({ code: 1006 }));
    act(() => jest.advanceTimersByTime(3000));
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(3);
  });

  it("keeps reconnecting and polling after a synchronous fallback failure", async () => {
    const pollFallback = jest
      .fn()
      .mockImplementationOnce(() => undefined)
      .mockImplementationOnce(() => {
        throw new Error("sync failure");
      })
      .mockImplementation(() => undefined);
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();

    act(() => sockets[0].onclose?.({ code: 1006 }));
    expect(pollFallback).toHaveBeenCalledTimes(2);
    act(() => jest.advanceTimersByTime(5000));
    expect(sockets).toHaveLength(2);
    expect(pollFallback).toHaveBeenCalledTimes(3);
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

  it("resynchronizes after 4408 and stops disconnected polling after reopening", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(1);

    act(() => sockets[0].onclose?.({ code: 4408 }));
    expect(pollFallback).toHaveBeenCalledTimes(2);
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).toHaveBeenCalledTimes(3);

    connect();
    expect(pollFallback).toHaveBeenCalledTimes(4);
    act(() => jest.advanceTimersByTime(10000));
    expect(pollFallback).toHaveBeenCalledTimes(4);
  });

  it("keeps disconnected polling when visibility returns during reconnect", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).not.toHaveBeenCalled(); // 初始 CONNECTING 不属于断线补偿。

    connect();
    act(() => sockets[0].onclose?.({ code: 1006 }));
    expect(pollFallback).toHaveBeenCalledTimes(2);
    act(() => {
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => jest.advanceTimersByTime(3000));
    expect(sockets).toHaveLength(2); // 重连已创建，但尚未 onopen。

    act(() => {
      Object.defineProperty(document, "hidden", { value: false, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => jest.advanceTimersByTime(2000));
    expect(pollFallback).toHaveBeenCalledTimes(3);
  });

  it("does not overlap fallback requests and resumes after the previous one settles", async () => {
    const resolvers: (() => void)[] = [];
    const pollFallback = jest.fn(
      () => new Promise<void>((resolve) => resolvers.push(resolve)),
    );
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(1);

    act(() => sockets[0].onclose?.({ code: 4408 }));
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvers[0]();
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).toHaveBeenCalledTimes(2);
    await act(async () => {
      resolvers[1]();
      await Promise.resolve();
    });
  });

  it.each(["resolve", "reject"])(
    "starts a fresh resync after reopening when the in-flight request settles by %s",
    async (outcome) => {
      const pending: { resolve: () => void; reject: (reason: Error) => void }[] = [];
      const pollFallback = jest
        .fn()
        .mockImplementationOnce(() => undefined)
        .mockImplementation(
          () =>
            new Promise<void>((resolve, reject) => {
              pending.push({ resolve, reject });
            }),
        );
      renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
      await settle();
      connect();
      expect(pollFallback).toHaveBeenCalledTimes(1);

      act(() => sockets[0].onclose?.({ code: 4408 }));
      expect(pollFallback).toHaveBeenCalledTimes(2);
      act(() => jest.advanceTimersByTime(3000));
      connect();
      expect(pollFallback).toHaveBeenCalledTimes(2);

      await act(async () => {
        if (outcome === "resolve") pending[0].resolve();
        else pending[0].reject(new Error("stale request failed"));
        await Promise.resolve();
      });
      expect(pollFallback).toHaveBeenCalledTimes(3);

      await act(async () => {
        pending[1].resolve();
        await Promise.resolve();
      });
      act(() => jest.advanceTimersByTime(10000));
      expect(pollFallback).toHaveBeenCalledTimes(3);
    },
  );

  it("keeps disconnected polling after a reopened resync rejects until a later poll succeeds", async () => {
    const pending: { resolve: () => void; reject: (reason: Error) => void }[] = [];
    const pollFallback = jest
      .fn()
      .mockImplementationOnce(() => undefined)
      .mockImplementationOnce(() => undefined)
      .mockImplementation(
        () =>
          new Promise<void>((resolve, reject) => {
            pending.push({ resolve, reject });
          }),
      );
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();

    act(() => sockets[0].onclose?.({ code: 1006 }));
    act(() => jest.advanceTimersByTime(3000));
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(3);

    await act(async () => {
      pending[0].reject(new Error("reconnect resync failed"));
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).toHaveBeenCalledTimes(4);

    await act(async () => {
      pending[1].resolve();
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(10000));
    expect(pollFallback).toHaveBeenCalledTimes(4);
  });

  it("keeps reopened resync polling after false and stops after true", async () => {
    const pollFallback = jest
      .fn()
      .mockImplementationOnce(() => undefined)
      .mockImplementationOnce(() => undefined)
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    connect();

    act(() => sockets[0].onclose?.({ code: 1006 }));
    act(() => jest.advanceTimersByTime(3000));
    connect();
    expect(pollFallback).toHaveBeenCalledTimes(3);

    await act(async () => {
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(5000));
    expect(pollFallback).toHaveBeenCalledTimes(4);

    await act(async () => {
      await Promise.resolve();
    });
    act(() => jest.advanceTimersByTime(10000));
    expect(pollFallback).toHaveBeenCalledTimes(4);
  });

  it("sends ping every 15s and stays open when pong responses arrive", async () => {
    const received: TaskUpdateFrame[] = [];
    renderHook(() =>
      useTaskWebSocket((frame: TaskUpdateFrame) => received.push(frame)),
    );
    await settle();
    const socket = connect();

    for (let count = 1; count <= 3; count += 1) {
      act(() => jest.advanceTimersByTime(15000));
      expect(socket.send).toHaveBeenCalledTimes(count);
      expect(socket.send).toHaveBeenLastCalledWith("ping");
      act(() => socket.onmessage?.({ data: "pong" }));
    }
    expect(socket.close).not.toHaveBeenCalled();
    expect(received).toHaveLength(0);
  });

  it("closes after 45s without pong and uses the same recovery path", async () => {
    const pollFallback = jest.fn();
    renderHook(() => useTaskWebSocket(() => undefined, { pollFallback }));
    await settle();
    const socket = connect();

    act(() => jest.advanceTimersByTime(45000));
    expect(socket.send).toHaveBeenCalledTimes(3);
    expect(socket.close).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(15000));
    expect(socket.close).toHaveBeenCalledTimes(1);
    expect(pollFallback).toHaveBeenCalledTimes(2);
    act(() => jest.advanceTimersByTime(3000));
    expect(sockets).toHaveLength(2);
  });

  it("clears heartbeat, polling, and reconnect timers on unmount", async () => {
    const pollFallback = jest.fn();
    const { unmount } = renderHook(() =>
      useTaskWebSocket(() => undefined, { pollFallback }),
    );
    await settle();
    const socket = connect();
    act(() => {
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    unmount();

    act(() => jest.advanceTimersByTime(60000));
    expect(socket.send).not.toHaveBeenCalled();
    expect(pollFallback).toHaveBeenCalledTimes(1);
    expect(sockets).toHaveLength(1);
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
