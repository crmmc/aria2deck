/**
 * Mock utilities for API and browser APIs
 */

import type {
  User,
  Task,
  SystemStats,
  SystemConfig,
  FileListResponse,
  FileInfo,
  BrowseFileInfo,
  MachineStats,
  RpcAccessStatus,
  TaskHistory,
  SpaceInfo,
} from "@/types";

// ============================================
// Mock Data Factories
// ============================================

export const createMockUser = (overrides: Partial<User> = {}): User => ({
  id: 1,
  username: "testuser",
  is_admin: false,
  quota: 107374182400, // 100GB
  is_initial_password: false,
  ...overrides,
});

export const createMockTask = (overrides: Partial<Task> = {}): Task => ({
  id: 1,
  name: "test-file.zip",
  uri: "https://example.com/test-file.zip",
  status: "active",
  total_length: 1073741824, // 1GB
  completed_length: 536870912, // 512MB
  download_speed: 1048576, // 1MB/s
  upload_speed: 0,
  frozen_space: 1073741824,
  error: null,
  created_at: new Date().toISOString(),
  ...overrides,
});

export const createMockFileInfo = (overrides: Partial<FileInfo> = {}): FileInfo => ({
  id: 1,
  content_hash: "hash_test_file",
  name: "test-file.zip",
  size: 1073741824,
  is_directory: false,
  created_at: new Date().toISOString(),
  ...overrides,
});

export const createMockSpaceInfo = (overrides: Partial<SpaceInfo> = {}): SpaceInfo => ({
  used: 5368709120, // 5GB
  frozen: 1073741824, // 1GB
  available: 101005473792, // ~94GB
  ...overrides,
});

export const createMockSystemStats = (overrides: Partial<SystemStats> = {}): SystemStats => ({
  download_speed: 1048576,
  upload_speed: 524288,
  active_task_count: 2,
  disk_used_space: 53687091200,
  disk_frozen_space: 0,
  disk_total_space: 107374182400,
  disk_space_limited: false,
  ...overrides,
});

export const createMockSystemConfig = (overrides: Partial<SystemConfig> = {}): SystemConfig => ({
  max_task_size: 10737418240,
  min_free_disk: 1073741824,
  aria2_rpc_url: "http://localhost:6800/jsonrpc",
  aria2_rpc_secret: "secret",
  hidden_file_extensions: [".aria2", ".torrent"],
  pack_format: "zip",
  pack_compression_level: 5,
  ws_reconnect_max_delay: 30000,
  ws_reconnect_jitter: 0.3,
  ws_reconnect_factor: 2,
  site_title: "Aria2 控制器",
  rate_limit_account_security: 5,
  rate_limit_authenticated_api: 60,
  rate_limit_public_api: 60,
  rate_limit_share_access: 5,
  rate_limit_authenticated_download: 300,
  rate_limit_anonymous_download: 60,
  rate_limit_create_task: 30,
  rate_limit_create_torrent: 20,
  rate_limit_create_pack: 5,
  rate_limit_aria2_test: 10,
  rate_limit_rpc: 300,
  download_total_connections: 100,
  download_authenticated_reserved_connections: 60,
  download_authenticated_per_user_connections: 16,
  download_authenticated_per_file_connections: 8,
  download_anonymous_base_connections: 20,
  download_anonymous_borrow_connections: 20,
  download_anonymous_per_ip_connections: 4,
  download_anonymous_per_file_connections: 2,
  ...overrides,
});

export const createMockTaskHistory = (overrides: Partial<TaskHistory> = {}): TaskHistory => ({
  id: 1,
  task_name: "completed-file.zip",
  uri: "https://example.com/completed-file.zip",
  total_length: 1073741824,
  result: "completed",
  reason: null,
  created_at: new Date().toISOString(),
  finished_at: new Date().toISOString(),
  ...overrides,
});

export const createMockRpcAccessStatus = (overrides: Partial<RpcAccessStatus> = {}): RpcAccessStatus => ({
  enabled: false,
  secret: null,
  created_at: null,
  ...overrides,
});

// ============================================
// Mock API Module
// ============================================

