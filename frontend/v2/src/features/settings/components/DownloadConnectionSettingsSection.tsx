import {
  DOWNLOAD_CONNECTION_LIMIT_FIELDS,
  DOWNLOAD_CONNECTION_LIMIT_MAX,
  DOWNLOAD_CONNECTION_LIMIT_MIN,
} from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

type DownloadConnectionSettingsSectionProps = {
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
};

export function DownloadConnectionSettingsSection({
  values,
  onChange,
}: DownloadConnectionSettingsSectionProps) {
  return (
    <section className="admin-settings-section" aria-label="下载并发限制">
      <div className="settings-section-head">
        <h2>下载并发限制</h2>
        <p>每项取值 0-1000；留空的项沿用后端默认值，不会被当成 0 提交。</p>
      </div>
      <div className="form-grid">
        {DOWNLOAD_CONNECTION_LIMIT_FIELDS.map(({ id, label, description, key }) => (
          <NumberSettingField
            key={id}
            id={id}
            label={label}
            description={description}
            value={values[key] ?? ""}
            min={DOWNLOAD_CONNECTION_LIMIT_MIN}
            max={DOWNLOAD_CONNECTION_LIMIT_MAX}
            unit="个"
            onChange={(value) => onChange(key, value)}
          />
        ))}
      </div>
    </section>
  );
}
