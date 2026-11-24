import {
  batchDeleteFiles,
  clearPackRecords,
  createPack,
  createShare,
  cancelPack,
  deletePackRecord,
  fetchEntries,
  fetchFiles,
  fetchPacks,
  fileDownloadUrl,
  precomputePack,
  renameFile,
  searchFiles,
} from "@/features/files/api";
import {
  cancelTasks,
  createTasks,
  createTorrent,
  fetchTasksAll,
  previewTorrent,
  retryTask,
} from "@/features/tasks/api";
import { retryFromHistory } from "@/features/history/api";
import {
  deleteShares,
  fetchSharesAll,
  revokeAllShares,
  revokeShares,
  shareCopyText,
} from "@/features/shares/api";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

beforeEach(() => {
  mockFetch.mockReset();
  mockFetch.mockResolvedValue(
    json(200, { items: [], accepted_count: 0, rejected_count: 0 }),
  );
});

describe("tasks api input normalization", () => {
  it("createTasks trims, drops blanks and dedupes identical lines", async () => {
    await createTasks(" https://a \n\nhttps://a\nhttps://b\n  ");
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body.tasks).toEqual([
      { uri: "https://a", options: { allow_rename: true } },
      { uri: "https://b", options: { allow_rename: true } },
    ]);
  });

  it("createTasks carries a strict allow_rename policy when requested", async () => {
    await createTasks("https://a", false);
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body.tasks[0].options.allow_rename).toBe(false);
  });

  it("fetchTasksAll aggregates every page for the current view (pipeline statuses only)", async () => {
    const requested: string[] = [];
    mockFetch.mockImplementation(async (url: string) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page"));
      requested.push(String(url));
      if (page === 1) {
        return json(200, {
          items: [1, 2].map((id) => ({ id })),
          total: 3,
          page: 1,
          page_size: 2,
        });
      }
      return json(200, { items: [{ id: 3 }], total: 3, page: 2, page_size: 2 });
    });
    const result = await fetchTasksAll();
    expect(result.map((t) => (t as { id: number }).id)).toEqual([1, 2, 3]);
    expect(requested).toHaveLength(2);
    const statuses = new URL(requested[0], "http://localhost").searchParams.getAll("status");
    // 初始加载只取管线状态；失败任务由 WebSocket 单帧保留，刷新后才消失。
    expect(statuses).toEqual(["queued", "active", "waiting", "paused"]);
  });

  it("fetchTasksAll 不发送搜索参数（搜索在聚合后本地完成）", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { items: [], total: 0, page: 1, page_size: 100 }));
    await fetchTasksAll();
    const url = new URL(String(mockFetch.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.has("q")).toBe(false);
    expect(url.searchParams.getAll("status")).toEqual([
      "queued", "active", "waiting", "paused",
    ]);
  });

  it("createTorrent sends null selection when no files selected", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { accepted: true, task: { id: 1 } }));
    await createTorrent("dGVzdA==", []);
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body).toEqual({
      torrent: "dGVzdA==",
      selected_file_indexes: null,
      allow_rename: true,
    });
  });
});

describe("files api contract shapes", () => {
  it("createShare fills omitted options with null fields", async () => {
    await createShare(5, { expires_in_seconds: 3600 });
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({
      user_file_id: 5,
      expires_in_seconds: 3600,
      max_downloads: null,
      password: null,
    });
  });

  it("createShare passes explicit max downloads and password", async () => {
    await createShare(6, { max_downloads: 7, password: "pw" });
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({
      user_file_id: 6,
      expires_in_seconds: null,
      max_downloads: 7,
      password: "pw",
    });
  });
});

