import { act, render } from "@testing-library/react";
import { useTaskWebSocket } from "@/hooks/useTaskWebSocket";
import { taskWsUrl } from "@/lib/api";
import type { Task } from "@/types";

jest.mock("@/lib/api", () => ({
  taskWsUrl: jest.fn(() => "ws://localhost/ws/tasks"),
}));

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  static instances: MockWebSocket[] = [];

  readyState = MockWebSocket.CONNECTING;
  url: string;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  send = jest.fn();

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close = jest.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  });

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  emitMessage(data: unknown) {
    this.onmessage?.({ data } as MessageEvent);
  }

  emitClose() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  }
}

function HookHarness({
  onTaskUpdate,
  onNotification,
  onConnected,
  onDisconnected,
}: {
  onTaskUpdate: (task: Task) => void;
  onNotification: (message: string, level: "info" | "warning" | "error") => void;
  onConnected?: () => void;
  onDisconnected?: () => void;
}) {
  useTaskWebSocket({ onTaskUpdate, onNotification, onConnected, onDisconnected });
  return null;
}

describe("useTaskWebSocket", () => {
  const originalWebSocket = global.WebSocket;
  const randomSpy = jest.spyOn(Math, "random");

  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    randomSpy.mockReturnValue(0);
    MockWebSocket.instances = [];
    Object.defineProperty(global, "WebSocket", {
      value: MockWebSocket,
      writable: true,
    });
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  afterAll(() => {
    Object.defineProperty(global, "WebSocket", {
      value: originalWebSocket,
      writable: true,
    });
    randomSpy.mockRestore();
  });

  it("connects, handles task updates and notifications", () => {
    const onTaskUpdate = jest.fn();
    const onNotification = jest.fn();
    const onConnected = jest.fn();

    render(
      <HookHarness
        onTaskUpdate={onTaskUpdate}
        onNotification={onNotification}
        onConnected={onConnected}
      />
    );

    expect(taskWsUrl).toHaveBeenCalled();
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toBe("ws://localhost/ws/tasks");

    act(() => {
      ws.open();
    });

    expect(onConnected).toHaveBeenCalled();

    const task: Task = {
      id: 100,
      name: "demo",
      uri: "magnet:?xt=urn:btih:demo",
      status: "active",
      total_length: 1000,
      completed_length: 500,
      download_speed: 1024,
      upload_speed: 0,
      frozen_space: 1000,
      error: null,
      created_at: new Date().toISOString(),
    };

    act(() => {
      ws.emitMessage(JSON.stringify({ type: "task_update", task }));
      ws.emitMessage(
        JSON.stringify({ type: "notification", message: "hello", level: "warning" })
      );
    });

    expect(onTaskUpdate).toHaveBeenCalledWith(task);
    expect(onNotification).toHaveBeenCalledWith("hello", "warning");
  });

  it("ignores malformed websocket payloads with warning logs", () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    const onTaskUpdate = jest.fn();
    const onNotification = jest.fn();

    render(<HookHarness onTaskUpdate={onTaskUpdate} onNotification={onNotification} />);
    const ws = MockWebSocket.instances[0];

    act(() => {
      ws.open();
      ws.emitMessage("{");
      ws.emitMessage(JSON.stringify({ type: "task_update", task: { id: 1 } }));
      ws.emitMessage(JSON.stringify({ type: "notification", level: "error" }));
      ws.emitMessage(JSON.stringify({ type: "unknown" }));
      ws.emitMessage(123 as unknown as string);
    });

    expect(onTaskUpdate).not.toHaveBeenCalled();
    expect(onNotification).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("ignores legacy json ping payloads without notifying consumers", () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    const onTaskUpdate = jest.fn();
    const onNotification = jest.fn();

    render(<HookHarness onTaskUpdate={onTaskUpdate} onNotification={onNotification} />);
    const ws = MockWebSocket.instances[0];

    act(() => {
      ws.open();
      ws.emitMessage(JSON.stringify({ type: "ping" }));
    });

    expect(onTaskUpdate).not.toHaveBeenCalled();
    expect(onNotification).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("reconnects with backoff and triggers disconnection callback", () => {
    const onDisconnected = jest.fn();

    render(
      <HookHarness
        onTaskUpdate={jest.fn()}
        onNotification={jest.fn()}
        onDisconnected={onDisconnected}
      />
    );

    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.open();
      ws.emitClose();
    });

    expect(onDisconnected).toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(2100);
    });

    expect(MockWebSocket.instances.length).toBe(2);
  });
});
