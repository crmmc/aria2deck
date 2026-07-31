import { ApiError, authEvents, api, taskWsUrl } from "@/lib/api";

// Mock hashPassword
jest.mock("@/lib/crypto", () => ({
  hashPassword: jest.fn().mockResolvedValue("hashed_password"),
}));

// Store original fetch
const originalFetch = global.fetch;

describe("ApiError", () => {
  it("creates error with status", () => {
    const error = new ApiError("test error", 404);
    expect(error.message).toBe("test error");
    expect(error.status).toBe(404);
    expect(error.isUnauthorized).toBe(false);
    expect(error.isNetworkError).toBe(false);
    expect(error.name).toBe("ApiError");
  });

  it("creates unauthorized error", () => {
    const error = new ApiError("unauthorized", 401, true);
    expect(error.isUnauthorized).toBe(true);
    expect(error.isNetworkError).toBe(false);
  });

  it("creates network error", () => {
    const error = new ApiError("network error", 0, false, true);
    expect(error.isNetworkError).toBe(true);
    expect(error.isUnauthorized).toBe(false);
  });
});

describe("authEvents", () => {
  beforeEach(() => {
    authEvents.listeners.clear();
  });

  it("registers callback with onUnauthorized", () => {
    const callback = jest.fn();
    authEvents.onUnauthorized(callback);
    expect(authEvents.listeners.size).toBe(1);
  });

  it("emit calls all registered callbacks", () => {
    const callback1 = jest.fn();
    const callback2 = jest.fn();
    authEvents.onUnauthorized(callback1);
    authEvents.onUnauthorized(callback2);
    
    authEvents.emit();
    
    expect(callback1).toHaveBeenCalledTimes(1);
    expect(callback2).toHaveBeenCalledTimes(1);
  });

  it("unsubscribe removes callback", () => {
    const callback = jest.fn();
    const unsubscribe = authEvents.onUnauthorized(callback);
    
    unsubscribe();
    authEvents.emit();
    
    expect(callback).not.toHaveBeenCalled();
  });
});

