import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type RunRow } from "../api";

/**
 * Live backend console.
 *
 * - Tails `data/backend.log` via `/api/logs/tail` every 2s.
 * - Shows recent runs (ingest / classify / train) with stats.
 * - Colour-codes log lines by level.
 */
export default function Console() {
  const [autoScroll, setAutoScroll] = useState(true);
  const [tailLines, setTailLines] = useState(300);
  const [levelFilter, setLevelFilter] = useState<"all" | "info" | "warn" | "error">(
    "all",
  );

  const logs = useQuery({
    queryKey: ["logs", tailLines],
    queryFn: () => api.logsTail(tailLines),
    refetchInterval: 2000,
    refetchIntervalInBackground: false,
  });

  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.runs(30),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });

  const endRef = useRef<HTMLDivElement | null>(null);

  const filtered = useMemo(() => {
    const all = logs.data?.lines ?? [];
    if (levelFilter === "all") return all;
    return all.filter((line) => {
      const lvl = detectLevel(line);
      if (levelFilter === "info") return lvl === "info" || lvl === "debug";
      if (levelFilter === "warn") return lvl === "warn" || lvl === "error";
      return lvl === "error";
    });
  }, [logs.data, levelFilter]);

  useEffect(() => {
    if (autoScroll) endRef.current?.scrollIntoView({ behavior: "auto" });
  }, [filtered, autoScroll]);

  return (
    <div className="space-y-6">
      {/* Recent runs */}
      <section>
        <div className="flex items-baseline justify-between mb-2">
          <h2 className="text-sm font-semibold text-slate-800">Recent runs</h2>
          <span className="text-xs text-slate-500">
            {runs.data?.runs.length ?? 0} runs · refreshes every 5s
          </span>
        </div>
        <div className="border border-slate-200 rounded-md bg-white overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Started</th>
                <th className="px-3 py-2">Duration</th>
                <th className="px-3 py-2">Stats</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data?.runs ?? []).map((r) => (
                <tr key={r.run_id} className="border-t border-slate-100">
                  <td className="px-3 py-2">
                    <KindBadge kind={r.kind} />
                  </td>
                  <td className="px-3 py-2 text-slate-700 tabular-nums text-xs">
                    {formatTime(r.started_at)}
                  </td>
                  <td className="px-3 py-2 text-slate-700 tabular-nums text-xs">
                    {formatDuration(r)}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-600">
                    {formatStats(r.stats)}
                  </td>
                </tr>
              ))}
              {(runs.data?.runs ?? []).length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-4 text-slate-500 text-sm">
                    No runs yet. Click “Run now” to kick one off.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Log tail */}
      <section>
        <div className="flex items-center justify-between mb-2 gap-3 flex-wrap">
          <h2 className="text-sm font-semibold text-slate-800">
            Backend log{" "}
            <span className="font-normal text-xs text-slate-500">
              ({logs.data?.path ?? "data/backend.log"})
            </span>
          </h2>
          <div className="flex items-center gap-2 text-xs">
            <label className="flex items-center gap-1">
              Tail
              <select
                value={tailLines}
                onChange={(e) => setTailLines(Number(e.target.value))}
                className="border border-slate-300 rounded px-1 py-0.5"
              >
                <option value={100}>100</option>
                <option value={300}>300</option>
                <option value={1000}>1000</option>
                <option value={3000}>3000</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              Level
              <select
                value={levelFilter}
                onChange={(e) =>
                  setLevelFilter(e.target.value as typeof levelFilter)
                }
                className="border border-slate-300 rounded px-1 py-0.5"
              >
                <option value="all">all</option>
                <option value="info">info+</option>
                <option value="warn">warn+</option>
                <option value="error">error only</option>
              </select>
            </label>
            <label className="flex items-center gap-1 cursor-pointer">
              <input
                type="checkbox"
                checked={autoScroll}
                onChange={(e) => setAutoScroll(e.target.checked)}
              />
              follow
            </label>
            <span className="text-slate-500">
              {logs.isFetching ? "refreshing…" : "·"}
            </span>
          </div>
        </div>

        <div className="bg-slate-900 text-slate-100 text-xs font-mono rounded-md border border-slate-700 h-[520px] overflow-auto p-3 leading-relaxed">
          {logs.isError && (
            <div className="text-rose-300">
              Failed to load logs: {(logs.error as Error).message}
            </div>
          )}
          {filtered.map((line, i) => (
            <div key={i} className={colorFor(line)}>
              {line || "\u00a0"}
            </div>
          ))}
          {filtered.length === 0 && !logs.isError && (
            <div className="text-slate-400">
              No log lines yet. Kick off a run to see progress here.
            </div>
          )}
          <div ref={endRef} />
        </div>
      </section>
    </div>
  );
}

// ─── helpers ────────────────────────────────────────────────────────────────

function detectLevel(line: string): "error" | "warn" | "info" | "debug" {
  // Matches both our Python formatter ("... ERROR name:") and uvicorn's format.
  if (/\bERROR\b|\bCRITICAL\b|Traceback|APIConnectionError/.test(line)) return "error";
  if (/\bWARNING\b|\bWARN\b/.test(line)) return "warn";
  if (/\bDEBUG\b/.test(line)) return "debug";
  return "info";
}

function colorFor(line: string): string {
  const lvl = detectLevel(line);
  if (lvl === "error") return "text-rose-300";
  if (lvl === "warn") return "text-amber-300";
  if (lvl === "debug") return "text-slate-500";
  // Highlight our own logger lines in a lighter tone.
  if (/ backend\.(pipeline|classify|train|pb_client): /.test(line))
    return "text-emerald-200";
  return "text-slate-200";
}

function KindBadge({ kind }: { kind: string }) {
  const cls =
    kind === "ingest"
      ? "bg-sky-100 text-sky-800"
      : kind === "classify"
        ? "bg-violet-100 text-violet-800"
        : kind === "train"
          ? "bg-amber-100 text-amber-800"
          : "bg-slate-100 text-slate-700";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {kind}
    </span>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function formatDuration(r: RunRow): string {
  if (!r.finished_at) return "…running";
  try {
    const ms = new Date(r.finished_at).getTime() - new Date(r.started_at).getTime();
    if (ms < 1000) return `${ms} ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
    return `${(ms / 60_000).toFixed(1)} m`;
  } catch {
    return "—";
  }
}

function formatStats(stats: Record<string, unknown>): string {
  const keys = Object.keys(stats);
  if (keys.length === 0) return "—";
  // Compact one-liner: key=value pairs.
  return keys
    .map((k) => `${k}=${typeof stats[k] === "object" ? JSON.stringify(stats[k]) : stats[k]}`)
    .join("  ");
}
