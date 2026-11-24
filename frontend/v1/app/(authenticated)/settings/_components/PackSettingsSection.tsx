import type { SettingsFormState } from "../settingsState";

type PackSettingsSectionProps = {
  form: Pick<SettingsFormState, "packFormat" | "packCompressionLevel">;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
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
        <label className="form-label-lg" htmlFor="settings-pack-compression-level">压缩等级: {form.packCompressionLevel}</label>
        <p className="muted text-sm mb-3">
          {form.packFormat === "zip"
            ? "ZIP: 0 = 仅打包不压缩, 1 = 最快, 9 = 最慢/最小体积"
            : "TAR+Zstandard: 0-9 会映射到 zstd 速度/压缩率档位"}
        </p>
        <input
          id="settings-pack-compression-level"
          type="range"
          min="0"
          max="9"
          value={form.packCompressionLevel}
          onChange={(e) => onFieldChange("packCompressionLevel", parseInt(e.target.value))}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="压缩等级"
        />
      </div>
    </>
  );
}
