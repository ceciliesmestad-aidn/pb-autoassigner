import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type Suggestion,
  parseTags,
  confidenceColor,
} from "../api";

export default function Reviewer() {
  const qc = useQueryClient();
  const [pmFilter, setPmFilter] = useState<string>("");
  const [confFilter, setConfFilter] = useState<"all" | "high" | "attention">("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<number | null>(null);

  const cfg = useQuery({ queryKey: ["config"], queryFn: api.config });
  const pms = useQuery({ queryKey: ["pms"], queryFn: api.pms });
  const suggestions = useQuery({
    queryKey: ["suggestions", pmFilter, confFilter],
    queryFn: () =>
      api.suggestions({
        pm_email: pmFilter || undefined,
        min_confidence:
          confFilter === "high" ? cfg.data?.autopilot_min_confidence : undefined,
        max_confidence:
          confFilter === "attention" ? cfg.data?.needs_attention_below : undefined,
      }),
    enabled: !cfg.isLoading,
  });

  const assign = useMutation({
    mutationFn: ({ noteId, pm_email }: { noteId: number; pm_email: string }) =>
      api.assign(noteId, pm_email),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["suggestions"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      setSelected(new Set());
    },
  });

  const skip = useMutation({
    mutationFn: (noteId: number) => api.skip(noteId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["suggestions"] }),
  });

  const items = suggestions.data?.items ?? [];

  const selectedWithSuggestion = useMemo(
    () => items.filter((i) => selected.has(i.note_id) && i.suggested_pm),
    [items, selected],
  );

  const allSelected = items.length > 0 && items.every((i) => selected.has(i.note_id));
  const toggleAll = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(items.map((i) => i.note_id)));
  };

  const assignAllSelected = async () => {
    // Assign each selected note to its currently-suggested PM.
    for (const item of selectedWithSuggestion) {
      if (!item.suggested_pm) continue;
      try {
        await assign.mutateAsync({
          noteId: item.note_id,
          pm_email: item.suggested_pm,
        });
      } catch (e) {
        console.error("bulk assign failed for", item.note_id, e);
      }
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3 items-center">
        <select
          value={pmFilter}
          onChange={(e) => setPmFilter(e.target.value)}
          className="border border-slate-300 bg-white rounded-md px-2 py-1.5 text-sm"
        >
          <option value="">All PMs</option>
          {pms.data?.map((p) => (
            <option key={p.email} value={p.email}>
              {p.name} — {p.team}
            </option>
          ))}
        </select>
        <div className="inline-flex rounded-md border border-slate-300 bg-white text-sm overflow-hidden">
          {(["all", "high", "attention"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setConfFilter(k)}
              className={[
                "px-3 py-1.5",
                confFilter === k
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-50",
              ].join(" ")}
            >
              {k === "all"
                ? "All"
                : k === "high"
                  ? "High confidence"
                  : "Needs attention"}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <span className="text-slate-500">
            {items.length} note{items.length === 1 ? "" : "s"}
            {selected.size > 0 && (
              <> · {selected.size} selected</>
            )}
          </span>
          {selectedWithSuggestion.length > 0 && (
            <button
              onClick={assignAllSelected}
              disabled={assign.isPending}
              className="px-3 py-1.5 rounded-md bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              Assign {selectedWithSuggestion.length} to suggested PM
            </button>
          )}
        </div>
      </div>

      {suggestions.isLoading && <div className="text-slate-500 text-sm">Loading…</div>}
      {suggestions.isError && (
        <div className="text-rose-700 text-sm">
          Failed to load: {(suggestions.error as Error).message}
        </div>
      )}

      <div className="bg-white border border-slate-200 rounded-md overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-3 py-2 w-10">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  aria-label="select all"
                />
              </th>
              <th className="px-3 py-2 text-left">Title</th>
              <th className="px-3 py-2 text-left">Suggested PM</th>
              <th className="px-3 py-2 text-left">Confidence</th>
              <th className="px-3 py-2 text-left">Reasoning</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((item) => (
              <Row
                key={item.note_id}
                item={item}
                pms={pms.data ?? []}
                selected={selected.has(item.note_id)}
                onToggle={() => {
                  const next = new Set(selected);
                  if (next.has(item.note_id)) next.delete(item.note_id);
                  else next.add(item.note_id);
                  setSelected(next);
                }}
                expanded={expanded === item.note_id}
                onExpand={() =>
                  setExpanded(expanded === item.note_id ? null : item.note_id)
                }
                onAssign={(pm_email) =>
                  assign.mutate({ noteId: item.note_id, pm_email })
                }
                onSkip={() => skip.mutate(item.note_id)}
                needsAttentionBelow={cfg.data?.needs_attention_below ?? 0.6}
              />
            ))}
            {items.length === 0 && !suggestions.isLoading && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-slate-400">
                  Nothing to review. Run the pipeline or adjust filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Row(props: {
  item: Suggestion;
  pms: { email: string; name: string; team: string }[];
  selected: boolean;
  onToggle: () => void;
  expanded: boolean;
  onExpand: () => void;
  onAssign: (pm_email: string) => void;
  onSkip: () => void;
  needsAttentionBelow: number;
}) {
  const { item, pms, selected, onToggle, expanded, onExpand, onAssign, onSkip } =
    props;
  const tags = parseTags(item.tags_json);
  const suggestedPM = pms.find((p) => p.email === item.suggested_pm);
  const attention =
    item.confidence != null && item.confidence < props.needsAttentionBelow;

  return (
    <>
      <tr className={selected ? "bg-slate-50" : ""}>
        <td className="px-3 py-2 align-top">
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            aria-label="select"
          />
        </td>
        <td className="px-3 py-2 align-top">
          <button
            onClick={onExpand}
            className="text-left font-medium text-slate-900 hover:underline"
          >
            {item.title || <em className="text-slate-400">untitled</em>}
          </button>
          <div className="text-xs text-slate-500 mt-0.5 flex flex-wrap gap-2">
            {item.company && <span>{item.company}</span>}
            {item.pb_created_at && (
              <span>{item.pb_created_at.slice(0, 10)}</span>
            )}
            {tags.map((t) => (
              <span
                key={t}
                className="bg-slate-100 text-slate-600 rounded px-1.5 py-0.5"
              >
                {t}
              </span>
            ))}
          </div>
        </td>
        <td className="px-3 py-2 align-top">
          {suggestedPM ? (
            <div>
              <div className="font-medium">{suggestedPM.name}</div>
              <div className="text-xs text-slate-500">{suggestedPM.team}</div>
            </div>
          ) : (
            <span className="text-slate-400 italic">leave open</span>
          )}
        </td>
        <td className="px-3 py-2 align-top">
          <span
            className={[
              "text-xs font-medium px-2 py-0.5 rounded-full",
              confidenceColor(item.confidence),
            ].join(" ")}
          >
            {item.confidence != null ? item.confidence.toFixed(2) : "—"}
          </span>
          {attention && (
            <div className="text-[11px] text-amber-700 mt-1">needs attention</div>
          )}
          {item.escalated ? (
            <div className="text-[11px] text-slate-500 mt-1">
              escalated ({item.model})
            </div>
          ) : null}
        </td>
        <td className="px-3 py-2 align-top text-slate-700 text-xs max-w-md">
          {item.reasoning}
        </td>
        <td className="px-3 py-2 align-top text-right">
          <div className="inline-flex gap-1">
            {item.suggested_pm && (
              <button
                onClick={() => onAssign(item.suggested_pm!)}
                className="px-2 py-1 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-500"
              >
                Assign
              </button>
            )}
            <select
              onChange={(e) => {
                if (e.target.value) onAssign(e.target.value);
                e.currentTarget.selectedIndex = 0;
              }}
              defaultValue=""
              className="text-xs border border-slate-300 rounded px-1 py-1 bg-white"
            >
              <option value="">Override…</option>
              {pms.map((p) => (
                <option key={p.email} value={p.email}>
                  {p.name}
                </option>
              ))}
            </select>
            <button
              onClick={onSkip}
              className="px-2 py-1 text-xs rounded border border-slate-300 text-slate-700 hover:bg-slate-50"
            >
              Skip
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-slate-50">
          <td colSpan={6} className="px-6 py-3">
            <div className="whitespace-pre-wrap text-sm text-slate-800 max-h-96 overflow-y-auto">
              {item.content || <em className="text-slate-400">empty body</em>}
            </div>
            {item.display_url && (
              <a
                href={item.display_url}
                target="_blank"
                rel="noreferrer"
                className="inline-block mt-2 text-xs text-sky-700 hover:underline"
              >
                Open in Productboard →
              </a>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
