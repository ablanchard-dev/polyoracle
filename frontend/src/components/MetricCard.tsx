type MetricCardProps = {
  label: string;
  value: string;
  tone?: "normal" | "good" | "warning" | "danger";
};

const toneClass = {
  normal: "text-slate-100",
  good: "text-accent",
  warning: "text-warning",
  danger: "text-danger"
};

export function MetricCard({ label, value, tone = "normal" }: MetricCardProps) {
  return (
    <div className="rounded border border-line bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${toneClass[tone]}`}>{value}</div>
    </div>
  );
}
