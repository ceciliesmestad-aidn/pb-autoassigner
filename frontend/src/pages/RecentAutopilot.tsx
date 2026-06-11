/**
 * Recent Autopilot tab — spot-check what the overnight launchd run
 * decided. Shows the last N hours of auto-assigned notes with the
 * chosen PM, the confidence, the reasoning, and a one-click override
 * (which just calls /api/notes/{id}/assign with a different pm_email,
 * exactly like the Reviewer tab does).
 *
 * Dry-run rows (pb_status=NULL, pb_error contains "[DRY-RUN]") are
 * shown with a yellow badge so you can tell which were "what would
 * have happened" vs. real PATCHes.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type AutopilotRow,
  confidenceColor,
} from "../api";

type Window = 24 | 72 | 168;  // hours: 1 day / 3 days / 1 week

export default function RecentAutopilot() {
  const qc = useQueryClient();
  const [hours, setHours] = useState<Window>(24);

  const cfg = useQuery({ queryKey: ["config"], queryFn: api.config });
  const pms = useQuery({ queryKey: ["pms"], queryFn: api.pms });
  const recent = useQuery({
    queryKey: ["recent-autopilot", hours],
    queryFn: () => api.recentAutopilot(hours),
    // Refresh every 60 s so a manual `pb-assigner run` shows up without reload.
    refetchInterval: 60_000,
  });

  const override = useMutation({
    mutationFn: ({ noteId, pm_email }: { noteId: number; pm_email: string }) =>
      api.assign(noteId, pm_email),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["recent-autopilot"] });
      qc.invalidateQueries({ queryKey: ["suggestions"] });
    },
  });

  const items = recent.data?.items ?? [];

  const stats = useMemo(() => {
    let real = 0;
    let dryRun = 0;
    let errors = 0;
    for (const r of items) {
      const isDryRun = r.pb_status == null && (r.pb_error ?? "").includes("[DRY-RUN]");
      if (isDryRun) dryRun++;
      else if (r.pb_error && !isDryRun) errors++;
      else real++;
    }
    return { real, dryRun, errors, total: items.length };
  }, [items]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3 items-center">
        <div className="inline-flex rounded-md border border-slate-300 bg-white text-sm overflow-hidden">
          {([24, 72, 168] as const).map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={[
                "px-3 py-1.5",
                hours === h
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-50",
              ].join(" ")}
            >
              {h === 24 ? "Last 24 h" : h === 72 ? "Last 3 days" : "Last week"}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-3 text-sm text-slate-600">
          {cfg.data && !cfg.data.autopilot_enabled && (
            <span className="px-2 py-1 rounded-md bg-slate-100 text-slate-600 text-xs">
              Autopilot is OFF — this list shows past runs only
            </span>
          )}
          <span>
            {stats.total} decision{stats.total === 1 ? "" : "s"}
            {stats.dryRun > 0 && <> · {stats.dryRun} dry-run</>}
            {stats.errors > 0 && (
              <span className="text-rose-700"> · {stats.errors} error{stats.errors === 1 ? "" : "s"}</span>
            )}
          </span>
        </div>
      </div>

      {recent.isLoading && (
        <div className="text-slate-500 text-sm">Loading…</div>
      )}
      {recent.isError && (
        <div className="text-rose-700 text-sm">
          Failed to load: {(recent.error as Error).message}
        </div>
      )}

      <div className="bg-white border border-slate-200 rounded-md overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-3 py-2 text-left">Assigned</th>
              <th className="px-3 py-2 text-left">Title</th>
              <th className="px-3 py-2 text-left">Auto-assigned to</th>
              <th className="px-3 py-2 text-left">Confidence</th>
              <th className="px-3 py-2 text-left">Reasoning</th>
              <th className="px-3 py-2 text-left">Status</th>
              <th className="px-3 py-2 text-right">Override</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((row) => (
              <Row
                key={row.assignment_id}
                row={row}
                pms={pms.data ?? []}
                onOverride={(pm_email) =>
                  override.mutate({ noteId: row.note_id, pm_email })
                }
              />
            ))}
            {items.length === 0 && !recent.isLoading && (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-slate-400">
                  No autopilot decisions in this window. Either autopilot
                  is off, the launchd run hasn't happened yet, or every
                  note needed manual review.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Row({
  row,
  pms,
  onOverride,
}: {
  row: AutopilotRow;
  pms: { email: string; name: string; team: string }[];
  onOverride: (pm_email: string) => void;
}) {
  const isDryRun =
    row.pb_status == null && (row.pb_error ?? "").includes("[DRY-RUN]");
  const isError = !isDryRun && !!row.pb_error;
  const assignedPM = pms.find((p) => p.email === row.pm_email);

  return (
    <tr>
      <td className="px-3 py-2 align-top text-xs text-slate-500 whitespace-nowrap">
        {formatRelative(row.assigned_at)}
      </td>
      <td className="px-3 py-2 align-top">
        <div className="font-medium text-slate-900">
          {row.title || <em className="text-slate-400">untitled</em>}
        </div>
        <div className="text-xs text-slate-500 mt-0.5 flex flex-wrap gap-2">
          {row.company && <span>{row.company}</span>}
          {row.display_url && (
            <a
              href={row.display_url}
              target="_blank"
              rel="noreferrer"
              className="text-sky-700 hover:underline"
            >
              Open in PB →
            </a>
          )}
        </div>
      </td>
      <td className="px-3 py-2 align-top">
        {assignedPM ? (
          <div>
            <div className="font-medium">{assignedPM.name}</div>
            <div className="text-xs text-slate-500">{assignedPM.team}</div>
          </div>
        ) : (
          <span className="text-slate-500 text-xs">{row.pm_email}</span>
        )}
      </td>
      <td className="px-3 py-2 align-top">
        <span
          className={[
            "text-xs font-medium px-2 py-0.5 rounded-full",
            confidenceColor(row.confidence),
          ].join(" ")}
        >
          {row.confidence != null ? row.confidence.toFixed(2) : "—"}
        </span>
      </td>
      <td className="px-3 py-2 align-top text-xs text-slate-700 max-w-md">
        {row.reasoning}
      </td>
      <td className="px-3 py-2 align-top">
        {isDryRun ? (
          <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-amber-100 text-amber-800">
            dry-run
          </span>
        ) : isError ? (
          <span
            className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-rose-100 text-rose-800"
            title={row.pb_error ?? ""}
          >
            error
          </span>
        ) : (
          <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800">
            patched
          </span>
        )}
      </td>
      <td className="px-3 py-2 align-top text-right">
        <select
          onChange={(e) => {
            if (e.target.value && e.target.value !== row.pm_email) {
              onOverride(e.target.value);
            }
            e.currentTarget.selectedIndex = 0;
          }}
          defaultValue=""
          className="text-xs border border-slate-300 rounded px-1 py-1 bg-white"
        >
          <option value="">Reassign…</option>
          {pms
            .filter((p) => p.email !== row.pm_email)
            .map((p) => (
              <option key={p.email} value={p.email}>
                {p.name}
              </option>
            ))}
        </select>
      </td>
    </tr>
  );
}

// ─── helpers ─────────────────────────────────────────────────────────────────

function formatRelative(iso: string): string {
  // SQLite returns 'YYYY-MM-DD HH:MM:SS' (UTC, no zone). Treat as UTC.
  const ts = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return iso;
  const diffMs = Date.now() - d.getTime();
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}
