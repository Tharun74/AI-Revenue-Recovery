export interface HealthResponse {
  status: string;
  batch_file_present: boolean;
  razorpay_configured: boolean;
  llm_configured: boolean;
  llm_unavailable_reason: string;
  policy: { max_retry_attempts: number; retry_cooldown_hours: number };
  last_run_id: string | null;
}

export type ActionType =
  | "send_alt_payment_link"
  | "retry_same_method"
  | "escalate_to_human"
  | "stop_no_action";

export type Recoverability = "recoverable" | "unrecoverable" | "unknown";
export type DiagnosisSource = "gate" | "rule_fallback" | "llm" | "llm_cache";
export type OutcomeStatus = "pending" | "modeled_success" | "modeled_failure" | "verified_api" | "failed_api" | "skipped";

export interface FailedPayment {
  payment_id: string;
  order_id: string;
  customer_id: string;
  customer_name: string;
  customer_email: string;
  customer_phone: string;
  amount_inr: number;
  amount_paise: number;
  currency: string;
  failure_reason: string;
  raw_failure_reason: string;
  error_description: string;
  created_at: string;
  retry_count: number;
  last_attempt_at: string | null;
  recoverability: { value: Recoverability };
  has_valid_email: boolean;
  has_valid_phone: boolean;
  data_warnings: string[];
}

export interface Diagnosis {
  payment_id: string;
  reason: string;
  recoverability: Recoverability;
  root_cause: string;
  likely_transient: boolean;
  confidence: number;
  suggested_action: ActionType | null;
  source: DiagnosisSource;
  model: string | null;
  llm_raw: string | null;
  boundary_violations: string[];
  diagnosed_at: string;
}

export interface Decision {
  payment_id: string;
  action: ActionType;
  rule: string;
  reason: string;
  effective_recoverability: Recoverability;
}

export interface Outcome {
  payment_id: string;
  status: OutcomeStatus;
  provider_object: string;
  provider_ref: string;
  provider_short_url: string;
  amount_paise: number;
  dry_run: boolean;
  error: string;
}

export interface CaseRecord {
  case: FailedPayment;
  diagnosis: Diagnosis;
  decision: Decision;
  outcome: Outcome;
}

export interface RecoverySummary {
  verified_cases: number;
  verified_inr: number;
  modeled_cases: number;
  modeled_inr: number;
  verified_rate_pct: number;
  modeled_rate_pct: number;
}

export interface RestraintSummary {
  gated_cases: number;
  gated_inr: number;
  withheld_cases: number;
  withheld_inr: number;
  escalated_cases: number;
  escalated_inr: number;
  stop_compliance_pct: number;
}

export interface MetricsReport {
  run_id: string;
  generated_at: string;
  dry_run: boolean;
  rows_read: number;
  cases_detected: number;
  cases_recoverable: number;
  cases_unrecoverable: number;
  cases_unknown: number;
  total_at_risk_inr: number;
  cases_attempted: number;
  amount_attempted_inr: number;
  real_contacts_made: number;
  recovery: RecoverySummary;
  restraint: RestraintSummary;
  boundary_violations_refused: number;
  llm_calls_made: number;
  llm_cache_hits: number;
  diagnosis_sources: Record<string, number>;
  action_counts: Record<string, number>;
  rule_counts: Record<string, number>;
  all_invariants_hold: boolean;
  reconciliation: Record<string, boolean>;
  exceptions: unknown[];
}

export interface AgentRun {
  run_id: string;
  started_at: string;
  finished_at: string;
  dry_run: boolean;
  records: CaseRecord[];
  metrics: MetricsReport;
}

export interface AuditEvent {
  seq: number;
  run_id: string;
  payment_id: string;
  stage: string;
  summary: string;
  detail: Record<string, unknown>;
  recorded_at: string;
  prev_hash: string;
  hash: string;
}

export interface AuditVerification {
  ok: boolean;
  entries: number;
  first_broken_seq: number | null;
  message: string;
}

export interface DetectionBatch {
  detected_at: string;
  source: string;
  rows_read: number;
  cases: FailedPayment[];
  rejected: RejectedRecord[];
  summary: DetectionSummary;
}

export interface RejectedRecord {
  row_number: number;
  raw_row: Record<string, string>;
  reason: string;
}

export interface DetectionSummary {
  total_cases: number;
  total_at_risk_inr: number;
  recoverable_cases: number;
  unrecoverable_cases: number;
  unknown_cases: number;
  by_reason: Record<string, { count: number; amount_inr: number }>;
}

export interface PaymentLink {
  payment_id: string;
  amount_inr: number;
  action: ActionType;
  status: OutcomeStatus;
  provider_object: string;
  provider_ref: string;
  short_url: string;
  verifiable: boolean;
}
