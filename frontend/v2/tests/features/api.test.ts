import { createShare, fileDownloadUrl } from "@/features/files/api";
import { createTasks, createTorrent, fetchTasks } from "@/features/tasks/api";

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

  it("fetchTasks appends sort params only when a sort is active", async () => {
    await fetchTasks(null, 2, 20, null, "");
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks?page=2&page_size=20");
    await fetchTasks("current", 1, 20, { key: "size", order: "asc" }, "");
    const url = new URL(String(mockFetch.mock.calls[1][0]), "http://localhost");
    expect(url.pathname).toBe("/api/v2/tasks");
    expect(url.searchParams.getAll("status")).toEqual(["queued", "active", "waiting", "paused"]);
    expect(url.searchParams.get("sort")).toBe("size");
    expect(url.searchParams.get("order")).toBe("asc");
    expect(url.searchParams.get("page")).toBe("1");
  });

  it("fetchTasks sends trimmed search term as q and skips blank terms", async () => {
    await fetchTasks(null, 1, 20, null, "  ubuntu  ");
    expect(mockFetch.mock.calls[0][0]).toBe(
      "/api/v2/tasks?page=1&page_size=20&q=ubuntu",
    );
    await fetchTasks(null, 1, 20, null, "   ");
    expect(mockFetch.mock.calls[1][0]).toBe("/api/v2/tasks?page=1&page_size=20");
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
