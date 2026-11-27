/**
 * 列表多选辅助：范围计算与快捷键目标判定（纯函数，无 React 依赖）。
 */

/** 文本框/下拉框/可编辑区域。复选框不在其中：它是列表选择自身的操作控件。 */
const EDITABLE_SELECTOR =
  'input:not([type="checkbox"]), textarea, select, [contenteditable]:not([contenteditable="false"])';

/** 键盘快捷键是否应让行给当前焦点元素（输入框、下拉框、可编辑区域）。 */
export function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(EDITABLE_SELECTOR) !== null;
}

/** 受控复选框的 onChange 由原生 click 派生：shiftKey 只存在于 nativeEvent 上。 */
export function isShiftClick(nativeEvent: Event): boolean {
  const { shiftKey } = nativeEvent as Event & { shiftKey?: boolean };
  return shiftKey === true;
}

/**
 * 锚点到目标项之间的连续范围，按 `ids` 的当前顺序取切片。
 * 结果只包含 `ids` 里存在的项目：目标不在列表中时返回空，锚点失效时退化为只选目标。
 */
export function rangeBetween<TId>(ids: readonly TId[], from: TId | null, to: TId): TId[] {
  const toIndex = ids.indexOf(to);
  if (toIndex < 0) return [];
  const fromIndex = from === null ? -1 : ids.indexOf(from);
  if (fromIndex < 0) return [to];
  return ids.slice(Math.min(fromIndex, toIndex), Math.max(fromIndex, toIndex) + 1);
}
