import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { SharesPage } from "@/features/shares/SharesPage";
import { shareCopyText } from "@/features/shares/api";
import type { ShareItemResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 1,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 1,
};

function share(id: number, overrides: Partial<ShareItemResponse> = {}): ShareItemResponse {
  return {
    id,
    code: `code-${id}`,
    file_name: `share-file-${id}.zip`,
    file_size: 1024,
    file_kind: "file",
    status: "active",
    expires_at_ms: null,
    max_downloads: 10,
    download_count: 2,
    created_at_ms: 1700000000000,
    has_password: false,
    ...overrides,
  };
}

function list(items: ShareItemResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(handler: (url: string, init?: { method?: string; body?: string }) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, ME);
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    return json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/shares"]}>
      <AppProviders>
        <SharesPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("SharesPage", () => {
  it("renders share rows with status, download counter and password badge", async () => {
    setup(() =>
      list([
        share(1),
        share(2, { status: "exhausted", has_password: true, max_downloads: null }),
      ]),
    );
    expect(await screen.findByTestId("share-row-1")).toBeInTheDocument();
    expect(screen.getByText("share-file-1.zip")).toBeInTheDocument();
    expect(screen.getByText("2 / 10")).toBeInTheDocument();
    expect(screen.getByTestId("share-row-1").textContent).toContain("有效");
    expect(screen.getByTestId("share-row-2").textContent).toContain("已耗尽");
    expect(screen.getByTitle("需要提取密码")).toHaveTextContent("密码");
    expect(screen.getByTestId("share-row-2").textContent).toContain("2 / 不限"); // 无限次分享
  });

  it("filters by status query parameter", async () => {
    setup((url) => {
      if (url.includes("status=revoked")) return list([]);
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "revoked" } });
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("status=revoked");
    });
    expect(await screen.findByText("暂无分享")).toBeInTheDocument();
  });

  it("condition change resets page and issues exactly one list request", async () => {
    setup((url) => {
      if (url.includes("status=revoked")) return list([]);
      return list([share(1)], 45);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("page=2");
    });
    const listCalls = () =>
      mockFetch.mock.calls.filter(([url]) => String(url).startsWith("/api/v2/shares")).length;
    const before = listCalls();

    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "revoked" } });

    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("status=revoked");
    });
    await waitFor(() => expect(listCalls()).toBe(before + 1)); // 旧页码请求不再发出。
    const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
    expect(url).toContain("page=1");
    expect(await screen.findByText("暂无分享")).toBeInTheDocument();
  });

  it("server-side search sends q parameter after debounce and renders server-filtered rows", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("q=bbb222")) return list([share(9, { code: "bbb222" })]);
        return list([share(1, { code: "aaa111" }), share(2, { code: "bbb222" })]);
      });
      await screen.findByTestId("share-row-1");
      fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "bbb222" } });
      await jest.advanceTimersByTimeAsync(350);
      await waitFor(() => {
        const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
        expect(url).toContain("q=bbb222");
      });
      // 服务器筛选结果（唯一行 9）替换整页旧数据。
      expect(await screen.findByTestId("share-row-9")).toBeInTheDocument();
      expect(screen.queryByTestId("share-row-1")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("search change while on page 2 resets to page 1 with q parameter", async () => {
    jest.useFakeTimers();
    try {
      setup((url) => {
        if (url.includes("page=2")) return list([share(2)], 21);
        if (url.includes("q=zip")) return list([share(9)], 1);
        return list([share(1)], 21);
      });
      await screen.findByTestId("share-row-1");
      fireEvent.click(screen.getByLabelText("下一页"));
      await screen.findByTestId("share-row-2");
      fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "zip" } });
      await jest.advanceTimersByTimeAsync(350);
      await waitFor(() => {
        const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
        expect(url).toContain("q=zip");
        expect(url).toContain("page=1");
      });
      expect(await screen.findByTestId("share-row-9")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("copies plain link for passwordless share via clipboard", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => list([share(1)]));
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/code-1`),
    );
    expect(await screen.findByText("已复制分享内容")).toBeInTheDocument();
  });

  it("copies password placeholder format for protected shares and degrades on failure", async () => {
    const writeText = jest.fn()
      .mockRejectedValueOnce(new Error("denied"))
      .mockResolvedValueOnce(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => list([share(1, { has_password: true, code: "pw9" })]));
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(await screen.findByText("复制失败，请手动复制")).toBeInTheDocument(); // 降级提示。
    expect(writeText).toHaveBeenLastCalledWith(
      `${window.location.origin}/s/pw9\n密码：（请向分享者索取）`,
    );
  });

  it("revokes a single share through confirm dialog", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return {
          results: [{ share_id: 1, ok: true, already_revoked: false, error: null }],
          accepted_count: 1,
          rejected_count: 0,
        };
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    const dialog = await screen.findByRole("dialog", { name: "撤销分享" });
    expect(dialog.textContent).toContain("撤销后访问者将无法继续下载");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await screen.findByRole("dialog", { name: "撤销分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "撤销" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/shares/revoke",
      );
      expect(JSON.parse(call![1].body as string)).toEqual({ share_ids: [1] });
    });
    expect(await screen.findByText(/撤销完成：1 个/)).toBeInTheDocument();
  });

  it("bulk delete reports partial failure", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return {
          results: [
            { share_id: 1, ok: true, already_revoked: false, error: null },
            { share_id: 2, ok: false, already_revoked: false, error: { code: "not_found", message: "不存在" } },
          ],
          accepted_count: 1,
          rejected_count: 1,
        };
      }
      return list([share(1), share(2)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByLabelText("选择 share-file-1.zip"));
    fireEvent.click(screen.getByLabelText("选择 share-file-2.zip"));
    fireEvent.click(screen.getByRole("button", { name: /批量删除/ }));
    await screen.findByRole("dialog", { name: "删除分享记录" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText(/删除完成：成功 1 个，失败 1 个/)).toBeInTheDocument();
  });

  it("revoke API failure surfaces Chinese toast", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/shares/revoke") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await screen.findByRole("dialog", { name: "撤销分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "撤销" }).at(-1)!);
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
  });

  it("select-all then clear selection works", async () => {
    setup(() => list([share(1), share(2)]));
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByLabelText("全选分享"));
    expect(screen.getByLabelText("选择 share-file-1.zip")).toBeChecked();
    expect(screen.getByRole("button", { name: /批量撤销/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除选择" }));
    expect(screen.getByLabelText("选择 share-file-1.zip")).not.toBeChecked();
  });

  it("unchecking select-all clears every row", async () => {
    setup(() => list([share(1), share(2)]));
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByLabelText("全选分享"));
    expect(screen.getByLabelText("选择 share-file-1.zip")).toBeChecked();
    fireEvent.click(screen.getByLabelText("全选分享")); // 再点一次取消全选。
    expect(screen.getByLabelText("选择 share-file-1.zip")).not.toBeChecked();
    expect(screen.getByLabelText("选择 share-file-2.zip")).not.toBeChecked();
  });

  it("empty result with a keyword suggests changing terms", async () => {
    setup((url) => {
      if (url.includes("q=nothing")) return list([]);
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.change(screen.getByLabelText("搜索分享"), { target: { value: "nothing" } });
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("q=nothing");
    });
    expect(await screen.findByText("换个关键词试试")).toBeInTheDocument();
  });

  it("row delete goes through its own confirm dialog and cancel path", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return { results: [{ share_id: 1, ok: true, already_revoked: false, error: null }], accepted_count: 1, rejected_count: 0 };
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享记录" });
    fireEvent.click(screen.getByRole("button", { name: "取消" })); // 删除弹窗取消。
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享记录" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText(/删除完成：1 个/)).toBeInTheDocument();
  });

  it("deleting the last row of the last page falls back to previous page", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return { results: [{ share_id: 2, ok: true, already_revoked: false, error: null }], accepted_count: 1, rejected_count: 0 };
      }
      return url.includes("page=2")
        ? list([share(2)], 21)
        : list([share(1)], 21);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("share-row-2");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享记录" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("page=1");
    });
  });

  it("renders expiry timestamp for timed shares", async () => {
    setup(() => list([share(3, { expires_at_ms: new Date(2027, 0, 2, 3, 4, 5).getTime() })]));
    await screen.findByTestId("share-row-3");
    expect(screen.getByTestId("share-row-3").textContent).toContain("2027-01-02 03:04:05");
  });

  it("error state offers a working retry", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(500, { error: { code: "internal", message: "后端炸了" } });
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(200, list([share(8)]));
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByTestId("share-row-8")).toBeInTheDocument();
  });

  it("discards stale list responses after filter changes", async () => {
    let calls = 0;
    let releaseFirst: (value: unknown) => void = () => undefined;
    const firstCall = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      calls += 1;
      if (calls === 1) return firstCall as never;
      return json(200, list([share(1)]));
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByLabelText("状态筛选");
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "revoked" } });
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2));
    releaseFirst(json(200, list([share(42)])));
    await screen.findByTestId("share-row-1");
    expect(screen.queryByTestId("share-row-42")).not.toBeInTheDocument();
  });

  it("delete API whole-request failure surfaces Chinese toast", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE" && url === "/api/v2/shares") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await screen.findByRole("dialog", { name: "删除分享记录" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
  });

  it("revoke-all API failure surfaces Chinese toast", async () => {
    setup((_url, init) => {
      if (init?.method === "DELETE" && _url === "/api/v2/shares/all") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "全部撤销" }));
    await screen.findByRole("dialog", { name: "撤销全部分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "全部撤销" }).at(-1)!);
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
  });

  it("revoke-all requires danger confirmation", async () => {
    setup((_url, init) => {
      if (init?.method === "DELETE" && _url === "/api/v2/shares/all") {
        return { revoked_count: 3 };
      }
      return list([share(1)]);
    });
    await screen.findByTestId("share-row-1");
    fireEvent.click(screen.getByRole("button", { name: "全部撤销" }));
    const dialog = await screen.findByRole("dialog", { name: "撤销全部分享" });
    expect(dialog.textContent).toContain("不可恢复");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(mockFetch.mock.calls.find(([, i]) => i?.method === "DELETE")).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "全部撤销" }));
    await screen.findByRole("dialog", { name: "撤销全部分享" });
    fireEvent.click(screen.getAllByRole("button", { name: "全部撤销" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, i]) => i?.method === "DELETE");
      expect(call![0]).toBe("/api/v2/shares/all");
    });
    expect(await screen.findByText("已撤销 3 个分享")).toBeInTheDocument();
  });

  it("shows error state with retry on load failure", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      return json(500, { error: { code: "internal", message: "后端异常" } });
    });
    render(
      <MemoryRouter initialEntries={["/shares"]}>
        <AppProviders>
          <SharesPage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("后端异常")).toBeInTheDocument();
  });
});

describe("shareCopyText", () => {
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
