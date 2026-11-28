import {
  api,
  ApiRequestError,
  errorMessage,
  setUnauthorizedHandler,
} from "@/shared/api/client";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

describe("api client", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setUnauthorizedHandler(null);
  });

  it("sends JSON headers for bodies and parses responses", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    const result = await api.post<{ ok: boolean }>("/api/v2/x", { a: 1 });
    expect(result).toEqual({ ok: true });
    const [, init] = mockFetch.mock.calls[0];
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(init.body).toBe(JSON.stringify({ a: 1 }));
  });

  it("omits headers when no body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, []));
    await api.get("/api/v2/y");
    const [, init] = mockFetch.mock.calls[0];
    expect(init.headers).toEqual({});
  });

  it("parses stable error structure", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(409, { error: { code: "duplicate_resource", message: "同名文件已存在" } }),
    );
    await expect(api.post("/api/v2/z", {})).rejects.toMatchObject({
      status: 409,
      code: "duplicate_resource",
      message: "同名文件已存在",
    });
  });


  it("network failure surfaces a network error and never triggers the 401 logout handler", async () => {
    const handler = jest.fn();
    setUnauthorizedHandler(handler);
    mockFetch.mockRejectedValueOnce(new TypeError("fetch failed"));
    const failure = await api.get("/api/v2/a").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiRequestError);
    expect((failure as ApiRequestError).status).toBe(0);
    expect((failure as ApiRequestError).code).toBe("network_error");
    expect((failure as ApiRequestError).message).toBe("网络异常，请检查连接后重试");
    expect(handler).not.toHaveBeenCalled();
  });

  it.each([
    [500, "服务器错误，请稍后重试"],
    [404, "资源不存在"],
    [403, "没有执行该操作的权限"],
    [429, "请求过于频繁，请稍后重试"],
    [422, "请求无效"],
  ])("uses fallback message for %i without error body", async (status, message) => {
    mockFetch.mockResolvedValueOnce(jsonResponse(status, {}));
    await expect(api.get("/api/v2/b")).rejects.toMatchObject({ message });
  });

  it("triggers the 401 handler exactly once per request", async () => {
    const handler = jest.fn();
    setUnauthorizedHandler(handler);
    mockFetch.mockResolvedValueOnce(
      jsonResponse(401, { error: { code: "unauthenticated", message: "请先登录" } }),
    );
    await expect(api.get("/api/v2/c")).rejects.toBeInstanceOf(ApiRequestError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("returns undefined for 204", async () => {
    mockFetch.mockResolvedValueOnce({ status: 204, ok: true } as unknown as Response);
    await expect(api.post("/api/v2/d")).resolves.toBeUndefined();
  });

  it("falls back to code/message when error body is unparseable", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 400,
      ok: false,
      json: () => Promise.reject(new Error("not json")),
    } as unknown as Response);
    await expect(api.get("/api/v2/e")).rejects.toMatchObject({
      status: 400,
      code: "http_400",
      message: "请求无效",
    });
  });

  it("maps non-standard error statuses to a generic Chinese message", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(302, {}));
    await expect(api.get("/api/v2/f")).rejects.toMatchObject({
      status: 302,
      code: "http_302",
      message: "请求失败（302）",
    });
  });

  it("401 with unparseable body falls back to default code and message", async () => {
    setUnauthorizedHandler(null);
    mockFetch.mockResolvedValueOnce({
      status: 401,
      ok: false,
      json: () => Promise.reject(new Error("not json")),
    } as unknown as Response);
    await expect(api.get("/api/v2/x")).rejects.toMatchObject({
      code: "unauthenticated",
      message: "会话已过期，请重新登录",
    });
  });

  it("errorMessage returns ApiRequestError message or fallback", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(429, { error: { code: "rate", message: "太频繁" } }));
    const caught = await api.get("/api/v2/y").catch((e) => e);
    expect(errorMessage(caught, "备用")).toBe("太频繁");
    expect(errorMessage("just a string", "备用")).toBe("备用"); // 非 Error 异常回退。
  });

  it("passes extra headers through and skips logout on Bearer 401", async () => {
    const handler = jest.fn();
    setUnauthorizedHandler(handler);
    mockFetch.mockResolvedValueOnce(
      jsonResponse(401, { error: { code: "unauthenticated", message: "令牌无效" } }),
    );
    await expect(
      api.get("/api/s/abc/entries", { headers: { Authorization: "Bearer tok" } }),
    ).rejects.toMatchObject({ code: "unauthenticated" });
    expect(handler).not.toHaveBeenCalled(); // 公开页 401 不触发全局登出。
    const [, init] = mockFetch.mock.calls[0];
    expect(init.headers).toEqual({ Authorization: "Bearer tok" });
  });

  it("Bearer 401 without an error body falls back to the login prompt", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 401,
      ok: false,
      json: () => Promise.reject(new Error("not json")),
    } as unknown as Response);
    await expect(
      api.get("/api/s/abc/entries", { headers: { Authorization: "Bearer tok" } }),
    ).rejects.toMatchObject({
      status: 401,
      code: "unauthenticated",
      message: "请先登录",
    });
  });

});

interface MockResponseInit {
  status: number;
  ok: boolean;
  json: () => Promise<unknown>;
}

function jsonResponse(status: number, body: unknown): MockResponseInit {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
  };
}
