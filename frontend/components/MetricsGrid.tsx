"use client";
import { formatINR, formatPct } from "@/lib/utils";
import type { MetricsReport } from "@/lib/types";
import { StatCard } from "./ui";
import {
  TrendingUp,
  ShieldCheck,
  AlertTriangle,
  BarChart3,
  Banknote,
  Brain,
} from "lucide-react";

interface MetricsGridProps {
  metrics: MetricsReport;
}

export function MetricsGrid({ metrics: m }: MetricsGridProps) {
  const r = m.recovery;
  const rest = m.restraint;

  return (
    <div className="flex flex-col gap-4">
      {/* Row 1 — Headline numbers */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="At Risk"
          value={formatINR(m.total_at_risk_inr)}
          sub={`${m.cases_detected} cases`}
          icon={<BarChart3 className="w-4 h-4" />}
          accent="default"
        />
        <StatCard
          label="Recoverable"
          value={m.cases_recoverable}
          sub={formatINR(m.amount_attempted_inr) + " attempted"}
          icon={<TrendingUp className="w-4 h-4" />}
          accent="blue"
        />
        <StatCard
          label="Stop Compliance"
          value={formatPct(rest.stop_compliance_pct)}
          sub={`${rest.gated_cases} gated cases`}
          icon={<ShieldCheck className="w-4 h-4" />}
          accent={rest.stop_compliance_pct >= 100 ? "emerald" : "rose"}
        />
        <StatCard
          label="All Invariants"
          value={m.all_invariants_hold ? "✓ Hold" : "✗ Failed"}
          sub={`${Object.values(m.reconciliation).filter(Boolean).length} / ${Object.keys(m.reconciliation).length} checks`}
          icon={<ShieldCheck className="w-4 h-4" />}
          accent={m.all_invariants_hold ? "emerald" : "rose"}
        />
      </div>

      {/* Row 2 — Recovery breakdown (NEVER summed) */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
        <div className="flex items-center gap-2 mb-4">
          <Banknote className="w-4 h-4 text-[var(--accent)]" />
          <h3 className="text-sm font-bold text-white">Recovery — Verified and Modeled are never added together</h3>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <RecoveryColumn
            label="Verified (Razorpay confirmed)"
            cases={r.verified_cases}
            inr={r.verified_inr}
            rate={r.verified_rate_pct}
            color="emerald"
            tooltip="Only payment links confirmed paid via /agent/reconcile"
          />
          <RecoveryColumn
            label="Modeled (seeded estimate)"
            cases={r.modeled_cases}
            inr={r.modeled_inr}
            rate={r.modeled_rate_pct}
            color="indigo"
            tooltip="Probability model — stated assumption, not measurement"
          />
        </div>
      </div>

      {/* Row 3 — Restraint + LLM */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Gated (not chased)"
          value={formatINR(rest.gated_inr)}
          sub="Deliberately withheld"
          icon={<ShieldCheck className="w-4 h-4" />}
          accent="rose"
        />
        <StatCard
          label="Withheld this run"
          value={formatINR(rest.withheld_inr)}
          sub="Cooldown / retry cap"
          icon={<AlertTriangle className="w-4 h-4" />}
          accent="amber"
        />
        <StatCard
          label="Escalated to Human"
          value={rest.escalated_cases}
          sub={formatINR(rest.escalated_inr)}
          icon={<AlertTriangle className="w-4 h-4" />}
          accent="amber"
        />
        <StatCard
          label="LLM Overreach Refused"
          value={m.boundary_violations_refused}
          sub={`${m.llm_calls_made} LLM calls · ${m.llm_cache_hits} cached`}
          icon={<Brain className="w-4 h-4" />}
          accent="indigo"
        />
      </div>
    </div>
  );
}

function RecoveryColumn({
  label,
  cases,
  inr,
  rate,
  color,
  tooltip,
}: {
  label: string;
  cases: number;
  inr: number;
  rate: number;
  color: "emerald" | "indigo";
  tooltip: string;
}) {
  const cls = color === "emerald"
    ? { bar: "bg-emerald-400", text: "text-emerald-400", bg: "bg-emerald-400/10 border-emerald-400/20" }
    : { bar: "bg-indigo-400", text: "text-indigo-400", bg: "bg-indigo-400/10 border-indigo-400/20" };

  return (
    <div className={`rounded-xl border p-4 flex flex-col gap-3 ${cls.bg}`} title={tooltip}>
      <p className="text-xs text-[var(--muted)] font-medium">{label}</p>
      <p className={`text-2xl font-bold ${cls.text}`}>{formatINR(inr)}</p>
      <p className="text-sm text-slate-400">{cases} cases · {formatPct(rate)} rate</p>
      <div className="h-1.5 rounded-full bg-[var(--surface-2)] overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-700 ${cls.bar}`}
          style={{ width: `${Math.min(rate, 100)}%` }}
        />
      </div>
    </div>
  );
}
