import { emitTaskActivity, onTaskActivity } from "@/shared/lib/taskEvents";

describe("taskEvents（G11 页面间活动信号）", () => {
  it("notifies subscribers on emit and stops after unsubscribe", () => {
    const listener = jest.fn();
    const unsubscribe = onTaskActivity(listener);
    emitTaskActivity();
    emitTaskActivity();
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    emitTaskActivity();
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
