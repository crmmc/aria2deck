import {
  DOWNLOAD_CONNECTION_LIMIT_FIELDS,
  DOWNLOAD_CONNECTION_LIMIT_MAX,
  DOWNLOAD_CONNECTION_LIMIT_MIN,
} from "../settingsForm";

/** 归档参考 输入即时夹取到范围内的行为保留；空值保持为空（表示沿用后端默认，不转成 0）。
 *  number 输入框本身不会产出非数字文本，非数字到达时也落到空串而不是 0。 */
function clampConnectionLimit(raw: string): string {
  const parsed = parseInt(raw);
  if (raw.trim() === "" || Number.isNaN(parsed)) return "";
  return String(Math.max(DOWNLOAD_CONNECTION_LIMIT_MIN, Math.min(DOWNLOAD_CONNECTION_LIMIT_MAX, parsed)));
}

type DownloadConnectionSettingsSectionProps = {
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
};

/**
 * 归档参考实现的「下载并发限制」区块与行式布局保留；控件集合改为 V2 真实的六个配置键。
 * 归档参考实现的 authenticated reserved 与 anonymous base/borrow 在 V2 没有对应键（C03/C04），
 * 因此渲染六项而不是旧的八项。留空表示沿用后端默认值，不会被当成 0 提交。
 */
export function DownloadConnectionSettingsSection({
  values,
  onChange,
}: DownloadConnectionSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title mt-7">下载并发限制</h2>
      <p className="muted text-sm mb-4">
        使用后端六个配置键控制并发下载容量，每项取值 0-1000；留空的项沿用后端默认值。
      </p>
      {DOWNLOAD_CONNECTION_LIMIT_FIELDS.map(({ id, label, description, key }) => (
        <div className="mb-7" key={id}>
          <label className="form-label-lg" htmlFor={id}>{label}</label>
          <p className="muted text-sm mb-3">{description}</p>
          <input
            id={id}
            type="number"
            min={DOWNLOAD_CONNECTION_LIMIT_MIN}
            max={DOWNLOAD_CONNECTION_LIMIT_MAX}
            value={values[key] ?? ""}
            onChange={(event) => onChange(key, clampConnectionLimit(event.target.value))}
            className="input"
            style={{ maxWidth: 200 }}
            placeholder="默认"
            aria-label={label}
          />
        </div>
      ))}
    </>
  );
}
