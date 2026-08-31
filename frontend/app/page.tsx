"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AgentRun, AuditEvent, AuditVerification, HealthResponse } from "@/lib/types";

import { Header } from "@/components/Header";
import { PipelineViz } from "@/components/PipelineViz";
import { AgentRunner } from "@/components/AgentRunner";
import { MetricsGrid } from "@/components/MetricsGrid";
import { CasesTable } from "@/components/CasesTable";
import { AuditTrail } from "@/components/AuditTrail";
import { Charts } from "@/components/Charts";
import { Card, Button, Skeleton } from "@/components/ui";

import {
  LayoutDashboard,
  TableProperties,
  ShieldCheck,
  Play,
  RefreshCw,
  ExternalLink,
  FileText,
  AlertCircle,
} from "lucide-react";

type Tab = "dashboard" | "cases" | "audit" | "run";

export default function Home() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [auditVerify, setAuditVerify] = useState<AuditVerification | null>(null);
  const [metricsText, setMetricsText] = useState<string>("");
  const [loadingAudit, setLoadingAudit] = useState(false);

  /* ── Health polling ── */
  const fetchHealth = useCallback(async () => {
    try {
      const h = await api.health();
      setHealth(h);
      setHealthError(false);
    } catch {
      setHealthError(true);
    }
  }, []);

  useEffect(() => {
    fetchHealth();
    const id = setInterval(fetchHealth, 8000);
    return () => clearInterval(id);
  }, [fetchHealth]);

  /* ── After run ── */
  const handleRunComplete = useCallback(async (newRun: AgentRun) => {
    setRun(newRun);
    setTab("dashboard");
    fetchAudit(newRun.run_id);
    try {
      const t = await api.metrics.text();
      setMetricsText(t);
    } catch { /* non-fatal */ }
  }, []);

  const fetchAudit = useCallback(async (runId?: string) => {
    setLoadingAudit(true);
    try {
      const [events, verify] = await Promise.all([
        api.audit.list({ run_id: runId, limit: 200 }),
        api.audit.verify(),
      ]);
      setAuditEvents(events);
      setAuditVerify(verify);
    } catch { /* non-fatal */ } finally {
      setLoadingAudit(false);
    }
  }, []);

  /* ── Load audit on tab switch ── */
  useEffect(() => {
    if (tab === "audit") fetchAudit(run?.run_id);
  }, [tab, run?.run_id, fetchAudit]);

  const navItems: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "dashboard", label: "Dashboard", icon: <LayoutDashboard className="w-4 h-4" /> },
    { id: "run", label: "Run Agent", icon: <Play className="w-4 h-4" /> },
    { id: "cases", label: "Cases", icon: <TableProperties className="w-4 h-4" /> },
    { id: "audit", label: "Audit Trail", icon: <ShieldCheck className="w-4 h-4" /> },
  ];

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg)" }}>
      <Header
        healthy={healthError ? false : health ? health.status === "ok" : null}
        llmConfigured={health?.llm_configured ?? false}
        razorpayConfigured={health?.razorpay_configured ?? false}
        lastRunId={health?.last_run_id ?? null}
      />

      <div className="flex flex-1 mx-auto w-full max-w-screen-xl px-4 pt-6 pb-16 gap-6">
        {/* Sidebar nav */}
        <nav className="hidden md:flex flex-col gap-1 w-44 flex-shrink-0 pt-1">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 text-left ${
                tab === item.id
                  ? "bg-[var(--accent)]/10 text-[var(--accent)] border border-[var(--accent)]/20"
                  : "text-[var(--muted)] hover:text-white hover:bg-[var(--surface)]"
              }`}
            >
              {item.icon}
              {item.label}
            </button>
          ))}

          <div className="mt-4 pt-4 border-t border-[var(--border)]">
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium text-[var(--muted)] hover:text-white hover:bg-[var(--surface)] transition-all duration-200"
            >
              <ExternalLink className="w-4 h-4" />
              API Docs
            </a>
          </div>

          {/* Policy card */}
          {health?.policy && (
            <div className="mt-4 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
              <p className="text-[9px] font-semibold uppercase tracking-widest text-[var(--muted)] mb-2">Policy</p>
              <p className="text-xs text-slate-400">
                Max retries: <span className="text-slate-200">{health.policy.max_retry_attempts}</span>
              </p>
              <p className="text-xs text-slate-400 mt-1">
                Cooldown: <span className="text-slate-200">{health.policy.retry_cooldown_hours}h</span>
              </p>
            </div>
          )}
        </nav>

        {/* Mobile nav */}
        <div className="md:hidden fixed bottom-0 left-0 right-0 z-50 border-t border-[var(--border)] backdrop-blur-xl" style={{ background: "rgba(5,12,24,0.95)" }}>
          <div className="flex">
            {navItems.map((item) => (
              <button
                key={item.id}
                onClick={() => setTab(item.id)}
                className={`flex-1 flex flex-col items-center gap-1 py-3 text-[10px] font-medium transition-colors ${
                  tab === item.id ? "text-[var(--accent)]" : "text-[var(--muted)]"
                }`}
              >
                {item.icon}
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {/* Main content */}
        <main className="flex-1 min-w-0 flex flex-col gap-6">
          {/* Pipeline strip — always visible */}
          <PipelineViz activeStep={run ? "all" : undefined} />

          {/* ── Connection error ── */}
          {healthError && (
            <div className="flex items-center gap-3 rounded-2xl border border-rose-400/20 bg-rose-400/5 p-4">
              <AlertCircle className="w-5 h-5 text-rose-400 flex-shrink-0" />
              <div>
                <p className="text-sm font-semibold text-rose-400">Cannot reach API</p>
                <p className="text-xs text-[var(--muted)] mt-0.5">
                  Start the backend with: <code className="font-mono text-slate-300">venv\Scripts\uvicorn app.main:app --reload</code>
                </p>
              </div>
              <Button variant="ghost" size="sm" onClick={fetchHealth} className="ml-auto flex-shrink-0">
                <RefreshCw className="w-3.5 h-3.5" /> Retry
              </Button>
            </div>
          )}

          {/* ── DASHBOARD ── */}
          {tab === "dashboard" && (
            <div className="flex flex-col gap-6">
              {!run ? (
                <EmptyState onRunClick={() => setTab("run")} />
              ) : (
                <>
                  <MetricsGrid metrics={run.metrics} />
                  <Charts metrics={run.metrics} />

                  {metricsText && (
                    <Card>
                      <div className="flex items-center gap-2 mb-3">
                        <FileText className="w-4 h-4 text-[var(--accent)]" />
                        <h3 className="text-sm font-bold text-white">Batch Report</h3>
                      </div>
                      <pre className="text-[11px] font-mono text-slate-300 leading-relaxed overflow-x-auto whitespace-pre">
                        {metricsText}
                      </pre>
                    </Card>
                  )}

                  {/* Stopped cases summary */}
                  {run.records.filter((r) => r.decision.action === "stop_no_action").length > 0 && (
                    <Card>
                      <div className="flex items-center gap-2 mb-4">
                        <ShieldCheck className="w-4 h-4 text-emerald-400" />
                        <h3 className="text-sm font-bold text-white">
                          Stopped Cases — Read This First
                        </h3>
                        <span className="ml-auto text-xs text-[var(--muted)]">
                          {run.records.filter((r) => r.decision.action === "stop_no_action").length} cases
                        </span>
                      </div>
                      <div className="flex flex-col gap-2">
                        {run.records
                          .filter((r) => r.decision.action === "stop_no_action")
                          .slice(0, 5)
                          .map((r) => (
                            <div
                              key={r.case.payment_id}
                              className="flex items-start justify-between gap-3 rounded-xl bg-[var(--surface-2)] border border-[var(--border)] p-3"
                            >
                              <div>
                                <p className="text-xs font-mono text-slate-400">{r.case.payment_id}</p>
                                <p className="text-xs text-slate-300 mt-0.5">{r.decision.reason}</p>
                                <p className="text-[10px] text-[var(--muted)] font-mono mt-1">{r.decision.rule}</p>
                              </div>
                              <span className="text-xs text-rose-400 font-semibold whitespace-nowrap">
                                {new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(r.case.amount_inr)}
                              </span>
                            </div>
                          ))}
                        {run.records.filter((r) => r.decision.action === "stop_no_action").length > 5 && (
                          <button
                            className="text-xs text-[var(--accent)] hover:underline text-center py-1"
                            onClick={() => setTab("cases")}
                          >
                            View all → switch to Cases tab
                          </button>
                        )}
                      </div>
                    </Card>
                  )}
                </>
              )}
            </div>
          )}

          {/* ── RUN ── */}
          {tab === "run" && (
            <AgentRunner onRunComplete={handleRunComplete} />
          )}

          {/* ── CASES ── */}
          {tab === "cases" && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-bold text-white">
                  All Cases
                  {run && <span className="text-sm font-normal text-[var(--muted)] ml-2">({run.records.length} total)</span>}
                </h2>
                {!run && (
                  <Button variant="secondary" size="sm" onClick={() => setTab("run")}>
                    <Play className="w-3.5 h-3.5" /> Run Agent first
                  </Button>
                )}
              </div>
              {run ? (
                <CasesTable records={run.records} />
              ) : (
                <EmptyState onRunClick={() => setTab("run")} />
              )}
            </div>
          )}

          {/* ── AUDIT ── */}
          {tab === "audit" && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-bold text-white">
                  Audit Trail
                  <span className="text-sm font-normal text-[var(--muted)] ml-2">append-only · hash-chained</span>
                </h2>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => fetchAudit(run?.run_id)}
                  loading={loadingAudit}
                >
                  <RefreshCw className="w-3.5 h-3.5" /> Refresh
                </Button>
              </div>
              {loadingAudit ? (
                <div className="flex flex-col gap-3">
                  {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}
                </div>
              ) : (
                <AuditTrail events={auditEvents} verification={auditVerify} />
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function EmptyState({ onRunClick }: { onRunClick: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 gap-6 text-center">
      <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-emerald-400/20 to-indigo-400/20 border border-[var(--border)] flex items-center justify-center">
        <Play className="w-8 h-8 text-[var(--accent)]" />
      </div>
      <div>
        <h2 className="text-xl font-bold text-white">No run yet</h2>
        <p className="text-sm text-[var(--muted)] mt-1 max-w-sm">
          Run the agent to see metrics, charts, case decisions, and the audit trail.
        </p>
      </div>
      <Button variant="primary" size="lg" onClick={onRunClick}>
        <Play className="w-4 h-4" /> Run Agent (Dry Run)
      </Button>

      <div className="grid grid-cols-3 gap-4 mt-4 text-center max-w-md">
        {[
          { label: "Safety First", desc: "Hard gates on fraud, blocked, cancelled" },
          { label: "LLM Bounded", desc: "Fireworks AI can narrow decisions, never widen" },
          { label: "Auditable", desc: "Every decision hash-chained, tamper-evident" },
        ].map((f) => (
          <div key={f.label} className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <p className="text-xs font-semibold text-[var(--accent)]">{f.label}</p>
            <p className="text-[10px] text-[var(--muted)] mt-1">{f.desc}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
