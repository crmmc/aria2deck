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

  describe("api.createTasks", () => {
    it("sends a single POST with the tasks array body", async () => {
      const mockResponse = {
        accepted_count: 1,
        failed_count: 0,
        results: [
          {
            input_index: 0,
            accepted: true,
            task_id: 1,
            status: "queued" as const,
            error: null,
          },
        ],
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.createTasks([{ uri: "https://example.com/a" }]);

      expect(result).toEqual(mockResponse);
      expect(global.fetch).toHaveBeenCalledTimes(1);
      const [url, options] = (global.fetch as jest.Mock).mock.calls[0] as [
        string,
        RequestInit,
      ];
      expect(url.endsWith("/api/tasks")).toBe(true);
      expect(options.method).toBe("POST");
      expect(JSON.parse(options.body as string)).toEqual({
        tasks: [{ uri: "https://example.com/a" }],
      });
    });

    it("sends the whole array in one request without an abort signal", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({ accepted_count: 0, failed_count: 0, results: [] }),
      });

      const items = [
        { uri: "https://example.com/1" },
        { uri: "https://example.com/2" },
        { uri: "https://example.com/3", options: { out: "x.zip" } },
      ];
      await api.createTasks(items);

      expect(global.fetch).toHaveBeenCalledTimes(1);
      const [url, options] = (global.fetch as jest.Mock).mock.calls[0] as [
        string,
        RequestInit,
      ];
      expect(url.endsWith("/api/tasks")).toBe(true);
      expect(JSON.parse(options.body as string)).toEqual({ tasks: items });
      expect(options.signal).toBeUndefined();
    });
  });

  describe("api.retryTask", () => {
    it("posts to /api/tasks/:id/retry", async () => {
      const mockTask = { id: 42, uri: "https://example.com/file" };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockTask),
      });

      const result = await api.retryTask(7);

      expect(result).toEqual(mockTask);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/7/retry"),
        expect.objectContaining({
          method: "POST",
        })
      );
    });
  });

  describe("api.cancelTasks", () => {
    it("posts task ids to /api/tasks/cancel as a single request", async () => {
      const mockResponse = {
        accepted_count: 2,
        failed_count: 0,
        results: [
          { task_id: 1, ok: true, state: "cancelled", accepted: true, error: null },
          { task_id: 2, ok: true, state: "cancelled", accepted: true, error: null },
        ],
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.cancelTasks([1, 2]);

      expect(result).toEqual(mockResponse);
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/tasks/cancel"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ task_ids: [1, 2] }),
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

  describe("api.searchFiles", () => {
    it("sends q only when no scope is given", async () => {
      const mockResponse = { items: [], total: 0, truncated: false };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.searchFiles({ q: "女王" });

      expect(result).toEqual(mockResponse);
      const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
      expect(url).toContain("/api/files/search");
      expect(decodeURIComponent(url)).toContain("q=女王");
      expect(url).not.toContain("scope_content_hash");
      expect(url).not.toContain("scope_path");
    });

    it("sends optional scope params when provided", async () => {
      const mockResponse = { items: [], total: 0, truncated: false };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.searchFiles({
        q: "a",
        scopeContentHash: "abc",
        scopePath: "dir",
      });

      expect(result).toEqual(mockResponse);
      const url = (global.fetch as jest.Mock).mock.calls[0][0] as string;
      expect(url).toContain("/api/files/search");
      expect(url).toContain("q=a");
      expect(url).toContain("scope_content_hash=abc");
      expect(url).toContain("scope_path=dir");
    });

    it("rejects without fetching when q is empty or whitespace", async () => {
      global.fetch = jest.fn();

      await expect(api.searchFiles({ q: "" })).rejects.toThrow("请输入关键词");
      await expect(api.searchFiles({ q: "   " })).rejects.toThrow("请输入关键词");
      expect(global.fetch).not.toHaveBeenCalled();
    });
  });

  describe("api.deleteFiles", () => {
    it("sends a single batch delete request with file hashes body", async () => {
      const mockResponse = {
        accepted_count: 2,
        failed_count: 0,
        results: [
          {
            content_hash: "abc123hash",
            ok: true,
            state: "pending",
            accepted: true,
            error: null,
          },
          {
            content_hash: "def456hash",
            ok: true,
            state: "released",
            accepted: false,
            error: null,
          },
        ],
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.deleteFiles(["abc123hash", "def456hash"]);

      expect(result).toEqual(mockResponse);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/files"),
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ file_hashes: ["abc123hash", "def456hash"] }),
        })
      );
    });

    it("sends a single-element array for inline single delete", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({ accepted_count: 1, failed_count: 0, results: [] }),
      });

      await api.deleteFiles(["abc123hash"]);

      const [url, options] = (global.fetch as jest.Mock).mock.calls[0] as [
        string,
        RequestInit,
      ];
      expect(url.endsWith("/api/files")).toBe(true);
      expect(options.method).toBe("DELETE");
      expect(JSON.parse(options.body as string)).toEqual({
        file_hashes: ["abc123hash"],
      });
    });
  });

  describe("api.deleteShares", () => {
    it("sends a single batch delete request with share ids body", async () => {
      const mockResponse = {
        accepted_count: 2,
        failed_count: 0,
        results: [
          { share_id: 1, ok: true, state: "deleted", accepted: true, error: null },
          { share_id: 2, ok: true, state: "deleted", accepted: true, error: null },
        ],
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.deleteShares([1, 2]);

      expect(result).toEqual(mockResponse);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/shares"),
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ share_ids: [1, 2] }),
        })
      );
    });

    it("sends a single-element array for inline single delete", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({ accepted_count: 1, failed_count: 0, results: [] }),
      });

      await api.deleteShares([7]);

      const [url, options] = (global.fetch as jest.Mock).mock.calls[0] as [
        string,
        RequestInit,
      ];
      expect(url.endsWith("/api/shares")).toBe(true);
      expect(options.method).toBe("DELETE");
      expect(JSON.parse(options.body as string)).toEqual({ share_ids: [7] });
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

  describe("api.downloadShare", () => {
    it("submits token and subpath in a POST form without URL exposure", () => {
      let submittedForm: HTMLFormElement | undefined;
      const submitSpy = jest
        .spyOn(HTMLFormElement.prototype, "submit")
        .mockImplementation(function (this: HTMLFormElement) {
          submittedForm = this;
        });

      api.downloadShare("code/with?chars", "bearer-secret", "folder/a b.txt");

      expect(submitSpy).toHaveBeenCalledTimes(1);
      expect(submittedForm).toBeDefined();
      const form = submittedForm as HTMLFormElement;
      expect(form.method).toBe("post");
      expect(form.target).toBe("_blank");
      expect(form.action).toContain("/api/s/code%2Fwith%3Fchars/download");
      expect(form.action).not.toContain("bearer-secret");
      expect(form.action).not.toContain("token=");
      expect(form.querySelector<HTMLInputElement>('input[name="token"]')?.value).toBe(
        "bearer-secret"
      );
      expect(form.querySelector<HTMLInputElement>('input[name="subpath"]')?.value).toBe(
        "folder/a b.txt"
      );
      expect(document.body.contains(form)).toBe(false);
      submitSpy.mockRestore();
    });
  });

  describe("api.browseShare", () => {
    it("uses a bearer header and keeps the token out of the URL", async () => {
      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve([]),
      });
      global.fetch = fetchMock;

      await api.browseShare("code/one", "bearer-secret", "folder/a");

      const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toContain("/api/s/code%2Fone/browse?subpath=folder%2Fa");
      expect(url).not.toContain("bearer-secret");
      expect(options.headers).toEqual({ Authorization: "Bearer bearer-secret" });
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

  describe("api.listHistoryPage", () => {
    it("fetches a page of task history with filters", async () => {
      const mockPage = {
        items: [{ id: 1, task_name: "task1" }],
        total: 1,
        page: 2,
        page_size: 20,
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockPage),
      });

      const result = await api.listHistoryPage({
        page: 2,
        pageSize: 20,
        status: "failed",
        q: "task",
      });

      expect(result).toEqual(mockPage);
      const [url] = (global.fetch as jest.Mock).mock.calls[0] as [string, RequestInit];
      expect(url).toContain("/api/v2/history?");
      expect(url).toContain("page=2");
      expect(url).toContain("page_size=20");
      expect(url).toContain("status=failed");
      expect(url).toContain("q=task");
    });

    it("omits empty filters from the query string", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ items: [], total: 0, page: 1, page_size: 20 }),
      });

      await api.listHistoryPage({ page: 1, pageSize: 20 });

      const [url] = (global.fetch as jest.Mock).mock.calls[0] as [string, RequestInit];
      expect(url).toBeTruthy();
      expect(url).not.toContain("status=");
      expect(url).not.toContain("q=");
    });
  });

  describe("api.deleteHistoryRecords", () => {
    it("sends a single batch delete request with history ids body", async () => {
      const mockResponse = {
        accepted_count: 2,
        failed_count: 0,
        results: [
          {
            history_id: 1,
            ok: true,
            state: "deleted",
            accepted: true,
            error: null,
          },
          {
            history_id: 2,
            ok: true,
            state: "deleted",
            accepted: true,
            error: null,
          },
        ],
      };
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await api.deleteHistoryRecords([1, 2]);

      expect(result).toEqual(mockResponse);
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/history"),
        expect.objectContaining({
          method: "DELETE",
          body: JSON.stringify({ history_ids: [1, 2] }),
        })
      );
    });

    it("sends a single-element array for inline single delete", async () => {
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({ accepted_count: 1, failed_count: 0, results: [] }),
      });

      await api.deleteHistoryRecords([123]);

      const [url, options] = (global.fetch as jest.Mock).mock.calls[0] as [
        string,
        RequestInit,
      ];
      expect(url.endsWith("/api/history")).toBe(true);
      expect(options.method).toBe("DELETE");
      expect(JSON.parse(options.body as string)).toEqual({
        history_ids: [123],
      });
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

describe("api error message parsing", () => {
  afterEach(() => {
    global.fetch = originalFetch;
  });

  test.each([
    ["detail field", { detail: "后端错误" }, "后端错误"],
    ["message field", { message: "服务异常" }, "服务异常"],
    [
      "blank detail falls back to message",
      { detail: "   ", message: "使用 message" },
      "使用 message",
    ],
    ["non-string fields keep raw text", { code: 1 }, '{"code":1}'],
  ])("uses %s for error message", async (_label, body, expected) => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 400,
      text: () => Promise.resolve(JSON.stringify(body)),
    });

    await expect(api.getStats()).rejects.toMatchObject({
      status: 400,
      message: expected,
    });
  });

  it("falls back to status text when body is empty", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 503,
      text: () => Promise.resolve(""),
    });

    await expect(api.getStats()).rejects.toMatchObject({
      status: 503,
      message: "请求失败: 503",
    });
  });
});

