import {
  RATE_LIMIT_FIELDS,
  RATE_LIMIT_MAX,
  RATE_LIMIT_MIN,
  type RateLimitField,
} from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

type RateLimitFieldsProps = {
  fields: readonly RateLimitField[];
  rateLimits: Record<string, string>;
  onChange: (key: string, value: string) => void;
};

export function RateLimitFields({ fields, rateLimits, onChange }: RateLimitFieldsProps) {
  return (
    <div className="form-grid">
      {fields.map(({ id, label, description, key }) => (
        <NumberSettingField
          key={id}
          id={id}
          label={label}
          description={description}
          value={rateLimits[key] ?? ""}
          min={RATE_LIMIT_MIN}
          max={RATE_LIMIT_MAX}
          unit="次"
          onChange={(value) => onChange(key, value)}
        />
      ))}
    </div>
  );
}

type RateLimitSettingsSectionProps = Omit<RateLimitFieldsProps, "fields">;

export function RateLimitSettingsSection({ rateLimits, onChange }: RateLimitSettingsSectionProps) {
  return (
    <section className="admin-settings-section" aria-label="接口频率限制">
      <div className="settings-section-head">
        <h2>接口频率限制</h2>
        <p>限制用户在单位时间内的请求次数，修改后即时生效。</p>
      </div>
      <RateLimitFields fields={RATE_LIMIT_FIELDS} rateLimits={rateLimits} onChange={onChange} />
    </section>
  );
}