describe("api methods", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    authEvents.listeners.clear();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  describe("api.me", () => {
    it("fetches current user", async () => {
      const mockUser = { id: 1, username: "test", is_admin: false };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUser),
      });

      const result = await api.me();
      
      expect(result).toEqual(mockUser);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/auth/me"),
        expect.objectContaining({
          credentials: "include",
        })
      );
    });

    it("throws ApiError on 401", async () => {
      const callback = jest.fn();
      authEvents.onUnauthorized(callback);
      
      global.fetch = jest.fn().mockResolvedValue({
        ok: false,
        status: 401,
        text: () => Promise.resolve("Unauthorized"),
      });

      await expect(api.me()).rejects.toThrow(ApiError);
      expect(callback).toHaveBeenCalled();
    });

    it("throws ApiError on network error", async () => {
      global.fetch = jest.fn().mockRejectedValue(new Error("Network error"));

      await expect(api.me()).rejects.toThrow(ApiError);
      try {
        await api.me();
      } catch (e) {
        expect((e as ApiError).isNetworkError).toBe(true);
      }
    });

    it("throws ApiError on other errors", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: () => Promise.resolve("Server error"),
      });

      await expect(api.me()).rejects.toThrow(ApiError);
      try {
        await api.me();
      } catch (e) {
        expect((e as ApiError).status).toBe(500);
      }
    });
  });

  describe("api.login", () => {
    it("calls hashPassword and sends request", async () => {
      const mockUser = { id: 1, username: "test", is_admin: false };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUser),
      });

      const result = await api.login("testuser", "password123");
      
      expect(result).toEqual(mockUser);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/auth/login"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining("hashed_password"),
        })
      );
    });
  });

  describe("api.logout", () => {
    it("sends logout request", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.logout();
      
      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/auth/logout"),
        expect.objectContaining({
          method: "POST",
        })
      );
    });
  });

  describe("api.listTasks", () => {
    it("fetches tasks without filter", async () => {
      const mockTasks = [{ id: 1, name: "task1" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTasks),
      });

      const result = await api.listTasks();
      
      expect(result).toEqual(mockTasks);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks"),
        expect.any(Object)
      );
    });

    it("fetches tasks with status filter", async () => {
      const mockTasks = [{ id: 1, name: "task1" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTasks),
      });

      await api.listTasks("active");
      
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks?status_filter=active"),
        expect.any(Object)
      );
    });
  });

  describe("api.createTask", () => {
    it("creates task with uri", async () => {
      const mockTask = { id: 1, uri: "magnet:?xt=..." };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTask),
      });

      const result = await api.createTask("magnet:?xt=...");
      
      expect(result).toEqual(mockTask);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ uri: "magnet:?xt=..." }),
        })
      );
    });
  });

  describe("api.cancelTask", () => {
    it("cancels task by subscription id", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.cancelTask(123);
      
      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/123"),
        expect.objectContaining({
          method: "DELETE",
        })
      );
    });
  });

  describe("api.getStats", () => {
    it("fetches system stats", async () => {
      const mockStats = { used: 1000, total: 10000 };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockStats),
      });

      const result = await api.getStats();
      
      expect(result).toEqual(mockStats);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/stats"),
        expect.any(Object)
      );
    });
  });

  describe("api.listFiles", () => {
    it("fetches file list", async () => {
      const mockFiles = { files: [], space: {} };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockFiles),
      });

      const result = await api.listFiles();
      
      expect(result).toEqual(mockFiles);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files"),
        expect.any(Object)
      );
    });
  });

  describe("api.deleteFile", () => {
    it("deletes file by id", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.deleteFile("abc123hash");

      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/abc123hash"),
        expect.objectContaining({
          method: "DELETE",
        })
      );
    });
  });

  describe("api.downloadFileUrl", () => {
    it("returns download URL without path", () => {
      const url = api.downloadFileUrl("abc123hash");
      expect(url).toContain("/api/files/abc123hash/download");
      expect(url).not.toContain("?path=");
    });

    it("returns download URL with path", () => {
      const url = api.downloadFileUrl("abc123hash", "subdir/file.txt");
      expect(url).toContain("/api/files/abc123hash/download");
      expect(url).toContain("?path=");
      expect(url).toContain(encodeURIComponent("subdir/file.txt"));
    });
  });

  describe("api.changePassword", () => {
    it("sends hashed passwords", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true, message: "success" }),
      });

      const result = await api.changePassword("oldpass", "newpass", "testuser");
      
      expect(result).toEqual({ ok: true, message: "success" });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/auth/change-password"),
        expect.objectContaining({
          method: "POST",
        })
      );
    });
  });

  describe("api.listStoredFiles", () => {
    it("sends the pagination and filter contract", async () => {
      const response = {
        files: [],
        total: 0,
        page: 2,
        page_size: 50,
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(response),
      });

      await expect(api.listStoredFiles(2, 50, "movie", true)).resolves.toEqual(response);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining(
          "/api/admin/storage/files?page=2&page_size=50&search=movie&orphan_only=true"
        ),
        expect.objectContaining({ credentials: "include" })
      );
    });
  });

  describe("api.previewTorrent", () => {
    it("previews torrent metadata", async () => {
      const preview = {
        info_hash: "abc",
        name: "root",
        file_count: 1,
        total_size: 10,
        files: [{ index: 1, path: ["root"], size: 10 }],
        tree: [{ type: "file", name: "root", path: ["root"], index: 1, size: 10 }],
        limits: { max_files: 5000 },
        default_selection: "all",
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(preview),
      });

      const result = await api.previewTorrent("base64data");

      expect(result).toEqual(preview);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/torrent/preview"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ torrent: "base64data" }),
        })
      );
    });
  });

  describe("api.uploadTorrent", () => {
    it("uploads torrent with selected indexes and options", async () => {
      const mockTask = { id: 1, name: "test.torrent" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTask),
      });

      const result = await api.uploadTorrent("base64data", {
        selected_file_indexes: [1, 3],
        options: { "bt-tracker": "http://tracker.example.com/announce" },
      });

      expect(result).toEqual(mockTask);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/torrent"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            torrent: "base64data",
            selected_file_indexes: [1, 3],
            options: { "bt-tracker": "http://tracker.example.com/announce" },
          }),
        })
      );
    });

    it("preserves old uploadTorrent callers without a second argument", async () => {
      const mockTask = { id: 1, name: "test.torrent" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTask),
      });

      await api.uploadTorrent("base64data");

      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/torrent"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ torrent: "base64data" }),
        })
      );
    });
  });

  describe("api.listHistory", () => {
    it("fetches task history", async () => {
      const mockHistory = [{ id: 1, name: "task1" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockHistory),
      });

      const result = await api.listHistory();
      
      expect(result).toEqual(mockHistory);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/history"),
        expect.any(Object)
      );
    });
  });

  describe("api.deleteHistory", () => {
    it("deletes history item", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.deleteHistory(123);
      
      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/history/123"),
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });

  describe("api.clearHistory", () => {
    it("clears all history", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true, count: 5 }),
      });

      const result = await api.clearHistory();
      
      expect(result).toEqual({ ok: true, count: 5 });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/history"),
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });

  describe("api.getMachineStats", () => {
    it("fetches machine stats", async () => {
      const mockStats = { disk_total: 1000, disk_free: 500 };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockStats),
      });

      const result = await api.getMachineStats();
      
      expect(result).toEqual(mockStats);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/stats/machine"),
        expect.any(Object)
      );
    });
  });

  describe("api.getConfig", () => {
    it("fetches system config", async () => {
      const mockConfig = { max_task_size: 1000 };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockConfig),
      });

      const result = await api.getConfig();
      
      expect(result).toEqual(mockConfig);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/config"),
        expect.any(Object)
      );
    });
  });

  describe("api.updateConfig", () => {
    it("updates system config", async () => {
      const mockConfig = { max_task_size: 2000 };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockConfig),
      });

      const result = await api.updateConfig({ max_task_size: 2000 });
      
      expect(result).toEqual(mockConfig);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/config"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ max_task_size: 2000 }),
        })
      );
    });
  });

  describe("api.getAria2Version", () => {
    it("fetches aria2 version", async () => {
      const mockVersion = { connected: true, version: "1.36.0" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockVersion),
      });

      const result = await api.getAria2Version();
      
      expect(result).toEqual(mockVersion);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/config/aria2/version"),
        expect.any(Object)
      );
    });
  });

  describe("api.testAria2Connection", () => {
    it("tests aria2 connection", async () => {
      const mockResult = { connected: true, version: "1.36.0" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResult),
      });

      const result = await api.testAria2Connection("http://localhost:6800/jsonrpc", "secret");
      
      expect(result).toEqual(mockResult);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/config/aria2/test"),
        expect.objectContaining({
          method: "POST",
        })
      );
    });
  });

  describe("api.getRpcAccess", () => {
    it("fetches RPC access status", async () => {
      const mockStatus = { enabled: true, secret: "abc123" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockStatus),
      });

      const result = await api.getRpcAccess();
      
      expect(result).toEqual(mockStatus);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/me/rpc-access"),
        expect.any(Object)
      );
    });
  });

  describe("api.setRpcAccess", () => {
    it("sets RPC access", async () => {
      const mockStatus = { enabled: true };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockStatus),
      });

      const result = await api.setRpcAccess(true);
      
      expect(result).toEqual(mockStatus);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/me/rpc-access"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ enabled: true }),
        })
      );
    });
  });

  describe("api.refreshRpcSecret", () => {
    it("refreshes RPC secret", async () => {
      const mockStatus = { enabled: true, secret: "newsecret" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockStatus),
      });

      const result = await api.refreshRpcSecret();
      
      expect(result).toEqual(mockStatus);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/me/rpc-access/refresh"),
        expect.objectContaining({ method: "POST" })
      );
    });
  });

  describe("api.listUsers", () => {
    it("fetches user list", async () => {
      const mockUsers = [{ id: 1, username: "admin" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUsers),
      });

      const result = await api.listUsers();
      
      expect(result).toEqual(mockUsers);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users"),
        expect.any(Object)
      );
    });
  });

  describe("api.createUser", () => {
    it("creates user with hashed password", async () => {
      const mockUser = { id: 2, username: "newuser" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUser),
      });

      const result = await api.createUser({ username: "newuser", password: "pass123" });
      
      expect(result).toEqual(mockUser);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining("hashed_password"),
        })
      );
    });
  });

  describe("api.updateUser", () => {
    it("updates user without password", async () => {
      const mockUser = { id: 1, username: "updated" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUser),
      });

      const result = await api.updateUser(1, { username: "updated" }, "oldname");
      
      expect(result).toEqual(mockUser);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/1"),
        expect.objectContaining({ method: "PUT" })
      );
    });

    it("updates user with password", async () => {
      const mockUser = { id: 1, username: "user" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockUser),
      });

      const result = await api.updateUser(1, { password: "newpass" }, "user");
      
      expect(result).toEqual(mockUser);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/1"),
        expect.objectContaining({
          method: "PUT",
          body: expect.stringContaining("hashed_password"),
        })
      );
    });
  });

  describe("api.deleteUser", () => {
    it("deletes user", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.deleteUser(123);

      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/users/123"),
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });

  describe("api.browseFile", () => {
    it("browses file without path", async () => {
      const mockFiles = [{ name: "file1.txt" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockFiles),
      });

      const result = await api.browseFile("abc123hash");

      expect(result).toEqual(mockFiles);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/abc123hash/browse"),
        expect.any(Object)
      );
    });

    it("browses file with path", async () => {
      const mockFiles = [{ name: "subfile.txt" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockFiles),
      });

      const result = await api.browseFile("abc123hash", "subdir");

      expect(result).toEqual(mockFiles);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/abc123hash/browse?path=subdir"),
        expect.any(Object)
      );
    });
  });

  describe("api.renameFile", () => {
    it("renames file", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });

      const result = await api.renameFile("abc123hash", "newname.txt");

      expect(result).toEqual({ ok: true });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/abc123hash/rename"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ name: "newname.txt" }),
        })
      );
    });
  });

  describe("api.listPackTasks", () => {
    it("fetches pack tasks", async () => {
      const mockTasks = [{ id: 1, status: "completed" }];
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTasks),
      });

      const result = await api.listPackTasks();
      
      expect(result).toEqual(mockTasks);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/pack"),
        expect.any(Object)
      );
    });
  });

  describe("api.cancelPackTask", () => {
    it("cancels pack task", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ ok: true, message: "cancelled" }),
      });

      const result = await api.cancelPackTask(123);
      
      expect(result).toEqual({ ok: true, message: "cancelled" });
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files/pack/123"),
        expect.objectContaining({ method: "DELETE" })
      );
    });
  });
});

describe("taskWsUrl", () => {
  const originalEnv = process.env.NEXT_PUBLIC_API_BASE;

  afterEach(() => {
    if (originalEnv !== undefined) {
      process.env.NEXT_PUBLIC_API_BASE = originalEnv;
    } else {
      delete process.env.NEXT_PUBLIC_API_BASE;
    }
  });

  it("converts http to ws", () => {
    process.env.NEXT_PUBLIC_API_BASE = "http://localhost:8001";
    const url = taskWsUrl();
    expect(url).toBe("ws://localhost:8001/ws/tasks");
  });

  it("converts https to wss", () => {
    process.env.NEXT_PUBLIC_API_BASE = "https://example.com";
    const url = taskWsUrl();
    expect(url).toBe("wss://example.com/ws/tasks");
  });

  it("uses window.location.origin when no env var", () => {
    delete process.env.NEXT_PUBLIC_API_BASE;
    const url = taskWsUrl();
    expect(url).toContain("/ws/tasks");
    expect(url).toMatch(/^wss?:\/\//);
  });
});
