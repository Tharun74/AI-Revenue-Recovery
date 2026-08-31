"use client";
import { cn } from "@/lib/utils";

interface CardProps {
  children: React.ReactNode;
  className?: string;
  glow?: boolean;
  onClick?: () => void;
}

export function Card({ children, className, glow, onClick }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={cn(
        "rounded-2xl border p-6 transition-all duration-300",
        "bg-[var(--surface)] border-[var(--border)]",
        glow && "glow-border shadow-[var(--accent-glow)]",
        onClick && "cursor-pointer hover:border-[var(--border-bright)]",
        className
      )}
    >
      {children}
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string | number;
  sub?: string;
  icon?: React.ReactNode;
  accent?: "emerald" | "blue" | "amber" | "rose" | "indigo" | "default";
  loading?: boolean;
}

const accentMap = {
  emerald: { text: "text-emerald-400", bg: "bg-emerald-400/10", border: "border-emerald-400/20" },
  blue: { text: "text-blue-400", bg: "bg-blue-400/10", border: "border-blue-400/20" },
  amber: { text: "text-amber-400", bg: "bg-amber-400/10", border: "border-amber-400/20" },
  rose: { text: "text-rose-400", bg: "bg-rose-400/10", border: "border-rose-400/20" },
  indigo: { text: "text-indigo-400", bg: "bg-indigo-400/10", border: "border-indigo-400/20" },
  default: { text: "text-slate-300", bg: "bg-slate-400/10", border: "border-slate-400/20" },
};

export function StatCard({ label, value, sub, icon, accent = "default", loading }: StatCardProps) {
  const colors = accentMap[accent];
  return (
    <Card className={cn("flex flex-col gap-3", colors.border, "border")}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-widest text-[var(--muted)]">{label}</span>
        {icon && (
          <div className={cn("p-2 rounded-xl", colors.bg)}>
            <span className={colors.text}>{icon}</span>
          </div>
        )}
      </div>
      {loading ? (
        <div className="h-9 w-32 rounded-lg shimmer" />
      ) : (
        <div className="count-up">
          <p className={cn("text-3xl font-bold tracking-tight", colors.text)}>{value}</p>
          {sub && <p className="text-sm text-[var(--muted)] mt-1">{sub}</p>}
        </div>
      )}
    </Card>
  );
}

export function Badge({
  children,
  variant = "default",
}: {
  children: React.ReactNode;
  variant?: "emerald" | "blue" | "amber" | "rose" | "indigo" | "default";
}) {
  const map = {
    emerald: "bg-emerald-400/10 text-emerald-400 border-emerald-400/20",
    blue: "bg-blue-400/10 text-blue-400 border-blue-400/20",
    amber: "bg-amber-400/10 text-amber-400 border-amber-400/20",
    rose: "bg-rose-400/10 text-rose-400 border-rose-400/20",
    indigo: "bg-indigo-400/10 text-indigo-400 border-indigo-400/20",
    default: "bg-slate-400/10 text-slate-400 border-slate-400/20",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        map[variant]
      )}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  loading,
  disabled,
  variant = "primary",
  size = "md",
  className,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  loading?: boolean;
  disabled?: boolean;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const variants = {
    primary: "bg-[var(--accent)] text-black hover:bg-emerald-300 shadow-[0_0_20px_rgba(0,229,160,0.3)]",
    secondary: "bg-[var(--surface-2)] text-[var(--text)] border border-[var(--border-bright)] hover:border-[var(--accent)]/40",
    danger: "bg-rose-500/10 text-rose-400 border border-rose-500/20 hover:bg-rose-500/20",
    ghost: "text-[var(--muted)] hover:text-[var(--text)] hover:bg-[var(--surface-2)]",
  };
  const sizes = { sm: "px-3 py-1.5 text-xs", md: "px-4 py-2 text-sm", lg: "px-6 py-3 text-base" };
  return (
    <button
      onClick={onClick}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center gap-2 rounded-xl font-semibold transition-all duration-200",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        variants[variant],
        sizes[size],
        className
      )}
    >
      {loading && (
        <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      )}
      {children}
    </button>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("rounded-lg shimmer", className)} />;
}
