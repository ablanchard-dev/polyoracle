import type { LucideIcon } from "lucide-react";

type ControlButtonProps = {
  label: string;
  icon: LucideIcon;
  tone?: "default" | "danger";
  disabled?: boolean;
  onClick: () => void;
};

export function ControlButton({ label, icon: Icon, tone = "default", disabled, onClick }: ControlButtonProps) {
  const toneClass =
    tone === "danger"
      ? "border-danger bg-danger/15 text-red-100 hover:bg-danger/25"
      : "border-line bg-slate-800 text-slate-100 hover:bg-slate-700";

  return (
    <button
      type="button"
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={`flex h-11 items-center gap-2 rounded border px-4 text-sm font-medium transition ${toneClass}`}
    >
      <Icon size={17} />
      <span>{label}</span>
    </button>
  );
}
