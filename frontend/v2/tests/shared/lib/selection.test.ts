import { isEditableTarget, isShiftClick, rangeBetween } from "@/shared/lib/selection";

const IDS = [1, 2, 3, 4];

describe("rangeBetween", () => {
  it("正向与反向都取两端闭区间", () => {
    expect(rangeBetween(IDS, 1, 3)).toEqual([1, 2, 3]);
    expect(rangeBetween(IDS, 3, 1)).toEqual([1, 2, 3]);
  });

  it("锚点等于目标时只含该项", () => {
    expect(rangeBetween(IDS, 2, 2)).toEqual([2]);
  });

  it("没有锚点时只选目标", () => {
    expect(rangeBetween(IDS, null, 3)).toEqual([3]);
  });

  it("锚点不在列表内时退化为单选", () => {
    expect(rangeBetween(IDS, 99, 3)).toEqual([3]);
  });

  it("目标不在列表内时返回空，不产生不可见选择", () => {
    expect(rangeBetween(IDS, 1, 99)).toEqual([]);
    expect(rangeBetween([], null, 1)).toEqual([]);
  });
});

describe("isShiftClick", () => {
  it("从原生 click 事件读取 shiftKey", () => {
    expect(isShiftClick(new MouseEvent("click", { shiftKey: true }))).toBe(true);
    expect(isShiftClick(new MouseEvent("click"))).toBe(false);
  });

  it("没有 shiftKey 的原生事件不算 Shift 点击", () => {
    expect(isShiftClick(new Event("change"))).toBe(false);
  });
});

describe("isEditableTarget", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("拦截文本输入、多行文本、下拉框与可编辑区域", () => {
    document.body.innerHTML = `
      <input id="text" />
      <textarea id="area"></textarea>
      <select id="select"><option>a</option></select>
      <div id="editable" contenteditable="true"><span id="editable-child">x</span></div>
    `;

    expect(isEditableTarget(document.getElementById("text"))).toBe(true);
    expect(isEditableTarget(document.getElementById("area"))).toBe(true);
    expect(isEditableTarget(document.getElementById("select"))).toBe(true);
    expect(isEditableTarget(document.getElementById("editable"))).toBe(true);
    expect(isEditableTarget(document.getElementById("editable-child"))).toBe(true);
  });

  it("复选框、不可编辑容器与按钮不拦截", () => {
    document.body.innerHTML = `
      <input id="check" type="checkbox" />
      <div id="off" contenteditable="false"><span id="off-child">x</span></div>
      <button id="button">b</button>
    `;

    expect(isEditableTarget(document.getElementById("check"))).toBe(false);
    expect(isEditableTarget(document.getElementById("off"))).toBe(false);
    expect(isEditableTarget(document.getElementById("off-child"))).toBe(false);
    expect(isEditableTarget(document.getElementById("button"))).toBe(false);
  });

  it("没有目标或目标不是元素时视为不可编辑", () => {
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget(new EventTarget())).toBe(false);
  });
});
