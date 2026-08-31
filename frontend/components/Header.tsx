"use client";
import { Activity, Zap } from "lucide-react";
import { Badge } from "./ui";

interface HeaderProps {
  healthy: boolean | null;
  llmConfigured: boolean;
  razorpayConfigured: boolean;
  lastRunId: string | null;
}

export function Header({ healthy, llmConfigured, razorpayConfigured, lastRunId }: HeaderProps) {
  return (
    <header className="sticky top-0 z-50 border-b border-[var(--border)] backdrop-blur-xl"
      style={{ background: "rgba(5,12,24,0.85)" }}>
      <div className="mx-auto max-w-screen-xl px-6 py-4 flex items-center justify-between gap-4">
        {/* Logo */}
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center shadow-[0_0_20px_rgba(0,229,160,0.4)]">
            <Zap className="w-5 h-5 text-black" fill="currentColor" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-white leading-none">Revenue Recovery Agent</h1>
            <p className="text-[10px] text-[var(--muted)] mt-0.5">Razorpay AI Buildathon · Track 03</p>
          </div>
        </div>

        {/* Status chips */}
        <div className="hidden md:flex items-center gap-2 flex-wrap">
          <IntegrationChip label="API" ok={healthy} />
          <IntegrationChip label="Fireworks LLM" ok={llmConfigured} />
          <IntegrationChip label="Razorpay" ok={razorpayConfigured} />
          {lastRunId && (
            <Badge variant="indigo">
              <Activity className="w-3 h-3" />
              {lastRunId.slice(0, 18)}…
            </Badge>
          )}
        </div>

        {/* Pipeline tag */}
        <div className="hidden lg:flex items-center gap-1.5 text-[11px] text-[var(--muted)] font-mono">
          {["detect", "diagnose", "decide", "act", "stop"].map((step, i) => (
            <span key={step} className="flex items-center gap-1.5">
              {i > 0 && <span className="text-[var(--accent)]">→</span>}
              <span className="text-slate-300">{step}</span>
            </span>
          ))}
        </div>
      </div>
    </header>
  );
}

function IntegrationChip({ label, ok }: { label: string; ok: boolean | null }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] px-2.5 py-1 text-[11px] font-medium">
      <span
        className={`w-1.5 h-1.5 rounded-full pulse-dot ${
          ok === null ? "bg-slate-500" : ok ? "bg-emerald-400" : "bg-rose-400"
        }`}
      />
      {label}
    </span>
  );
}
