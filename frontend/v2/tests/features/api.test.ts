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
    expect(body.tasks).toEqual([{ uri: "https://a" }, { uri: "https://b" }]);
  });

  it("fetchTasks appends sort params only when a sort is active", async () => {
    await fetchTasks(null, 2, 20, null);
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/tasks?page=2&page_size=20");
    await fetchTasks("current", 1, 20, { key: "size", order: "asc" });
    const url = new URL(String(mockFetch.mock.calls[1][0]), "http://localhost");
    expect(url.pathname).toBe("/api/v2/tasks");
    expect(url.searchParams.getAll("status")).toEqual(["queued", "active", "waiting", "paused"]);
    expect(url.searchParams.get("sort")).toBe("size");
    expect(url.searchParams.get("order")).toBe("asc");
    expect(url.searchParams.get("page")).toBe("1");
  });

  it("createTorrent sends null selection when no files selected", async () => {
    await createTorrent("dGVzdA==", []);
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body).toEqual({ torrent: "dGVzdA==", selected_file_indexes: null });
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
