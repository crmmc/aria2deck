import {
  buildPageItems,
  measurePageCapacity,
  MIN_PAGE_CAPACITY,
  type PageItem,
} from "@/shared/components/pagination-utils";

function numbers(items: PageItem[]): number[] {
  return items.filter((item): item is number => typeof item === "number");
}

/** 造一个可测量的容器：jsdom 不实现布局，宽度/外边距全部显式给定。 */
function measureTarget(tag: string, width: number): HTMLElement {
  const element = document.createElement(tag);
  Object.defineProperty(element, "offsetWidth", { value: width, configurable: true });
  Object.defineProperty(element, "clientWidth", { value: width, configurable: true });
  return element;
}

type BoxStyle = {
  paddingLeft?: string;
  paddingRight?: string;
  columnGap?: string;
  gap?: string;
  marginLeft?: string;
  marginRight?: string;
};

const styles = new Map<Element, BoxStyle>();

function stubComputedStyle(): void {
  jest.spyOn(window, "getComputedStyle").mockImplementation((element: Element) => {
    const style = styles.get(element) ?? {};
    return {
      paddingLeft: style.paddingLeft ?? "",
      paddingRight: style.paddingRight ?? "",
      columnGap: style.columnGap ?? "",
      gap: style.gap ?? "",
      marginLeft: style.marginLeft ?? "",
      marginRight: style.marginRight ?? "",
    } as unknown as CSSStyleDeclaration;
  });
}

afterEach(() => {
  styles.clear();
  jest.restoreAllMocks();
});

describe("buildPageItems 容量归一化", () => {
  it.each([
    ["NaN 回退最小容量", Number.NaN, buildPageItems(3, 20, MIN_PAGE_CAPACITY)],
    ["Infinity 回退最小容量", Number.POSITIVE_INFINITY, buildPageItems(3, 20, MIN_PAGE_CAPACITY)],
    ["小数向下取整", 7.9, buildPageItems(3, 20, MIN_PAGE_CAPACITY)],
    ["小于最小值被抬到最小值", 2, buildPageItems(3, 20, MIN_PAGE_CAPACITY)],
  ])("%s", (_label, capacity, expected) => {
    expect(buildPageItems(3, 20, capacity)).toEqual(expected);
  });
});

