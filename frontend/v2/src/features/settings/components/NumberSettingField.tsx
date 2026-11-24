import type { CSSProperties } from "react";

type NumberSettingFieldProps = {
  id: string;
  label: string;
  description: string;
  value: string;
  min?: number;
  max?: number;
  step?: number | "any";
  unit?: string;
  rangeMax?: number;
  error?: string;
  onChange: (value: string) => void;
};

export function NumberSettingField({
  id,
  label,
  description,
  value,
  min = 0,
  max,
  step = 1,
  unit,
  rangeMax,
  error,
  onChange,
}: NumberSettingFieldProps) {
  const hasRange = rangeMax !== undefined;
  const parsed = Number(value);
  const sliderValue = value.trim() === "" || !Number.isFinite(parsed)
    ? min
    : Math.max(min, Math.min(rangeMax ?? max ?? parsed, parsed));
  const sliderMax = rangeMax ?? max ?? min;
  const rangePercent = sliderMax <= min
    ? 0
    : ((sliderValue - min) / (sliderMax - min)) * 100;
  const rangeStyle = { "--range": `${rangePercent}%` } as CSSProperties;

  return (
    <div className="field">
      {hasRange ? (
        <div className="range-label">
          <label className="field-label" htmlFor={id}>{label}</label>
          <strong className="mono">{value}{unit ? ` ${unit}` : ""}</strong>
        </div>
      ) : (
        <label className="field-label" htmlFor={id}>{label}</label>
      )}
      <p className="field-hint">{description}</p>
      {hasRange ? (
        <div className="range-row">
          <input
            id={id}
            type="range"
            min={min}
            max={rangeMax}
            step={step}
            value={sliderValue}
            style={rangeStyle}
            onChange={(event) => onChange(event.target.value)}
            aria-label={label}
          />
          <input
            className="input"
            type="number"
            min={min}
            max={max}
            step={step}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            aria-label={`${label}数值`}
            aria-invalid={error ? true : undefined}
          />
        </div>
      ) : (
        <div className={unit ? "number-unit" : undefined}>
          <input
            id={id}
            className="input"
            type="number"
            min={min}
            max={max}
            step={step}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            aria-label={label}
            aria-invalid={error ? true : undefined}
          />
          {unit && (
            <input
              className="input number-unit-readonly"
              value={unit}
              aria-label={`${label}单位`}
              tabIndex={-1}
              readOnly
            />
          )}
        </div>
      )}
      <p className="field-error" data-error-for={id} role={error ? "alert" : undefined}>
        {error ?? ""}
      </p>
    </div>
  );
}