describe("api thin wrappers", () => {
  afterEach(() => {
    global.fetch = originalFetch;
  });

  const ok = (data: unknown) => ({
    ok: true,
    json: () => Promise.resolve(data),
  });

  test.each([
    ["refreshTrackers", () => api.refreshTrackers(), "/api/config/trackers/refresh", "POST", undefined],
    [
      "invalidateAllCredentials",
      () => api.invalidateAllCredentials(),
      "/api/config/credentials/invalidate",
      "POST",
      { confirm: "INVALIDATE_ALL_CREDENTIALS" },
    ],
    [
      "createPackTask",
      () => api.createPackTask([1, 2], "out.zip", true),
      "/api/files/pack",
      "POST",
      { file_ids: [1, 2], output_name: "out.zip", delete_source: true },
    ],
    [
      "createPackTask defaults",
      () => api.createPackTask([3]),
      "/api/files/pack",
      "POST",
      { file_ids: [3], output_name: undefined, delete_source: false },
    ],
    [
      "calculatePackSize",
      () => api.calculatePackSize([4, 5]),
      "/api/files/pack/calculate-size",
      "POST",
      { file_ids: [4, 5] },
    ],
    ["getAvailableSpace", () => api.getAvailableSpace(), "/api/files/pack/available-space", "GET", undefined],
    ["deletePackTask", () => api.deletePackTask(9), "/api/files/pack/9", "DELETE", undefined],
    ["clearPackTasks", () => api.clearPackTasks(), "/api/files/pack", "DELETE", undefined],
    [
      "listStoredFiles with filters",
      () => api.listStoredFiles(2, 50, "term", true),
      "/api/admin/storage/files?page=2&page_size=50&search=term&orphan_only=true",
      "GET",
      undefined,
    ],
    ["getFileUsers", () => api.getFileUsers(7), "/api/admin/storage/files/7/users", "GET", undefined],
    [
      "bulkDeleteStoredFiles",
      () => api.bulkDeleteStoredFiles([1, 2]),
      "/api/admin/storage/files",
      "DELETE",
      { file_ids: [1, 2] },
    ],
    [
      "createShare",
      () => api.createShare({ file_ids: [1], expire_hours: 24 } as never),
      "/api/shares",
      "POST",
      { file_ids: [1], expire_hours: 24 },
    ],
    ["listShares", () => api.listShares(), "/api/shares", "GET", undefined],
    ["revokeShare", () => api.revokeShare(3), "/api/shares/3/revoke", "PUT", undefined],
    [
      "deleteShares",
      () => api.deleteShares([5, 6]),
      "/api/shares",
      "DELETE",
      { share_ids: [5, 6] },
    ],
    ["revokeAllShares", () => api.revokeAllShares(), "/api/shares/revoke-all", "PUT", undefined],
    ["getShareInfo", () => api.getShareInfo("ab c"), "/api/s/ab%20c", "GET", undefined],
    [
      "accessShare",
      () => api.accessShare("code1", "pw"),
      "/api/s/code1/access",
      "POST",
      { password: "pw" },
    ],
  ])("%s hits expected endpoint", async (_name, invoke, url, method, body) => {
    global.fetch = jest.fn().mockResolvedValue(ok({}));

    await invoke();

    const [calledUrl, options] = (global.fetch as jest.Mock).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(calledUrl.endsWith(url)).toBe(true);
    expect(options.method ?? "GET").toBe(method);
    if (body !== undefined) {
      expect(JSON.parse(options.body as string)).toEqual(body);
    } else {
      expect(options.body).toBeUndefined();
    }
  });
});
