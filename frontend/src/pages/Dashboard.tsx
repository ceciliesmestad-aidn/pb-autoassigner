import { useQuery } from "@tanstack/react-query";

import { api } from "../api";

export default function Dashboard() {
  const stats = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });
  const pms = useQuery({ queryKey: ["pms"], queryFn: api.pms });

  if (stats.isLoading) return <div className="text-slate-500 text-sm">Loading…</div>;
  if (stats.isError)
    return (
      <div className="text-rose-700 text-sm">
        Failed to load: {(stats.error as Error).message}
      </div>
    );
  if (!stats.data) return null;

  const { notes_by_state, assignments_7d, assignments_30d, confidence_distribution, weekly_volume } =
    stats.data;
  const pmByEmail = new Map((pms.data ?? []).map((p) => [p.email, p]));

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="New" value={notes_by_state.new ?? 0} />
        <Card label="Suggested" value={notes_by_state.suggested ?? 0} />
        <Card label="Assigned" value={notes_by_state.assigned ?? 0} />
        <Card label="Skipped" value={notes_by_state.skipped ?? 0} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Panel title="Assignments last 7 days">
          <BarList
            rows={assignments_7d.map((r) => ({
              label: pmByEmail.get(r.pm_email)?.name ?? r.pm_email,
              sub: pmByEmail.get(r.pm_email)?.team ?? "",
              value: r.n,
            }))}
          />
        </Panel>
        <Panel title="Assignments last 30 days">
          <BarList
            rows={assignments_30d.map((r) => ({
              label: pmByEmail.get(r.pm_email)?.name ?? r.pm_email,
              sub: pmByEmail.get(r.pm_email)?.team ?? "",
              value: r.n,
            }))}
          />
        </Panel>
        <Panel title="Confidence distribution (current suggestions)">
          <BarList
            rows={Object.entries(confidence_distribution).map(([k, v]) => ({
              label: k,
              sub: "",
              value: v,
            }))}
          />
        </Panel>
        <Panel title="Weekly assignment volume">
          <BarList
            rows={weekly_volume.map((w) => ({
              label: w.week,
              sub: "",
              value: w.n,
            }))}
          />
        </Panel>
      </div>
    </div>
  );
}

function Card({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-white border border-slate-200 rounded-md px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-2xl font-semibold text-slate-900 mt-1">{value}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white border border-slate-200 rounded-md">
      <div className="px-4 py-2 border-b border-slate-100 text-sm font-medium text-slate-700">
        {title}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

function BarList({ rows }: { rows: { label: string; sub: string; value: number }[] }) {
  if (rows.length === 0) return <div className="text-slate-400 text-sm">No data.</div>;
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-3">
          <div className="w-40 shrink-0">
            <div className="text-sm text-slate-800 truncate">{r.label}</div>
            {r.sub && <div className="text-xs text-slate-500 truncate">{r.sub}</div>}
          </div>
          <div className="flex-1 bg-slate-100 rounded h-3 overflow-hidden">
            <div
              className="bg-sky-500 h-full"
              style={{ width: `${(r.value / max) * 100}%` }}
            />
          </div>
          <div className="w-10 text-right text-sm tabular-nums">{r.value}</div>
        </div>
      ))}
    </div>
  );
}
