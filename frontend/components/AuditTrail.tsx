"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import type { AuditEvent, AuditVerification } from "@/lib/types";
import { formatDate } from "@/lib/utils";
import { Card, Button, Badge } from "./ui";
import { ShieldCheck, ShieldAlert, Hash, RefreshCw } from "lucide-react";

interface AuditTrailProps {
  events: AuditEvent[];
  verification: AuditVerification | null;
}

const stageColor: Record<string, "emerald" | "blue" | "amber" | "rose" | "indigo"> = {
  detect: "blue",
  diagnose: "indigo",
  decide: "amber",
  act: "emerald",
  stop: "rose",
};

export function AuditTrail({ events, verification }: AuditTrailProps) {
  const [expanded, setExpanded] = useState<number | null>(null);

  return (
    <div className="flex flex-col gap-4">
      {/* Verification banner */}
      {verification && (
        <div
          className={`flex items-center gap-3 rounded-2xl border p-4 ${
            verification.ok
              ? "bg-emerald-400/5 border-emerald-400/20"
              : "bg-rose-400/5 border-rose-400/20"
          }`}
        >
          {verification.ok ? (
            <ShieldCheck className="w-5 h-5 text-emerald-400 flex-shrink-0" />
          ) : (
            <ShieldAlert className="w-5 h-5 text-rose-400 flex-shrink-0" />
          )}
          <div>
            <p className={`text-sm font-semibold ${verification.ok ? "text-emerald-400" : "text-rose-400"}`}>
              {verification.ok ? "Audit chain intact" : "Chain broken!"}
            </p>
            <p className="text-xs text-[var(--muted)]">
              {verification.entries} entries · {verification.message}
              {!verification.ok && verification.first_broken_seq !== null &&
                ` · Broken at seq #${verification.first_broken_seq}`}
            </p>
          </div>
        </div>
      )}

      {/* Events timeline */}
      <div className="relative">
        <div className="absolute left-6 top-0 bottom-0 w-px bg-[var(--border)]" />
        <div className="flex flex-col gap-1">
          {events.map((ev) => {
            const color = stageColor[ev.stage] ?? "default";
            const isOpen = expanded === ev.seq;
            return (
              <div
                key={ev.seq}
                className={`relative pl-14 pr-4 py-3 rounded-xl border transition-all duration-200 cursor-pointer ${
                  isOpen
                    ? "border-[var(--border-bright)] bg-[var(--surface-2)]"
                    : "border-transparent hover:border-[var(--border)] hover:bg-[var(--surface)]"
                }`}
                onClick={() => setExpanded(isOpen ? null : ev.seq)}
              >
                {/* Timeline dot */}
                <div className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 rounded-full border-2 border-[var(--bg)] bg-[var(--surface-2)] flex items-center justify-center">
                  <div
                    className={`w-1.5 h-1.5 rounded-full ${
                      color === "emerald" ? "bg-emerald-400" :
                      color === "blue" ? "bg-blue-400" :
                      color === "amber" ? "bg-amber-400" :
                      color === "rose" ? "bg-rose-400" :
                      color === "indigo" ? "bg-indigo-400" : "bg-slate-400"
                    }`}
                  />
                </div>

                <div className="flex items-start justify-between gap-3">
                  <div className="flex flex-col gap-0.5 flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge variant={color}>
                        {ev.stage}
                      </Badge>
                      <span className="font-mono text-xs text-[var(--muted)]">#{ev.seq}</span>
                      <span className="font-mono text-xs text-slate-400 truncate">{ev.payment_id}</span>
                    </div>
                    <p className="text-xs text-slate-300 mt-1 leading-relaxed">{ev.summary}</p>
                  </div>
                  <span className="text-[10px] text-[var(--muted)] whitespace-nowrap flex-shrink-0">
                    {formatDate(ev.recorded_at)}
                  </span>
                </div>

                {isOpen && (
                  <div className="mt-3 flex flex-col gap-2">
                    <div className="flex items-center gap-1.5 text-[10px] text-[var(--muted)]">
                      <Hash className="w-3 h-3" />
                      <span className="font-mono break-all">{ev.hash.slice(0, 32)}…</span>
                    </div>
                    {Object.keys(ev.detail).length > 0 && (
                      <pre className="text-[10px] text-slate-400 bg-[var(--bg)] rounded-lg p-3 overflow-x-auto max-h-48">
                        {JSON.stringify(ev.detail, null, 2)}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {events.length === 0 && (
            <p className="text-center text-sm text-[var(--muted)] py-8">
              No audit events yet. Run the agent first.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