describe("files api request shapes", () => {
  it("fetchFiles sends page and page_size", async () => {
    await fetchFiles(2, 10);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/files?page=2&page_size=10");
  });

  it("searchFiles omits scope when unscoped and encodes the term", async () => {
    await searchFiles("报告 2026", null);
    const url = new URL(String(mockFetch.mock.calls[0][0]), "http://localhost");
    expect(url.pathname).toBe("/api/v2/files");
    expect(url.searchParams.get("search")).toBe("报告 2026");
    expect(url.searchParams.has("scope")).toBe(false);
  });

  it("searchFiles sends the directory id as scope", async () => {
    await searchFiles("a", 7);
    const url = new URL(String(mockFetch.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.get("scope")).toBe("7");
  });

  it("fetchEntries targets the file entries endpoint with path and paging", async () => {
    await fetchEntries(3, "子目录", 2, 200);
    const url = new URL(String(mockFetch.mock.calls[0][0]), "http://localhost");
    expect(url.pathname).toBe("/api/v2/files/3/entries");
    expect(url.searchParams.get("path")).toBe("子目录");
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("page_size")).toBe("200");
  });

  it("renameFile patches the file name", async () => {
    await renameFile(9, "新名字.txt");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/files/9");
    expect(mockFetch.mock.calls[0][1].method).toBe("PATCH");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ name: "新名字.txt" });
  });

  it("precomputePack posts the selected file ids", async () => {
    await precomputePack([1, 2]);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs/precompute");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ items: [1, 2] });
  });

  it("cancelPack posts to the cancel endpoint", async () => {
    mockFetch.mockResolvedValueOnce({ status: 204, ok: true, json: () => Promise.resolve(null) });
    await cancelPack(5);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs/5/cancel");
    expect(mockFetch.mock.calls[0][1].method).toBe("POST");
  });

  it("deletePackRecord deletes the record only", async () => {
    mockFetch.mockResolvedValueOnce({ status: 204, ok: true, json: () => Promise.resolve(null) });
    await deletePackRecord(6);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs/6/record");
    expect(mockFetch.mock.calls[0][1].method).toBe("DELETE");
  });

  it("fetchPacks forwards the limit query", async () => {
    await fetchPacks(50);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs?limit=50");
  });
});

describe("fileDownloadUrl", () => {
  it("appends encoded path when present", () => {
    expect(fileDownloadUrl(3)).toBe("/api/v2/files/3/download");
    expect(fileDownloadUrl(3, "docs/a b.txt")).toBe(
      "/api/v2/files/3/download?path=docs%2Fa%20b.txt",
    );
  });
});

// --- 回归（D8）：Torrent 创建响应是单项 TaskWriteResultResponse，不是批量 items ---
describe("torrent single-item response contract", () => {
  const TASK = {
    id: 9,
    status: "completed",
    status_label: "已完成",
    name: "ubuntu.iso",
    uri: null,
    total_bytes: 3,
    completed_bytes: 3,
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
    updated_at_ms: 1,
    finished_at_ms: 1,
  };

  it("resolves a successful single-item torrent response and returns the task", async () => {
    // 与后端 contracts.py 一致：POST /api/v2/tasks/torrent 返回 TaskWriteResultResponse。
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: TASK, outcome: "created", error: null, stop_confirmed: null }),
    );
    const result = (await createTorrent("dGVzdA==", [1])) as unknown as { name?: string };
    expect(result?.name).toBe("ubuntu.iso");
  });

  it("falls back to the fixed Chinese message when a rejection carries no error body", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: false, task: null, outcome: null, error: null, stop_confirmed: null }),
    );
    await expect(createTorrent("dGVzdA==", [1])).rejects.toThrow("创建种子任务失败");
  });

  it("surfaces a business rejection with the backend message so it stays in the wizard", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        accepted: false,
        task: null,
        outcome: null,
        error: { code: "duplicate_resource", message: "同名文件或活动任务已存在" },
        stop_confirmed: null,
      }),
    );
    await expect(createTorrent("dGVzdA==", [1])).rejects.toThrow("同名文件或活动任务已存在");
  });
});