describe("buildPageItems 窗口与省略号", () => {
  it("首页时左侧无省略号，右侧有", () => {
    const items = buildPageItems(1, 100, MIN_PAGE_CAPACITY);
    expect(items[0]).toBe(1);
    expect(items[1]).not.toBe("ellipsis");
    expect(items[items.length - 2]).toBe("ellipsis");
    expect(items[items.length - 1]).toBe(100);
  });

  it("末页时右侧无省略号，左侧有", () => {
    const items = buildPageItems(100, 100, MIN_PAGE_CAPACITY);
    expect(items[0]).toBe(1);
    expect(items[1]).toBe("ellipsis");
    expect(items[items.length - 2]).not.toBe("ellipsis");
    expect(items[items.length - 1]).toBe(100);
  });

  it("中间页两侧都有省略号，且包含当前页", () => {
    const items = buildPageItems(50, 100, MIN_PAGE_CAPACITY);
    expect(items[0]).toBe(1);
    expect(items[1]).toBe("ellipsis");
    expect(items[items.length - 1]).toBe(100);
    expect(items[items.length - 2]).toBe("ellipsis");
    expect(numbers(items)).toContain(50);
  });

  it("紧邻首页时窗口左端停在 2，右侧以省略号接末页", () => {
    // currentPage=2 时窗口左端被抬到 firstInner=2，内层只剩 4 个页码。
    expect(buildPageItems(2, 100, MIN_PAGE_CAPACITY)).toEqual([1, 2, 3, 4, 5, "ellipsis", 100]);
  });

  it("紧邻末页时窗口右端补满到末页内层，左侧以省略号接首页", () => {
    expect(buildPageItems(99, 100, MIN_PAGE_CAPACITY)).toEqual([1, "ellipsis", 96, 97, 98, 99, 100]);
  });

  it("窗口右侧仍可扩展时向右扩展，末页由省略号表示", () => {
    // totalPages=8、capacity=7：内层 5 格，窗口从 2 起只能向右扩。
    expect(buildPageItems(1, 8, MIN_PAGE_CAPACITY)).toEqual([1, 2, 3, 4, 5, "ellipsis", 8]);
    expect(numbers(buildPageItems(1, 8, MIN_PAGE_CAPACITY))).not.toContain(6);
  });

  it("capacity 较大时不再夹紧为最小值", () => {
    const items = buildPageItems(50, 100, 40);
    expect(items.length).toBe(40);
    expect(items[0]).toBe(1);
    expect(items[items.length - 1]).toBe(100);
  });

  it("总页数恰好等于容量时全部平铺", () => {
    expect(buildPageItems(4, 7, MIN_PAGE_CAPACITY)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("窗口左端正好落在 2 之后一格时向左对齐到 2", () => {
    // currentPage=4 时 centerWindow 得到 start=3（= firstInner+1），被拉回 2。
    expect(buildPageItems(4, 100, MIN_PAGE_CAPACITY)).toEqual([1, 2, 3, 4, 5, "ellipsis", 100]);
  });

  it("窗口右端正好落在末页内层之前一格时向右对齐到末页内层", () => {
    // currentPage=5、totalPages=8 时 end=6（= lastInner-1），被拉到 7。
    expect(buildPageItems(5, 8, MIN_PAGE_CAPACITY)).toEqual([1, "ellipsis", 4, 5, 6, 7, 8]);
  });
});

describe("measurePageCapacity", () => {
  it("按容器内宽减去固定占用后计算可容纳页数", () => {
    stubComputedStyle();
    const container = measureTarget("div", 400);
    styles.set(container, { paddingLeft: "10px", paddingRight: "10px", columnGap: "6px" });

    const sizeHint = measureTarget("span", 80);
    styles.set(sizeHint, { marginLeft: "2px", marginRight: "2px" });
    const pageGroup = measureTarget("div", 200);
    pageGroup.dataset.paginationPages = "true";
    const arrow = measureTarget("button", 30);
    arrow.dataset.paginationArrow = "true";
    pageGroup.appendChild(arrow);
    container.append(sizeHint, pageGroup);

    // 固定占用 = 20(内边距) + 6(间距) + 80+4(尺寸提示) + 30(箭头)
    expect(measurePageCapacity(container)).toBe(Math.floor((400 - 140) / 36));
  });

  it("只有单个子元素时不产生间距，结果不低于最小容量", () => {
    stubComputedStyle();
    const container = measureTarget("div", 200);
    styles.set(container, { gap: "20px" });
    const only = measureTarget("span", 50);
    container.append(only);

    // (200 - 50) / 36 = 4，被抬到最小容量 7。
    expect(measurePageCapacity(container)).toBe(MIN_PAGE_CAPACITY);
  });

  it("columnGap 为空字符串时回退到 gap", () => {
    stubComputedStyle();
    const container = measureTarget("div", 300);
    styles.set(container, { columnGap: "", gap: "8px" });
    container.append(measureTarget("span", 10), measureTarget("span", 10));

    // 固定占用 = 8(间距) + 10 + 10
    expect(measurePageCapacity(container)).toBe(Math.floor((300 - 28) / 36));
  });

  it("无法解析的宽度按 0 处理且结果不低于最小容量", () => {
    stubComputedStyle();
    const container = measureTarget("div", 100);
    styles.set(container, { paddingLeft: "auto", paddingRight: "auto" });
    container.append(measureTarget("span", 500));

    expect(measurePageCapacity(container)).toBe(MIN_PAGE_CAPACITY);
  });
});

describe("buildPageItems 窗口扩展分支", () => {
  it("只在右侧可扩展时向右补足（当前页靠近首页）", () => {
    // capacity 10、totalPages 30、currentPage=2：左侧无可扩展空间，只能向右扩。
    const items = buildPageItems(2, 30, 10);
    expect(items[0]).toBe(1);
    expect(items).toContain(2);
    // 不应把第 1 页渲染成省略号
    expect(items[1]).not.toBe("ellipsis");
  });

  it("只在左侧可扩展时向左补足（当前页靠近末页）", () => {
    const items = buildPageItems(29, 30, 10);
    expect(items[items.length - 1]).toBe(30);
    // 末页前可直接连到 30，不应出现右侧省略号
    expect(items[items.length - 2]).not.toBe("ellipsis");
  });

  it("就近扩展时优先补距离较近的一侧", () => {
    const nearLeft = buildPageItems(5, 60, 9);
    const nearRight = buildPageItems(56, 60, 9);
    // 两者都应包含当前页与首末页
    expect(nearLeft[0]).toBe(1);
    expect(nearLeft[nearLeft.length - 1]).toBe(60);
    expect(nearRight[0]).toBe(1);
    expect(nearRight[nearRight.length - 1]).toBe(60);
    expect(nearLeft).toContain(5);
    expect(nearRight).toContain(56);
  });
});

describe("buildPageItems 双侧扩展的距离比较", () => {
  it("左右都可扩展时按距离就近补位", () => {
    // capacity=9 → innerSlots=7 → windowSize=5；currentPage 略偏左时先向右扩，偏右时先向左扩。
    for (const current of [3, 4, 5, 6, 7]) {
      const items = buildPageItems(current, 50, 9) as (number | "ellipsis")[];
      expect(items[0]).toBe(1);
      expect(items[items.length - 1]).toBe(50);
      expect(items).toContain(current);
      // 双侧都还有空间时一定出现省略号（即有截断）
      expect(items.filter((x) => x === "ellipsis").length).toBeGreaterThan(0);
    }
  });
});
