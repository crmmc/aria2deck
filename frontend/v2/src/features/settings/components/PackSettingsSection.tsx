import { SegmentedControl } from "@/shared/components/SegmentedControl";

import type { SettingsForm, SettingsFormSetter } from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

const PACK_FORMAT_OPTIONS = [
  { value: "zip", label: "ZIP · Deflate64", count: null },
  { value: "tar.zst", label: "TAR · Zstandard", count: null },
] as const;

type PackSettingsSectionProps = {
  form: Pick<SettingsForm, "packFormat" | "packCompressionLevel">;
  onFieldChange: SettingsFormSetter;
};

export function PackSettingsSection({ form, onFieldChange }: PackSettingsSectionProps) {
  return (
    <section id="settings-pack" tabIndex={-1} className="admin-settings-section" aria-label="打包设置">
      <div className="settings-section-head">
        <h2>打包设置</h2>
        <p>选择文件夹打包格式和默认压缩等级。</p>
      </div>
      <div className="field admin-settings-format-field">
        <span className="field-label">打包格式</span>
        <p className="field-hint">新创建的打包任务使用此格式。</p>
        <div className="pack-format-options">
          <SegmentedControl
            ariaLabel="打包格式"
            mode="radio"
            options={PACK_FORMAT_OPTIONS}
            value={form.packFormat}
            onChange={(value) => onFieldChange("packFormat", value)}
          />
        </div>
      </div>
      <NumberSettingField
        id="settings-pack-compression-level"
        label="压缩等级"
        description="0 表示仅打包，9 表示更高压缩率；数值输入支持 0–22，滑杆调整 0–9。"
        value={form.packCompressionLevel}
        min={0}
        max={22}
        rangeMax={9}
        onChange={(value) => onFieldChange("packCompressionLevel", value)}
      />
    </section>
  );
}
