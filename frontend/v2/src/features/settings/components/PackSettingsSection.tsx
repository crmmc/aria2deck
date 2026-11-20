import type { SettingsForm, SettingsFormSetter } from "../settingsForm";

type PackSettingsSectionProps = {
  form: Pick<SettingsForm, "packFormat" | "packCompressionLevel">;
  onFieldChange: SettingsFormSetter;
};

export function PackSettingsSection({ form, onFieldChange }: PackSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title mt-7">打包设置</h2>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-pack-format">打包格式</label>
        <p className="muted text-sm mb-3">选择文件夹打包的压缩格式。</p>
        <div className="flex gap-4">
          <label className="checkbox-label">
            <input
              id="settings-pack-format"
              type="radio"
              name="packFormat"
              value="zip"
              checked={form.packFormat === "zip"}
              onChange={() => onFieldChange("packFormat", "zip")}
              aria-label="ZIP（Deflate64）"
            />
            <span>ZIP（Deflate64）</span>
          </label>
          <label className="checkbox-label">
            <input
              type="radio"
              name="packFormat"
              value="tar.zst"
              checked={form.packFormat === "tar.zst"}
              onChange={() => onFieldChange("packFormat", "tar.zst")}
              aria-label="TAR + Zstandard"
            />
            <span>TAR + Zstandard</span>
          </label>
        </div>
      </div>

      <div className="mb-7">
        <label className="form-label-lg" htmlFor="settings-pack-compression-level">
          压缩等级: {form.packCompressionLevel}
        </label>
        <p className="muted text-sm mb-3">
          {form.packFormat === "zip"
            ? "ZIP: 0 = 仅打包不压缩, 1 = 最快, 9 = 最慢/最小体积"
            : "TAR+Zstandard: 0-9 会映射到 zstd 速度/压缩率档位"}
        </p>
        {/* shortcut: 滑杆沿用 0-9（归档参考实现的界面），V2 契约允许 0-22（仅 zstd 有意义）。
            滑杆 max=9 只限制控件显示：React state 仍保存接口原值，提交按 0-22 放行，
            因此存量值 10-22 会原值提交、不被夹到 9，唯一可见影响是滑杆停在最右端。
            等出现需要 10-22 的界面需求时再扩范围并同步文案。 */}
        <input
          id="settings-pack-compression-level"
          type="range"
          min="0"
          max="9"
          value={form.packCompressionLevel}
          onChange={(e) => onFieldChange("packCompressionLevel", e.target.value)}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="压缩等级"
        />
      </div>
    </>
  );
}
