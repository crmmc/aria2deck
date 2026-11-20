/**
 * C21 全量聚合读取：归档参考 无分页界面要求数据层顺序读取 V2 全部分页。
 * 处理空页（防御提前返回）、单页满载继续翻页、总数中途增长（取最大）、
 * 总数缩小（已收集量达标即停）；失败直接抛给调用方。
 */

export interface PageResult<T> {
  items: T[];
  total: number;
}

/** 单页读取上限防御：防止服务端异常导致无限翻页。 */
const MAX_PAGES = 10_000;

export async function fetchAllPages<T>(fetchPage: (page: number) => Promise<PageResult<T>>): Promise<T[]> {
  const collected: T[] = [];
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const result = await fetchPage(page);
    collected.push(...result.items);
    if (result.items.length === 0) break;
    if (collected.length >= result.total) break;
  }
  return collected;
}
