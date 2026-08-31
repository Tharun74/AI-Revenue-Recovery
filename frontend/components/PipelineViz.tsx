"use client";
import { cn } from "@/lib/utils";
import { CheckCircle2, ChevronRight } from "lucide-react";

const STEPS = [
  {
    id: "detect",
    label: "Detect",
    desc: "Normalize, validate, gate",
    color: "from-cyan-500 to-blue-500",
  },
  {
    id: "diagnose",
    label: "Diagnose",
    desc: "Fireworks LLM · one-way ratchet",
    color: "from-violet-500 to-indigo-500",
  },
  {
    id: "decide",
    label: "Decide",
    desc: "8 ordered deterministic rules",
    color: "from-blue-500 to-indigo-500",
  },
  {
    id: "act",
    label: "Act",
    desc: "Razorpay link · order · human",
    color: "from-emerald-500 to-teal-500",
  },
  {
    id: "stop",
    label: "Stop",
    desc: "Hard gate · 100% compliant",
    color: "from-rose-500 to-pink-500",
  },
];

export function PipelineViz({ activeStep }: { activeStep?: string }) {
  return (
    <div className="w-full overflow-x-auto">
      <div className="flex items-stretch gap-0 min-w-max mx-auto w-full">
        {STEPS.map((step, i) => {
          const active = activeStep === step.id || activeStep === "all";
          return (
            <div key={step.id} className="flex items-center flex-1">
              <div
                className={cn(
                  "flex-1 rounded-2xl border p-4 transition-all duration-500 relative overflow-hidden",
                  "bg-[var(--surface)] border-[var(--border)]",
                  active && "border-transparent shadow-lg"
                )}
              >
                {active && (
                  <div
                    className={cn(
                      "absolute inset-0 opacity-10 bg-gradient-to-br",
                      step.color
                    )}
                  />
                )}
                <div className="relative z-10">
                  <div className="flex items-center gap-2 mb-1">
                    <div
                      className={cn(
                        "w-6 h-6 rounded-lg text-[10px] font-bold flex items-center justify-center",
                        active
                          ? `bg-gradient-to-br ${step.color} text-white`
                          : "bg-[var(--surface-2)] text-[var(--muted)]"
                      )}
                    >
                      {active ? <CheckCircle2 className="w-3.5 h-3.5" /> : i + 1}
                    </div>
                    <span
                      className={cn(
                        "text-xs font-bold uppercase tracking-wider",
                        active ? "text-white" : "text-[var(--muted)]"
                      )}
                    >
                      {step.label}
                    </span>
                  </div>
                  <p className="text-[10px] text-[var(--muted)] leading-tight">{step.desc}</p>
                </div>
              </div>

              {i < STEPS.length - 1 && (
                <div className="flex-shrink-0 px-1">
                  <ChevronRight
                    className={cn(
                      "w-4 h-4 transition-colors duration-300",
                      active ? "text-[var(--accent)]" : "text-[var(--border-bright)]"
                    )}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