describe("createTasks outcome mapping", () => {
  it("maps rejected items back to their input uri and reason", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [
          { accepted: true, task: { id: 1 }, outcome: "created", error: null, stop_confirmed: null },
          {
            accepted: false,
            task: null,
            outcome: null,
            error: { code: "invalid_uri", message: "链接无效" },
            stop_confirmed: null,
          },
        ],
        accepted_count: 1,
        rejected_count: 1,
      }),
    );
    const outcome = await createTasks("https://a/1\nhttps://a/2");
    expect(outcome.acceptedCount).toBe(1);
    expect(outcome.rejectedCount).toBe(1);
    expect(outcome.failedItems).toEqual([{ uri: "https://a/2", message: "链接无效" }]);
  });

  it("uses a positional fallback when the response has more items than inputs", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        items: [
          {
            accepted: false,
            task: null,
            outcome: null,
            error: { code: "invalid_uri", message: "链接无效" },
            stop_confirmed: null,
          },
        ],
        accepted_count: 0,
        rejected_count: 1,
      }),
    );
    // 空输入会被去重成零条任务，响应条目因此没有对应 uri。
    const outcome = await createTasks("   \n  ");
    expect(outcome.failedItems).toEqual([{ uri: "第 1 条", message: "链接无效" }]);
  });
});

describe("tasks api torrent preview", () => {
  it("posts the base64 payload to the preview endpoint and returns the preview", async () => {
    const PREVIEW = { info_hash: "abc", name: "ubuntu.iso", total_size: 3, files: [] };
    mockFetch.mockResolvedValueOnce(json(200, PREVIEW));
    const result = (await previewTorrent("dGVzdA==")) as unknown as { info_hash?: string };
    expect(result?.info_hash).toBe("abc");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks/torrent/preview");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ torrent: "dGVzdA==" });
  });
});

describe("tasks api retry (C29)", () => {
  it("retries through the per-task endpoint and forwards the allow_rename policy", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: { id: 7 }, outcome: "created", error: null, stop_confirmed: null }),
    );
    const task = (await retryTask(7, false)) as unknown as { id: number };
    expect(task.id).toBe(7);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks/7/retry");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ allow_rename: false });
  });

  it("surfaces the backend message when the retry is business-rejected", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        accepted: false,
        task: null,
        outcome: null,
        error: { code: "not_retryable", message: "任务不可重试" },
        stop_confirmed: null,
      }),
    );
    await expect(retryTask(7)).rejects.toThrow("任务不可重试");
  });

  it("falls back to the fixed Chinese message when the rejection carries no message", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: false, task: null, outcome: null, error: null, stop_confirmed: null }),
    );
    await expect(retryTask(7)).rejects.toThrow("重试失败");
  });
});

describe("tasks api batch cancel", () => {
  it("posts the task ids to the cancel endpoint", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { items: [], accepted_count: 0, rejected_count: 0 }));
    await cancelTasks([3, 4]);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks/cancel");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ task_ids: [3, 4] });
  });
});

// --- C29 回归：历史重试必须检查单项 accepted，业务拒绝不得提示成功 ---
describe("history retry single-item acceptance (C29)", () => {
  const TASK = { id: 42, name: "retry.iso" };

  it("resolves and returns the task on acceptance", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, { accepted: true, task: TASK, outcome: "created", error: null, stop_confirmed: null }),
    );
    const result = (await retryFromHistory(42)) as unknown as { id?: number };
    expect(result?.id).toBe(42);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks/42/retry");
  });

  it("rejects with the backend message when business-rejected over HTTP 200", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        accepted: false,
        task: null,
        outcome: null,
        error: { code: "not_retryable", message: "该任务当前不允许重试" },
        stop_confirmed: null,
      }),
    );
    await expect(retryFromHistory(42)).rejects.toThrow("该任务当前不允许重试");
  });
});

// --- C1 契约：文件批量删除与打包清空使用真实批量端点 ---
describe("files batch delete and pack clear contracts", () => {
  it("batchDeleteFiles sends file_ids to the batch endpoint", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        accepted_count: 1,
        rejected_count: 1,
        results: [
          { file_id: 3, accepted: true, error: null },
          { file_id: 4, accepted: false, error: { code: "not_found", message: "文件不存在" } },
        ],
      }),
    );
    const result = await batchDeleteFiles([3, 4]);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/files");
    expect(mockFetch.mock.calls[0][1].method).toBe("DELETE");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ file_ids: [3, 4] });
    expect(result.rejected_count).toBe(1);
    expect(result.results[1].error?.message).toBe("文件不存在");
  });

  it("clearPackRecords hits the records endpoint and returns cleared_count", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { cleared_count: 5 }));
    const result = await clearPackRecords();
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs/records");
    expect(mockFetch.mock.calls[0][1].method).toBe("DELETE");
    expect(result.cleared_count).toBe(5);
  });

  it("createPack forwards output_name verbatim and delete_source", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { id: 3, status: "pending" }));
    await createPack([1, 2], "archive.zip", true);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/packs");
    expect(mockFetch.mock.calls[0][1].method).toBe("POST");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({
      items: [1, 2],
      output_name: "archive.zip",
      delete_source: true,
    });
  });
});

