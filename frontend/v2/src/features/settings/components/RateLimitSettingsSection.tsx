import { RATE_LIMIT_FIELDS, RATE_LIMIT_MAX, RATE_LIMIT_MIN } from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

type RateLimitSettingsSectionProps = {
  rateLimits: Record<string, string>;
  onChange: (key: string, value: string) => void;
};

/**
 * 归档参考实现的 11 项限流控件与顺序保留；标签与说明沿用 归档参考，区间取 V2 契约 1-1000000
 * （归档参考实现的 0 = 不限制与部分更窄上限按 C32 视为安全下限收紧）。
 */
export function RateLimitSettingsSection({ rateLimits, onChange }: RateLimitSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title">接口频率限制</h2>
      <p className="muted text-sm mb-4">限制用户在单位时间内的请求次数，修改后即时生效。</p>
      {RATE_LIMIT_FIELDS.map(({ id, label, description, key }) => (
        <NumberSettingField
          key={id}
          id={id}
          label={label}
          description={description}
          value={Number(rateLimits[key] ?? "") || RATE_LIMIT_MIN}
          min={RATE_LIMIT_MIN}
          max={RATE_LIMIT_MAX}
          onChange={(value) => onChange(key, String(value))}
        />
      ))}
    </>
  );
}
