"use client";
import type { CaseRecord } from "@/lib/types";
import { actionColor, actionLabel, formatINR, outcomeLabel } from "@/lib/utils";
import { Badge } from "./ui";
import { useState } from "react";
import { ChevronDown, ChevronUp, Brain, ShieldAlert } from "lucide-react";

type Filter = "all" | "send_alt_payment_link" | "retry_same_method" | "escalate_to_human" | "stop_no_action";

interface CasesTableProps {
  records: CaseRecord[];
}

export function CasesTable({ records }: CasesTableProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 10;

  const filtered =
    filter === "all" ? records : records.filter((r) => r.decision.action === filter);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);

  const filters: { value: Filter; label: string }[] = [
    { value: "all", label: "All" },
    { value: "send_alt_payment_link", label: "Payment Link" },
    { value: "retry_same_method", label: "Retry" },
    { value: "escalate_to_human", label: "Escalated" },
    { value: "stop_no_action", label: "Stopped" },
  ];

  return (
    <div className="flex flex-col gap-4">
      {/* Filter bar */}
      <div className="flex items-center gap-2 flex-wrap">
        {filters.map((f) => (
          <button
            key={f.value}
            onClick={() => { setFilter(f.value); setPage(0); }}
            className={`rounded-full px-3 py-1.5 text-xs font-medium border transition-all duration-200 ${
              filter === f.value
                ? "bg-[var(--accent)] text-black border-[var(--accent)] shadow-[0_0_12px_rgba(0,229,160,0.2)]"
                : "border-[var(--border)] text-[var(--muted)] hover:text-white hover:border-[var(--border-bright)]"
            }`}
          >
            {f.label}
            <span className="ml-1.5 opacity-60">
              {f.value === "all" ? records.length : records.filter((r) => r.decision.action === f.value).length}
            </span>
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-2xl border border-[var(--border)]">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] bg-[var(--surface-2)]">
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Payment ID</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Amount</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Failure</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Action</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Outcome</th>
              <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-[var(--muted)]">Diag. Source</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {paged.length === 0 && (
              <tr>
                <td colSpan={7} className="text-center py-8 text-[var(--muted)] text-sm">No cases match this filter.</td>
              </tr>
            )}
            {paged.map((rec) => {
              const isOpen = expanded === rec.case.payment_id;
              const color = actionColor(rec.decision.action) as "emerald" | "blue" | "amber" | "rose" | "indigo";
              return (
                <>
                  <tr
                    key={rec.case.payment_id}
                    className={`border-b border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--surface-2)] transition-colors cursor-pointer ${isOpen ? "bg-[var(--surface-2)]" : ""}`}
                    onClick={() => setExpanded(isOpen ? null : rec.case.payment_id)}
                  >
                    <td className="px-4 py-3 font-mono text-xs text-slate-300">{rec.case.payment_id}</td>
                    <td className="px-4 py-3 font-semibold text-white">{formatINR(rec.case.amount_inr)}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs text-slate-400">{rec.case.failure_reason}</span>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant={color}>{actionLabel(rec.decision.action)}</Badge>
                    </td>
                    <td className="px-4 py-3">
                      <OutcomeBadge status={rec.outcome.status} />
                    </td>
                    <td className="px-4 py-3">
                      <DiagSourceBadge source={rec.diagnosis.source} />
                    </td>
                    <td className="px-4 py-3 text-[var(--muted)]">
                      {isOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    </td>
                  </tr>

                  {isOpen && (
                    <tr key={`${rec.case.payment_id}-detail`} className="bg-[var(--surface-2)]">
                      <td colSpan={7} className="px-4 py-4">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                          <div className="flex flex-col gap-2">
                            <p className="font-semibold text-slate-300 flex items-center gap-1.5">
                              <Brain className="w-3.5 h-3.5 text-indigo-400" /> Diagnosis
                            </p>
                            <p className="text-slate-400 leading-relaxed">{rec.diagnosis.root_cause}</p>
                            <p className="text-[var(--muted)]">
                              Confidence: <span className="text-slate-300">{(rec.diagnosis.confidence * 100).toFixed(0)}%</span>
                              {" · "}Transient: <span className="text-slate-300">{rec.diagnosis.likely_transient ? "Yes" : "No"}</span>
                            </p>
                            {rec.diagnosis.boundary_violations.length > 0 && (
                              <div className="rounded-lg bg-amber-400/5 border border-amber-400/20 p-2">
                                <p className="text-amber-400 font-semibold mb-1 flex items-center gap-1">
                                  <ShieldAlert className="w-3.5 h-3.5" /> Boundary violations
                                </p>
                                {rec.diagnosis.boundary_violations.map((v, i) => (
                                  <p key={i} className="text-amber-300/80">{v}</p>
                                ))}
                              </div>
                            )}
                          </div>
                          <div className="flex flex-col gap-2">
                            <p className="font-semibold text-slate-300">Decision Rule</p>
                            <p className="font-mono text-indigo-400">{rec.decision.rule}</p>
                            <p className="text-slate-400">{rec.decision.reason}</p>
                            {rec.outcome.provider_short_url && (
                              <a
                                href={rec.outcome.provider_short_url}
                                target="_blank"
                                rel="noreferrer"
                                className="text-[var(--accent)] hover:underline font-medium mt-1"
                                onClick={(e) => e.stopPropagation()}
                              >
                                → Open Payment Link
                              </a>
                            )}
                            {rec.case.data_warnings.length > 0 && (
                              <div className="rounded-lg bg-amber-400/5 border border-amber-400/20 p-2 mt-1">
                                <p className="text-amber-400 font-semibold mb-1">Data warnings</p>
                                {rec.case.data_warnings.map((w, i) => (
                                  <p key={i} className="text-amber-300/80">{w}</p>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-xs text-[var(--muted)]">
          <span>
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)} of {filtered.length}
          </span>
          <div className="flex gap-2">
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
              className="px-3 py-1.5 rounded-lg border border-[var(--border)] disabled:opacity-30 hover:border-[var(--border-bright)] transition-colors"
            >
              ← Prev
            </button>
            <button
              disabled={page >= totalPages - 1}
              onClick={() => setPage((p) => p + 1)}
              className="px-3 py-1.5 rounded-lg border border-[var(--border)] disabled:opacity-30 hover:border-[var(--border-bright)] transition-colors"
            >
              Next →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function OutcomeBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    pending: { label: "Pending", cls: "text-slate-400" },
    modeled_success: { label: "Modeled ✓", cls: "text-indigo-400" },
    modeled_failure: { label: "Modeled ✗", cls: "text-slate-500" },
    verified_api: { label: "Verified ✓", cls: "text-emerald-400 font-bold" },
    failed_api: { label: "API Error", cls: "text-rose-400" },
    skipped: { label: "Skipped", cls: "text-slate-500" },
  };
  const s = map[status] ?? { label: status, cls: "text-slate-400" };
  return <span className={`text-xs ${s.cls}`}>{s.label}</span>;
}

function DiagSourceBadge({ source }: { source: string }) {
  const map: Record<string, string> = {
    gate: "text-rose-400",
    rule_fallback: "text-amber-400",
    llm: "text-indigo-400",
    llm_cache: "text-violet-400",
  };
  return (
    <span className={`text-xs font-mono ${map[source] ?? "text-slate-400"}`}>{source}</span>
  );
}
