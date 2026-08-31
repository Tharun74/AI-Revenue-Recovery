"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import type { AgentRun } from "@/lib/types";
import { Button, Card } from "./ui";
import { Play, ToggleLeft, ToggleRight, Cpu, Zap } from "lucide-react";

interface AgentRunnerProps {
  onRunComplete: (run: AgentRun) => void;
}

export function AgentRunner({ onRunComplete }: AgentRunnerProps) {
  const [dryRun, setDryRun] = useState(true);
  const [useLlm, setUseLlm] = useState(true);
  const [limit, setLimit] = useState<number | "">("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastDuration, setLastDuration] = useState<number | null>(null);

  async function handleRun() {
    setLoading(true);
    setError(null);
    const t0 = Date.now();
    try {
      const run = await api.agent.run({
        dry_run: dryRun,
        use_llm: useLlm,
        settle: true,
        ...(limit !== "" ? { limit: Number(limit) } : {}),
      });
      setLastDuration(Date.now() - t0);
      onRunComplete(run);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card className="flex flex-col gap-5">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-[var(--accent)]/10 flex items-center justify-center">
          <Zap className="w-4 h-4 text-[var(--accent)]" />
        </div>
        <div>
          <h2 className="text-sm font-bold text-white">Run Agent</h2>
          <p className="text-[11px] text-[var(--muted)]">Execute the full detect → act pipeline</p>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {/* Dry Run toggle */}
        <Toggle
          label="Dry Run"
          description={dryRun ? "No real API calls" : "Live Razorpay calls"}
          enabled={dryRun}
          onChange={setDryRun}
          icon={dryRun ? <ToggleLeft className="w-4 h-4" /> : <ToggleRight className="w-4 h-4 text-[var(--accent)]" />}
          safeWhenOn
        />

        {/* LLM toggle */}
        <Toggle
          label="Fireworks LLM"
          description={useLlm ? "AI diagnosis active" : "Rule-based fallback"}
          enabled={useLlm}
          onChange={setUseLlm}
          icon={<Cpu className={`w-4 h-4 ${useLlm ? "text-indigo-400" : ""}`} />}
        />

        {/* Limit */}
        <div className="rounded-xl border border-[var(--border)] bg-[var(--surface-2)] p-3 flex flex-col gap-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Case Limit</span>
          <input
            type="number"
            min={1}
            max={1000}
            placeholder="All cases"
            value={limit}
            onChange={(e) => setLimit(e.target.value === "" ? "" : Number(e.target.value))}
            className="bg-transparent text-sm text-white outline-none placeholder:text-[var(--muted)] w-full"
          />
          <span className="text-[10px] text-[var(--muted)]">Leave blank for full batch</span>
        </div>
      </div>

      {!dryRun && (
        <div className="flex items-start gap-2 rounded-xl bg-amber-400/5 border border-amber-400/20 p-3">
          <span className="text-amber-400 text-sm mt-0.5">⚠</span>
          <p className="text-xs text-amber-300">
            <strong>Live mode:</strong> This will create real Razorpay test-mode payment links and
            orders. Customers will not be contacted, but API objects will be created.
          </p>
        </div>
      )}

      {error && (
        <div className="rounded-xl bg-rose-400/5 border border-rose-400/20 p-3">
          <p className="text-xs text-rose-400 font-mono">{error}</p>
        </div>
      )}

      <div className="flex items-center gap-3">
        <Button
          onClick={handleRun}
          loading={loading}
          variant="primary"
          size="lg"
          className="flex-1 justify-center"
        >
          <Play className="w-4 h-4" />
          {loading ? "Running pipeline…" : "Run Agent"}
        </Button>
        {lastDuration && (
          <span className="text-xs text-[var(--muted)]">
            Completed in {(lastDuration / 1000).toFixed(1)}s
          </span>
        )}
      </div>
    </Card>
  );
}

function Toggle({
  label,
  description,
  enabled,
  onChange,
  icon,
  safeWhenOn,
}: {
  label: string;
  description: string;
  enabled: boolean;
  onChange: (v: boolean) => void;
  icon?: React.ReactNode;
  safeWhenOn?: boolean;
}) {
  return (
    <button
      onClick={() => onChange(!enabled)}
      className={`rounded-xl border p-3 flex flex-col gap-1.5 text-left transition-all duration-200 ${
        enabled
          ? safeWhenOn
            ? "border-emerald-400/30 bg-emerald-400/5"
            : "border-indigo-400/30 bg-indigo-400/5"
          : "border-[var(--border)] bg-[var(--surface-2)]"
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">{label}</span>
        {icon}
      </div>
      <span className="text-xs text-slate-300">{description}</span>
    </button>
  );
}
