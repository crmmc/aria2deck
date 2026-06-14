type NumberSettingFieldProps = {
  id: string;
  label: string;
  description: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
};

export function NumberSettingField({
  id,
  label,
  description,
  value,
  min,
  max,
  onChange,
}: NumberSettingFieldProps) {
  return (
    <div className="mb-7">
      <label className="form-label-lg" htmlFor={id}>{label}</label>
      <p className="muted text-sm mb-3">{description}</p>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(Math.max(min, Math.min(max, parseInt(event.target.value) || min)))}
        className="input"
        style={{ maxWidth: 200 }}
        aria-label={label}
      />
    </div>
  );
}