export const mockApi = {
  // Auth
  login: jest.fn().mockResolvedValue(createMockUser()),
  logout: jest.fn().mockResolvedValue({ ok: true }),
  me: jest.fn().mockResolvedValue(createMockUser()),
  changePassword: jest.fn().mockResolvedValue({ ok: true, message: "Password changed" }),

  // Tasks
  listTasks: jest.fn().mockResolvedValue([createMockTask()]),
  createTask: jest.fn().mockResolvedValue(createMockTask()),
  uploadTorrent: jest.fn().mockResolvedValue(createMockTask()),
  cancelTask: jest.fn().mockResolvedValue({ ok: true }),

  // History
  listHistory: jest.fn().mockResolvedValue([createMockTaskHistory()]),
  deleteHistory: jest.fn().mockResolvedValue({ ok: true }),
  clearHistory: jest.fn().mockResolvedValue({ ok: true, count: 5 }),

  // Stats & Config
  getStats: jest.fn().mockResolvedValue(createMockSystemStats()),
  getMachineStats: jest.fn().mockResolvedValue({
    disk_total: 500000000000,
    disk_used: 250000000000,
    disk_free: 250000000000,
    download_used: 100000000000,
    system_used: 150000000000,
  } as MachineStats),
  getConfig: jest.fn().mockResolvedValue(createMockSystemConfig()),
  updateConfig: jest.fn().mockResolvedValue(createMockSystemConfig()),
  getAria2Version: jest.fn().mockResolvedValue({
    connected: true,
    version: "1.36.0",
    enabled_features: ["BitTorrent", "GZip"],
  }),
  testAria2Connection: jest.fn().mockResolvedValue({
    connected: true,
    version: "1.36.0",
  }),

  // RPC Access
  getRpcAccess: jest.fn().mockResolvedValue(createMockRpcAccessStatus()),
  setRpcAccess: jest.fn().mockResolvedValue(createMockRpcAccessStatus({ enabled: true, secret: "test-secret" })),
  refreshRpcSecret: jest.fn().mockResolvedValue(createMockRpcAccessStatus({ enabled: true, secret: "new-secret" })),

  // Users (Admin)
  listUsers: jest.fn().mockResolvedValue([createMockUser(), createMockUser({ id: 2, username: "admin", is_admin: true })]),
  createUser: jest.fn().mockResolvedValue(createMockUser({ id: 3, username: "newuser" })),
  updateUser: jest.fn().mockResolvedValue(createMockUser()),
  deleteUser: jest.fn().mockResolvedValue({ ok: true }),

  // Files
  listFiles: jest.fn().mockResolvedValue({
    files: [createMockFileInfo()],
    space: createMockSpaceInfo(),
  } as FileListResponse),
  browseFile: jest.fn().mockResolvedValue([
    { name: "file1.txt", size: 1024, is_directory: false },
    { name: "folder", size: 0, is_directory: true },
  ] as BrowseFileInfo[]),
  downloadFileUrl: jest.fn().mockReturnValue("http://localhost:8000/api/files/1/download"),
  deleteFile: jest.fn().mockResolvedValue({ ok: true }),
  renameFile: jest.fn().mockResolvedValue({ ok: true }),

  // Pack Tasks
  listPackTasks: jest.fn().mockResolvedValue([]),
  cancelPackTask: jest.fn().mockResolvedValue({ ok: true, message: "Cancelled" }),
  deletePackTask: jest.fn().mockResolvedValue({ ok: true, message: "Deleted" }),
  clearPackTasks: jest.fn().mockResolvedValue({ ok: true, count: 0 }),
  downloadPackResult: jest.fn().mockReturnValue("http://localhost:8000/api/files/pack/1/download"),
};

// Mock authEvents
export const mockAuthEvents = {
  listeners: new Set<() => void>(),
  onUnauthorized: jest.fn((callback: () => void) => {
    mockAuthEvents.listeners.add(callback);
    return () => {
      mockAuthEvents.listeners.delete(callback);
    };
  }),
  emit: jest.fn(() => {
    mockAuthEvents.listeners.forEach((cb) => cb());
  }),
};

