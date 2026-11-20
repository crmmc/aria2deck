import { fetchAllPages } from "@/shared/api/pagination";

function paged(totalItems: number, pageSize: number) {
  return (page: number) => {
    const start = (page - 1) * pageSize;
    const items = Array.from({ length: Math.max(0, Math.min(pageSize, totalItems - start)) }, (_, i) => start + i);
    return Promise.resolve({ items, total: totalItems });
  };
}

describe("fetchAllPages", () => {
  it("顺序读取全部页并聚合（超过单页 100 条）", async () => {
    const fetchPage = jest.fn(paged(250, 100));
    const result = await fetchAllPages(fetchPage);
    expect(result).toHaveLength(250);
    expect(result[0]).toBe(0);
    expect(result[249]).toBe(249);
    expect(fetchPage).toHaveBeenCalledTimes(3);
    expect(fetchPage.mock.calls.map(([p]) => p)).toEqual([1, 2, 3]);
  });

  it("单页即全部时只读一页", async () => {
    const fetchPage = jest.fn(paged(7, 100));
    const result = await fetchAllPages(fetchPage);
    expect(result).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("total 为 0 的空列表读一页即停", async () => {
    const fetchPage = jest.fn(paged(0, 100));
    const result = await fetchAllPages(fetchPage);
    expect(result).toEqual([]);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("中途空页提前终止，不无限翻页", async () => {
    const fetchPage = jest
      .fn()
      .mockResolvedValueOnce({ items: [1, 2, 3], total: 300 })
      .mockResolvedValueOnce({ items: [], total: 300 });
    const result = await fetchAllPages(fetchPage);
    expect(result).toEqual([1, 2, 3]);
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it("总数中途增长时按增长后的总量继续读取", async () => {
    const fetchPage = jest
      .fn()
      .mockResolvedValueOnce({ items: [1, 2], total: 4 })
      .mockResolvedValueOnce({ items: [3, 4], total: 6 })
      .mockResolvedValueOnce({ items: [5, 6], total: 6 });
    const result = await fetchAllPages(fetchPage);
    expect(result).toEqual([1, 2, 3, 4, 5, 6]);
    expect(fetchPage).toHaveBeenCalledTimes(3);
  });

  it("总数中途缩小时按已收集量达标即停", async () => {
    const fetchPage = jest
      .fn()
      .mockResolvedValueOnce({ items: [1, 2, 3], total: 100 })
      .mockResolvedValueOnce({ items: [4], total: 4 });
    const result = await fetchAllPages(fetchPage);
    expect(result).toEqual([1, 2, 3, 4]);
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it("某页读取失败时抛出，不返回部分数据", async () => {
    const failure = new Error("network down");
    const fetchPage = jest
      .fn()
      .mockResolvedValueOnce({ items: [1], total: 3 })
      .mockRejectedValueOnce(failure);
    await expect(fetchAllPages(fetchPage)).rejects.toThrow("network down");
  });
});