describe("shares api contracts (SH1)", () => {
  it("fetchSharesAll aggregates every page sequentially for the V1 unpaginated view", async () => {
    const requested: string[] = [];
    mockFetch.mockImplementation(async (url: string) => {
      requested.push(String(url));
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page"));
      if (page === 1) {
        return json(200, {
          items: [{ id: 1 }, { id: 2 }],
          total: 3,
          page: 1,
          page_size: 100,
        });
      }
      return json(200, { items: [{ id: 3 }], total: 3, page: 2, page_size: 100 });
    });
    const items = await fetchSharesAll();
    expect(items.map((item) => item.id)).toEqual([1, 2, 3]);
    expect(requested).toHaveLength(2);
    expect(requested[0]).toBe("/api/v2/shares?page=1&page_size=100");
    expect(requested[1]).toBe("/api/v2/shares?page=2&page_size=100");
  });

  it("fetchSharesAll stops on an empty page without a hidden item cap", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      const page = Number(new URL(String(url), "http://localhost").searchParams.get("page"));
      if (page === 1) {
        return json(200, { items: [{ id: 1 }], total: 9, page: 1, page_size: 100 });
      }
      return json(200, { items: [], total: 9, page: 2, page_size: 100 });
    });
    const items = await fetchSharesAll();
    expect(items.map((item) => item.id)).toEqual([1]);
    expect(mockFetch.mock.calls).toHaveLength(2);
  });

  it("revokeShares posts share_ids and returns per-item results", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        results: [{ share_id: 1, ok: true, already_revoked: false, error: null }],
        accepted_count: 1,
        rejected_count: 0,
      }),
    );
    const result = await revokeShares([1]);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/shares/revoke");
    expect(mockFetch.mock.calls[0][1].method).toBe("POST");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ share_ids: [1] });
    expect(result.accepted_count).toBe(1);
  });

  it("deleteShares sends share_ids to the batch delete endpoint", async () => {
    mockFetch.mockResolvedValueOnce(
      json(200, {
        results: [
          { share_id: 1, ok: true, already_revoked: false, error: null },
          { share_id: 2, ok: false, already_revoked: false, error: { code: "not_found", message: "不存在" } },
        ],
        accepted_count: 1,
        rejected_count: 1,
      }),
    );
    const result = await deleteShares([1, 2]);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/shares");
    expect(mockFetch.mock.calls[0][1].method).toBe("DELETE");
    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string)).toEqual({ share_ids: [1, 2] });
    expect(result.rejected_count).toBe(1);
    expect(result.results[1].error?.message).toBe("不存在");
  });

  it("revokeAllShares hits the revoke-all endpoint", async () => {
    mockFetch.mockResolvedValueOnce(json(200, { revoked_count: 3 }));
    const result = await revokeAllShares();
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/shares/all");
    expect(mockFetch.mock.calls[0][1].method).toBe("DELETE");
    expect(result.revoked_count).toBe(3);
  });
});

describe("shareCopyText (C01)", () => {
  it("plain link without password", () => {
    expect(shareCopyText({ code: "ab", has_password: false })).toBe(
      `${window.location.origin}/s/ab`,
    );
  });

  it("password line uses provided secret or placeholder", () => {
    expect(shareCopyText({ code: "ab", has_password: true }, "pw")).toBe(
      `${window.location.origin}/s/ab\n密码：pw`,
    );
    expect(shareCopyText({ code: "ab", has_password: true })).toBe(
      `${window.location.origin}/s/ab\n密码：（请向分享者索取）`,
    );
  });
});