// Mock ApiError class
export class MockApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public isUnauthorized: boolean = false,
    public isNetworkError: boolean = false
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// Mock taskWsUrl
export const mockTaskWsUrl = jest.fn().mockReturnValue("ws://localhost:8000/ws/tasks");

// ============================================
// Browser API Mocks
// ============================================

// LocalStorage mock
export const createLocalStorageMock = () => {
  let store: Record<string, string> = {};
  return {
    getItem: jest.fn((key: string) => store[key] || null),
    setItem: jest.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: jest.fn((key: string) => {
      delete store[key];
    }),
    clear: jest.fn(() => {
      store = {};
    }),
    get length() {
      return Object.keys(store).length;
    },
    key: jest.fn((index: number) => Object.keys(store)[index] || null),
  };
};

// WebSocket mock
export class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  url: string;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    // Simulate connection after a tick
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN;
      if (this.onopen) {
        this.onopen(new Event("open"));
      }
    }, 0);
  }

  send = jest.fn();
  close = jest.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose) {
      this.onclose(new CloseEvent("close"));
    }
  });

  // Helper to simulate receiving a message
  simulateMessage(data: unknown) {
    if (this.onmessage) {
      this.onmessage(new MessageEvent("message", { data: JSON.stringify(data) }));
    }
  }

  // Helper to simulate an error
  simulateError() {
    if (this.onerror) {
      this.onerror(new Event("error"));
    }
  }
}

// Notification mock
export const createNotificationMock = () => {
  const mockNotification = jest.fn().mockImplementation((title: string, options?: NotificationOptions) => ({
    title,
    options,
    close: jest.fn(),
    onclick: null as ((event: Event) => void) | null,
  }));

  Object.defineProperty(mockNotification, "permission", {
    value: "default",
    writable: true,
  });

  const notificationMock = mockNotification as typeof mockNotification & {
    requestPermission: jest.Mock<Promise<NotificationPermission>, []>;
  };
  notificationMock.requestPermission = jest.fn().mockResolvedValue("granted");

  return notificationMock;
};

// Crypto.subtle mock for PBKDF2
export const createCryptoSubtleMock = () => ({
  digest: jest.fn().mockImplementation(async (_algorithm: string, data: ArrayBuffer) => {
    // Return a deterministic hash based on input length
    const result = new Uint8Array(32);
    const view = new Uint8Array(data);
    for (let i = 0; i < 32; i++) {
      result[i] = (view[i % view.length] || 0) ^ (i * 7);
    }
    return result.buffer;
  }),
  importKey: jest.fn().mockResolvedValue({ type: "secret" }),
  deriveBits: jest.fn().mockImplementation(async () => {
    // Return deterministic 256 bits
    const result = new Uint8Array(32);
    for (let i = 0; i < 32; i++) {
      result[i] = (i * 13 + 42) % 256;
    }
    return result.buffer;
  }),
});

// Clipboard mock
export const createClipboardMock = () => ({
  writeText: jest.fn().mockResolvedValue(undefined),
  readText: jest.fn().mockResolvedValue(""),
});

// ============================================
// Test Utilities
// ============================================

// Reset all mocks
export const resetAllMocks = () => {
  Object.values(mockApi).forEach((mock) => {
    if (typeof mock === "function" && "mockClear" in mock) {
      mock.mockClear();
    }
  });
  mockAuthEvents.listeners.clear();
  mockAuthEvents.onUnauthorized.mockClear();
  mockAuthEvents.emit.mockClear();
  mockTaskWsUrl.mockClear();
};

// Setup fetch mock
export const setupFetchMock = (responses: Record<string, unknown> = {}) => {
  const mockFetch = jest.fn().mockImplementation(async (url: string, _options?: RequestInit) => {
    const path = new URL(url, "http://localhost").pathname;
    const response = responses[path];

    if (response === undefined) {
      return {
        ok: false,
        status: 404,
        json: async () => ({ detail: "Not found" }),
        text: async () => "Not found",
      };
    }

    if (response instanceof Error) {
      throw response;
    }

    return {
      ok: true,
      status: 200,
      json: async () => response,
      text: async () => JSON.stringify(response),
    };
  });

  global.fetch = mockFetch;
  return mockFetch;
};
