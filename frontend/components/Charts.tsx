"use client";
import type { MetricsReport } from "@/lib/types";
import {
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { actionLabel } from "@/lib/utils";

const ACTION_COLORS: Record<string, string> = {
  send_alt_payment_link: "#00e5a0",
  retry_same_method: "#60a5fa",
  escalate_to_human: "#fbbf24",
  stop_no_action: "#fb7185",
};

const DIAGNOSIS_COLORS: Record<string, string> = {
  llm: "#818cf8",
  llm_cache: "#a78bfa",
  rule_fallback: "#fbbf24",
  gate: "#fb7185",
};

interface ChartsProps {
  metrics: MetricsReport;
}

export function Charts({ metrics: m }: ChartsProps) {
  const actionData = Object.entries(m.action_counts)
    .filter(([, v]) => v > 0)
    .map(([key, count]) => ({
      name: actionLabel(key),
      value: count,
      fill: ACTION_COLORS[key] ?? "#64748b",
    }));

  const diagData = Object.entries(m.diagnosis_sources)
    .filter(([, v]) => v > 0)
    .map(([key, count]) => ({
      name: key,
      count,
      fill: DIAGNOSIS_COLORS[key] ?? "#64748b",
    }));

  const ruleData = Object.entries(m.rule_counts)
    .filter(([, v]) => v > 0)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 8)
    .map(([rule, count]) => ({
      rule: rule.replace(/_/g, " ").replace("escalate ", "").replace("stop ", "").replace("retry ", "").replace("link ", ""),
      count,
    }));

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {/* Action distribution donut */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-[var(--muted)] mb-4">Action Distribution</h3>
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie
              data={actionData}
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={80}
              paddingAngle={3}
              dataKey="value"
            >
              {actionData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{ background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 11 }}
            />
            <Legend
              iconType="circle"
              iconSize={8}
              wrapperStyle={{ fontSize: 10 }}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>

      {/* Diagnosis sources */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-[var(--muted)] mb-4">Diagnosis Sources</h3>
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie
              data={diagData}
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={80}
              paddingAngle={3}
              dataKey="count"
            >
              {diagData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{ background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 11 }}
            />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 10 }} />
          </PieChart>
        </ResponsiveContainer>
      </div>

      {/* Rules fired */}
      <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
        <h3 className="text-xs font-semibold uppercase tracking-widest text-[var(--muted)] mb-4">Rules Fired</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={ruleData} layout="vertical" margin={{ left: -10 }}>
            <XAxis type="number" tick={{ fontSize: 10, fill: "#64748b" }} />
            <YAxis
              type="category"
              dataKey="rule"
              tick={{ fontSize: 9, fill: "#94a3b8" }}
              width={95}
            />
            <Tooltip
              contentStyle={{ background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 11 }}
            />
            <Bar dataKey="count" fill="#6366f1" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
