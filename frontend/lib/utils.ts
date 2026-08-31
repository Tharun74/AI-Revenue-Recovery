import { type ClassValue, clsx } from "clsx";

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

export function formatINR(amount: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

export function formatPct(value: number, decimals = 1): string {
  return `${value.toFixed(decimals)}%`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function actionLabel(action: string): string {
  const map: Record<string, string> = {
    send_alt_payment_link: "Payment Link",
    retry_same_method: "Retry",
    escalate_to_human: "Escalated",
    stop_no_action: "Stopped",
  };
  return map[action] ?? action;
}

export function actionColor(action: string): string {
  const map: Record<string, string> = {
    send_alt_payment_link: "emerald",
    retry_same_method: "blue",
    escalate_to_human: "amber",
    stop_no_action: "rose",
  };
  return map[action] ?? "slate";
}

export function outcomeLabel(status: string): string {
  const map: Record<string, string> = {
    pending: "Pending",
    modeled_success: "Modeled ✓",
    modeled_failure: "Modeled ✗",
    verified_api: "Verified ✓",
    failed_api: "API Error",
    skipped: "Skipped",
  };
  return map[status] ?? status;
}
