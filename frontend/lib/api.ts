import type {
  AgentRun,
  AuditEvent,
  AuditVerification,
  DetectionBatch,
  FailedPayment,
  HealthResponse,
  MetricsReport,
  CaseRecord,
  PaymentLink,
  RejectedRecord,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function get<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(BASE + path);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const res = await fetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}: ${await res.text()}`);
  return res.json();
}

async function post<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(BASE + path);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const res = await fetch(url.toString(), {
    method: "POST",
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`POST ${path} → ${res.status}: ${await res.text()}`);
  return res.json();
}

export const api = {
  health: () => get<HealthResponse>("/health"),

  detect: {
    run: () => get<DetectionBatch>("/detect/run"),
    summary: () => get<{ detected_at: string; source: string; rows_read: number; summary: DetectionBatch["summary"] }>("/detect/summary"),
    rejected: () => get<RejectedRecord[]>("/detect/rejected"),
    unrecoverable: () => get<FailedPayment[]>("/detect/unrecoverable"),
  },

  agent: {
    run: (opts: { dry_run?: boolean; use_llm?: boolean; limit?: number; settle?: boolean } = {}) =>
      post<AgentRun>("/agent/run", {
        dry_run: opts.dry_run ?? true,
        use_llm: opts.use_llm ?? true,
        settle: opts.settle ?? true,
        ...(opts.limit !== undefined ? { limit: opts.limit } : {}),
      }),
    decisions: (action?: string) =>
      get<CaseRecord[]>("/agent/decisions", action ? { action } : undefined),
    stopped: () => get<CaseRecord[]>("/agent/stopped"),
    escalations: () => get<CaseRecord[]>("/agent/escalations"),
    links: () => get<PaymentLink[]>("/agent/links"),
    reconcile: (run_id?: string) =>
      post<unknown>("/agent/reconcile", run_id ? { run_id } : undefined),
  },

  metrics: {
    get: () => get<MetricsReport>("/metrics"),
    text: () =>
      fetch(BASE + "/metrics/text", { cache: "no-store" }).then((r) => r.text()),
  },

  audit: {
    list: (params?: { run_id?: string; limit?: number }) =>
      get<AuditEvent[]>("/audit", params as Record<string, string | number>),
    verify: () => get<AuditVerification>("/audit/verify"),
    runs: () => get<{ runs: string[] }>("/audit/runs"),
  },

  payments: {
    list: (limit = 100) => get<FailedPayment[]>("/payments", { limit }),
    summary: () => get<unknown>("/payments/summary"),
  },
};
