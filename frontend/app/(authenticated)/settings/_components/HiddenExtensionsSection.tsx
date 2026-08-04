import type { Dispatch } from "react";
import type { SettingsFormAction, SettingsFormState } from "../settingsState";

type HiddenExtensionsSectionProps = {
  form: Pick<SettingsFormState, "extensionInput" | "hiddenExtensions">;
  dispatch: Dispatch<SettingsFormAction>;
};

export function HiddenExtensionsSection({ form, dispatch }: HiddenExtensionsSectionProps) {
  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      dispatch({ type: "add_hidden_extension", extension: form.extensionInput });
    }
  }

  return (
    <>
      <h2 className="section-title">文件管理配置</h2>

      <div className="mb-7">
        <label className="form-label-lg" htmlFor="settings-hidden-extensions">隐藏文件后缀名</label>
        <p className="muted text-sm mb-2">
          在文件管理页面隐藏指定后缀名的文件。输入后缀名（如 aria2 或 .aria2）并按回车添加。
        </p>

        <div className="flex gap-2 mb-3">
          <input
            id="settings-hidden-extensions"
            className="input flex-1"
            type="text"
            value={form.extensionInput}
            onChange={(e) => dispatch({ type: "set_extension_input", value: e.target.value })}
            onKeyDown={handleKeyDown}
            placeholder="输入后缀名，按回车添加"
            aria-label="隐藏文件后缀名"
          />
          <button type="button" className="button px-4" onClick={() => dispatch({ type: "add_hidden_extension", extension: form.extensionInput })}>
            添加
          </button>
        </div>

        <div className="mb-3">
          <p className="muted text-xs mb-2">常用后缀名：</p>
          <div className="flex gap-2 flex-wrap">
            {[".aria2", ".tmp", ".part", ".download", ".crdownload"].map((ext) => (
              <button
                key={ext}
                type="button"
                onClick={() => dispatch({ type: "add_hidden_extension", extension: ext })}
                className="ext-btn"
              >
                {ext}
              </button>
            ))}
          </div>
        </div>

        {form.hiddenExtensions.length > 0 && (
          <div className="flex gap-2 flex-wrap p-3 bg-black-02 rounded">
            {form.hiddenExtensions.map((ext) => (
              <div key={ext} className="chip">
                <span>{ext}</span>
                <button
                  type="button"
                  onClick={() => dispatch({ type: "remove_hidden_extension", extension: ext })}
                  aria-label={`移除 ${ext}`}
                  className="chip-close"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
