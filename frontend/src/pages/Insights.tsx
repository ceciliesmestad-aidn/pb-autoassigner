import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type InsightsResult } from "../api";

type WindowOption = { label: string; days: number };

const WINDOWS: WindowOption[] = [
  { label: "1 week", days: 7 },
  { label: "1 month", days: 30 },
  { label: "3 months", days: 90 },
  { label: "6 months", days: 180 },
];

const CATEGORY_LABELS: Record<string, string> = {
  tender: "Tender",
  feedback: "Feedback",
  bug: "Bug",
  feature_request: "Feature request",
  question: "Question",
  other: "Other",
};

const CATEGORY_COLORS: Record<string, string> = {
  tender: "bg-slate-300",
  feedback: "bg-sky-500",
  bug: "bg-rose-500",
  feature_request: "bg-emerald-500",
  question: "bg-amber-400",
  other: "bg-slate-200",
};

const PROGRESS_STAGES = [
  { at: 0,  label: "Fetching notes from Productboard…" },
  { at: 8,  label: "Categorising notes with Claude…" },
  { at: 25, label: "Summarising feedback themes…" },
  { at: 45, label: "Finalising insights…" },
];

export default function Insights() {
  const qc = useQueryClient();
  const pms = useQuery({ queryKey: ["pms"], queryFn: api.pms });

  const [pmEmail, setPmEmail] = useState<string>(
    () => localStorage.getItem("pb-assigner.insights.pm") || "",
  );
  const [windowDays, setWindowDays] = useState<number>(
    () => Number(localStorage.getItem("pb-assigner.insights.window")) || 30,
  );

  useEffect(() => {
    if (!pmEmail && pms.data && pms.data.length > 0) {
      setPmEmail(pms.data[0].email);
    }
  }, [pms.data, pmEmail]);

  useEffect(() => {
    if (pmEmail) localStorage.setItem("pb-assigner.insights.pm", pmEmail);
  }, [pmEmail]);
  useEffect(() => {
    localStorage.setItem("pb-assigner.insights.window", String(windowDays));
  }, [windowDays]);

  const cacheKey = ["insights", pmEmail, windowDays] as const;

  // Result cache — survives tab navigation.
  const resultCache = useQuery<InsightsResult | null>({
    queryKey: cacheKey,
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
  });
  const result = resultCache.data ?? null;

  const generate = useMutation({
    mutationFn: () => api.insights({ pm_email: pmEmail, window_days: windowDays }),
    onSuccess: (data) => {
      qc.setQueryData(cacheKey, data);
    },
  });

  // Elapsed-time progress while generating
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!generate.isPending) { setElapsed(0); return; }
    const start = Date.now();
    const id = setInterval(() => setElapsed((Date.now() - start) / 1000), 250);
    return () => clearInterval(id);
  }, [generate.isPending]);

  const stageLabel = useMemo(() => {
    let label = PROGRESS_STAGES[0].label;
    for (const s of PROGRESS_STAGES) if (elapsed >= s.at) label = s.label;
    return label;
  }, [elapsed]);

  const selectedPM = pms.data?.find((p) => p.email === pmEmail);

  return (
    <div className="space-y-6">
      {/* ── Controls ── */}
      <div className="bg-white border border-slate-200 rounded-md p-4">
        <div className="flex flex-col sm:flex-row sm:items-end gap-4">
          <div className="flex-1 min-w-0">
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Product manager
            </label>
            <select
              value={pmEmail}
              onChange={(e) => setPmEmail(e.target.value)}
              className="input w-full"
              disabled={pms.isLoading}
            >
              {!pmEmail && <option value="">Select a PM…</option>}
              {(pms.data ?? []).map((p) => (
                <option key={p.email} value={p.email}>
                  {p.name} — {p.team}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">
              Time window
            </label>
            <div className="flex gap-1 p-0.5 rounded-md bg-slate-100 border border-slate-200">
              {WINDOWS.map((w) => (
                <button
                  key={w.days}
                  type="button"
                  onClick={() => setWindowDays(w.days)}
                  className={[
                    "text-xs px-3 py-1.5 rounded-md font-medium transition-colors",
                    windowDays === w.days
                      ? "bg-white text-slate-900 shadow-sm"
                      : "text-slate-500 hover:text-slate-700",
                  ].join(" ")}
                >
                  {w.label}
                </button>
              ))}
            </div>
          </div>

          <button
            onClick={() => generate.mutate()}
            disabled={!pmEmail || generate.isPending}
            className="px-4 py-2 text-sm rounded-md bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50 flex items-center gap-2"
          >
            {generate.isPending && <Spinner />}
            {generate.isPending ? "Generating…" : result ? "Regenerate" : "Generate insights"}
          </button>
        </div>

        {generate.isPending && (
          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-between text-xs text-slate-600">
              <span>{stageLabel}</span>
              <span className="tabular-nums text-slate-400">{Math.floor(elapsed)}s</span>
            </div>
            <div className="h-1.5 bg-slate-100 rounded overflow-hidden">
              <div
                className="h-full bg-sky-500 transition-all duration-500"
                style={{ width: `${Math.min(95, (elapsed / 60) * 100)}%` }}
              />
            </div>
          </div>
        )}

        {generate.isError && (
          <div className="mt-3 text-sm text-rose-700">
            {(generate.error as Error).message}
          </div>
        )}
      </div>

      {/* ── Empty state ── */}
      {!result && !generate.isPending && (
        <div className="bg-white border border-dashed border-slate-300 rounded-md p-10 text-center text-sm text-slate-500">
          Choose a PM and time window, then click <strong>Generate insights</strong>.
        </div>
      )}

      {result && (
        <InsightsView
          result={result}
          pmName={selectedPM?.name ?? result.pm_email}
        />
      )}
    </div>
  );
}

// ─── result view ────────────────────────────────────────────────────────────

function InsightsView({ result, pmName }: { result: InsightsResult; pmName: string }) {
  const windowLabel = WINDOWS.find((w) => w.days === result.window_days)?.label
    ?? `${result.window_days} days`;

  return (
    <div className="space-y-6">
      {/* Row 1: Note types + Frequency side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Panel title="Note types">
          <CategoryBars cats={result.notes_by_category} />
        </Panel>
        <Panel
          title="Frequency"
          subtitle={`${pmName} · ${windowLabel} · tender excluded`}
        >
          <FrequencyChart data={result.frequency} />
        </Panel>
      </div>

      {/* Row 2: Summary */}
      <Panel title="Feedback summary" subtitle="Tender notes excluded">
        <SummaryBlock text={result.summary_no} />
      </Panel>
    </div>
  );
}

// ─── sub-components ──────────────────────────────────────────────────────────

function Panel({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-md">
      <div className="px-4 py-2.5 border-b border-slate-100 flex items-baseline gap-2">
        <div className="text-sm font-medium text-slate-800">{title}</div>
        {subtitle && <div className="text-xs text-slate-400">{subtitle}</div>}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

function CategoryBars({ cats }: { cats: Record<string, number> }) {
  const order = ["feedback", "bug", "feature_request", "question", "tender", "other"];
  const entries = order
    .map((k) => [k, cats[k] ?? 0] as const)
    .filter(([, v]) => v > 0);
  if (entries.length === 0)
    return <div className="text-sm text-slate-400">No data.</div>;
  const max = Math.max(...entries.map(([, v]) => v), 1);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  return (
    <div className="space-y-2.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center gap-3">
          <div className="w-32 shrink-0 text-sm text-slate-700">
            {CATEGORY_LABELS[k] ?? k}
          </div>
          <div className="flex-1 bg-slate-100 rounded h-3 overflow-hidden">
            <div
              className={`${CATEGORY_COLORS[k] ?? "bg-slate-300"} h-full`}
              style={{ width: `${(v / max) * 100}%` }}
            />
          </div>
          <div className="w-16 text-right text-sm tabular-nums text-slate-600">
            {v}
            <span className="text-xs text-slate-400 ml-1">
              ({Math.round((v / total) * 100)}%)
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function FrequencyChart({ data }: { data: { bucket: string; n: number }[] }) {
  if (data.length === 0)
    return (
      <div className="text-sm text-slate-400 py-6 text-center">
        No data for this window.
      </div>
    );

  const max = Math.max(...data.map((d) => d.n), 1);
  // Show up to 10 labels, evenly spaced
  const labelEvery = Math.max(1, Math.ceil(data.length / 10));

  return (
    <div>
      {/* bar chart */}
      <div className="flex items-end gap-px h-28 mb-1">
        {data.map((d) => (
          <div
            key={d.bucket}
            className="flex-1 min-w-0 flex flex-col justify-end"
            title={`${d.bucket}: ${d.n}`}
          >
            <div
              className="bg-sky-500 hover:bg-sky-600 rounded-t-sm transition-colors"
              style={{
                height: `${(d.n / max) * 100}%`,
                minHeight: d.n > 0 ? 2 : 0,
              }}
            />
          </div>
        ))}
      </div>
      {/* x-axis labels */}
      <div className="flex items-start gap-px">
        {data.map((d, idx) => (
          <div key={d.bucket} className="flex-1 min-w-0 overflow-hidden">
            {idx % labelEvery === 0 && (
              <div className="text-[10px] text-slate-400 leading-tight truncate">
                {formatBucket(d.bucket)}
              </div>
            )}
          </div>
        ))}
      </div>
      {/* max label */}
      <div className="flex justify-between mt-2 text-[10px] text-slate-400">
        <span>0</span>
        <span>peak: {max}</span>
      </div>
    </div>
  );
}

function SummaryBlock({ text }: { text: string }) {
  if (!text.trim()) {
    return (
      <div className="text-sm text-slate-400 italic">
        No non-tender feedback in this window.
      </div>
    );
  }

  // Split into paragraphs on blank lines and render each as its own block.
  const paragraphs = text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

  return (
    <div className="space-y-3">
      {paragraphs.map((para, i) => (
        <p key={i} className="text-sm text-slate-700 leading-relaxed">
          {renderInline(para)}
        </p>
      ))}
    </div>
  );
}

/**
 * Convert **bold** and *italic* markers inside a single paragraph into
 * React elements. Handles the most common Claude output patterns without
 * needing a markdown library.
 */
function renderInline(text: string): React.ReactNode {
  // Strip any leftover markdown headers (### / ## / #)
  const clean = text.replace(/^#{1,6}\s+/gm, "").replace(/^[-*]\s+/gm, "• ");

  // Split on **bold** or *italic* spans
  const parts = clean.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i} className="font-semibold text-slate-900">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("*") && part.endsWith("*")) {
      return <em key={i}>{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

function formatBucket(b: string): string {
  // 2026-W15 → W15
  if (/^\d{4}-W\d{2}$/.test(b)) return b.slice(5);
  // 2026-04-17 → 4/17
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(b);
  if (m) return `${Number(m[2])}/${Number(m[3])}`;
  // 2026-04 → Apr
  const m2 = /^(\d{4})-(\d{2})$/.exec(b);
  if (m2) {
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return months[Number(m2[2]) - 1] ?? b;
  }
  return b;
}

function Spinner() {
  return (
    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
}
