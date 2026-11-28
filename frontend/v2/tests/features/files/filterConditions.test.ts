import {
  defaultRule,
  distinctTypeLabels,
  isRuleDisabled,
  isValueDisabled,
  matchesCondition,
  SIZE_PRESETS,
  type FilterCondition,
} from "@/features/files/model";

function condition(overrides: Partial<FilterCondition> = {}): FilterCondition {
  return { id: 1, field: "type", rule: "eq", value: "zip", ...overrides };
}

const file = { name: "a.zip", size: 200 * 1024 * 1024, isDirectory: false };
const folder = { name: "素材", size: 0, isDirectory: true };
const MB = 1024 * 1024;

describe("matchesCondition", () => {
  it("未选指标的条件不生效", () => {
    expect(matchesCondition(file, condition({ value: "" }))).toBe(true);
    expect(matchesCondition(folder, condition({ field: "size", rule: "gt", value: String(MB) }))).toBe(false);
  });

  it.each([
    ["eq", "zip", true],
    ["eq", "mp4", false],
    ["ne", "zip", false],
    ["ne", "mp4", true],
  ] as const)("类型 %s %s → %s", (rule, value, expected) => {
    expect(matchesCondition(file, condition({ rule, value }))).toBe(expected);
  });

  it("文件夹的类型标签是「文件夹」", () => {
    expect(matchesCondition(folder, condition({ rule: "eq", value: "文件夹" }))).toBe(true);
    expect(matchesCondition(folder, condition({ rule: "ne", value: "文件夹" }))).toBe(false);
  });

  it.each([
    ["gt", String(100 * MB), true],
    ["gt", String(300 * MB), false],
    ["lt", String(300 * MB), true],
    ["lt", String(100 * MB), false],
  ] as const)("大小 %s %s → %s", (rule, value, expected) => {
    expect(matchesCondition(file, condition({ field: "size", rule, value }))).toBe(expected);
  });

  it("大小条件对文件夹恒不匹配", () => {
    expect(matchesCondition(folder, condition({ field: "size", rule: "lt", value: String(10 * MB) }))).toBe(false);
  });

  it("大小等于边界时 ≥ 命中、< 不命中", () => {
    const exact = { name: "b.bin", size: 100 * MB, isDirectory: false };
    expect(matchesCondition(exact, condition({ field: "size", rule: "gt", value: String(100 * MB) }))).toBe(true);
    expect(matchesCondition(exact, condition({ field: "size", rule: "lt", value: String(100 * MB) }))).toBe(false);
  });
});

describe("isRuleDisabled", () => {
  it("已有生效的类型 = 条件时，其他行的 = 禁选", () => {
    const conditions = [condition({ id: 1, rule: "eq", value: "zip" })];
    expect(isRuleDisabled(conditions, 1, "type", "eq")).toBe(true);
  });

  it("未生效（未选指标）的 = 条件不阻塞其他行", () => {
    const conditions = [condition({ id: 1, rule: "eq", value: "" })];
    expect(isRuleDisabled(conditions, 1, "type", "eq")).toBe(false);
  });

  it("本行的 = 不受自己影响；≠ 与大小规则永不禁选", () => {
    const conditions = [condition({ id: 1, rule: "eq", value: "zip" })];
    expect(isRuleDisabled(conditions, 0, "type", "eq")).toBe(false);
    expect(isRuleDisabled(conditions, 1, "type", "ne")).toBe(false);
    expect(isRuleDisabled(conditions, 1, "size", "gt")).toBe(false);
    expect(isRuleDisabled(conditions, 1, "size", "lt")).toBe(false);
  });
});

describe("defaultRule", () => {
  it("类型默认 =，被占用时回退 ≠", () => {
    expect(defaultRule([], 0, "type")).toBe("eq");
    const conditions = [condition({ id: 1, rule: "eq", value: "zip" })];
    expect(defaultRule(conditions, 1, "type")).toBe("ne");
  });

  it("大小默认 ≥", () => {
    expect(defaultRule([], 0, "size")).toBe("gt");
  });
});

describe("isValueDisabled", () => {
  it("完全重复的条件禁选", () => {
    const conditions = [condition({ id: 1, rule: "ne", value: "zip" })];
    expect(isValueDisabled(conditions, 1, { field: "type", rule: "ne" }, "zip")).toBe(true);
    expect(isValueDisabled(conditions, 1, { field: "type", rule: "ne" }, "mp4")).toBe(false);
  });

  it("大小互斥：已有 ≥100MB 时 <100MB 与更小档位禁选", () => {
    const conditions = [condition({ id: 1, field: "size", rule: "gt", value: String(100 * MB) })];
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "lt" }, String(100 * MB))).toBe(true);
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "lt" }, String(10 * MB))).toBe(true);
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "lt" }, String(500 * MB))).toBe(false);
  });

  it("大小互斥：已有 <100MB 时 ≥100MB 与更大档位禁选", () => {
    const conditions = [condition({ id: 1, field: "size", rule: "lt", value: String(100 * MB) })];
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "gt" }, String(100 * MB))).toBe(true);
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "gt" }, String(500 * MB))).toBe(true);
    expect(isValueDisabled(conditions, 1, { field: "size", rule: "gt" }, String(10 * MB))).toBe(false);
  });

  it("同向不互斥但重复禁选；未选指标的其他行不参与计算", () => {
    const conditions = [
      condition({ id: 1, field: "size", rule: "gt", value: String(100 * MB) }),
      condition({ id: 2, field: "size", rule: "gt", value: "" }),
    ];
    expect(isValueDisabled(conditions, 2, { field: "size", rule: "gt" }, String(100 * MB))).toBe(true);
    expect(isValueDisabled(conditions, 2, { field: "size", rule: "gt" }, String(500 * MB))).toBe(false);
  });
});

describe("distinctTypeLabels / SIZE_PRESETS", () => {
  it("去重、文件夹在前、其余字母序", () => {
    expect(distinctTypeLabels([
      { id: 1, name: "b.zip", size: 0, isDirectory: false, createdAtMs: 0 },
      { id: 2, name: "a.zip", size: 0, isDirectory: false, createdAtMs: 0 },
      { id: 3, name: "a.mp4", size: 0, isDirectory: false, createdAtMs: 0 },
      { id: 4, name: "素材", size: 0, isDirectory: true, createdAtMs: 0 },
    ])).toEqual(["文件夹", "mp4", "zip"]);
  });

  it("大小档位固定四档且字节数递增", () => {
    expect(SIZE_PRESETS.map((preset) => preset.label)).toEqual(["10 MB", "100 MB", "500 MB", "1 GB"]);
    const bytes = SIZE_PRESETS.map((preset) => Number(preset.value));
    expect([...bytes].sort((a, b) => a - b)).toEqual(bytes);
  });
});
