import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ScopeProposal } from "../api";

const WINDOW_OPTIONS = [
  { label: "1 month",  days: 30  },
  { label: "3 months", days: 90  },
  { label: "6 months", days: 180 },
];

// Stages shown while the ~35-second propose call runs
const PROGRESS_STAGES = [
  { after: 0,  label: "Fetching notes from Productboard…" },
  { after: 6,  label: "Sending to Claude for analysis…"   },
  { after: 18, label: "Claude is reviewing the scope…"    },
];

export default function Training() {
  const qc = useQueryClient();
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: api.scopesList });
  const [selectedPM, setSelectedPM] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(90);
  const [elapsed, setElapsed] = useState(0);

  // Proposals are stored in the query cache so they survive tab navigation.
  // Key: ["training-proposal", pm_email]. Never auto-fetched (enabled: false).
  const proposalCache = useQuery<ScopeProposal[] | null>({
    queryKey: ["training-proposal", selectedPM ?? ""],
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
    gcTime: 10 * 60 * 1000,
  });
  const proposals = proposalCache.data ?? null;
  const activeProposal = proposals?.find((p) => p.pm_email === selectedPM) ?? null;

  const propose = useMutation({
    mutationFn: (pm_email: string) =>
      api.proposeTraining({ pm_email, window_days: windowDays }),
    onSuccess: (d, pm_email) => {
      qc.setQueryData(["training-proposal", pm_email], d.proposals);
    },
  });

  const apply = useMutation({
    mutationFn: (p: ScopeProposal) =>
      api.applyTraining({
        pm_email: p.pm_email,
        yaml_content: p.proposed_yaml,
        rationale_no: p.rationale_no,
        sample_size: p.sample_size,
      }),
    onSuccess: (_d, p) => {
      qc.invalidateQueries({ queryKey: ["scopes"] });
      qc.invalidateQueries({ queryKey: ["scope", p.pm_email] });
      // Remove this PM's proposal from cache after applying
      qc.setQueryData(["training-proposal", p.pm_email], null);
    },
  });

  // Elapsed-time counter while proposing
  useEffect(() => {
    if (!propose.isPending) { setElapsed(0); return; }
    const t = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(t);
  }, [propose.isPending]);

  const progressLabel = [...PROGRESS_STAGES]
    .reverse()
    .find((s) => elapsed >= s.after)?.label ?? PROGRESS_STAGES[0].label;

  const pmEmails = scopes.data?.pm_emails ?? [];

  return (
    <div className="space-y-4">
      {/* ── header ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-lg font-medium text-slate-900">Scope training</h2>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-slate-500">
            {selectedPM ? selectedPM : "select a PM below"}
          </span>
          <select
            value={windowDays}
            onChange={(e) => setWindowDays(Number(e.target.value))}
            className="text-xs border border-slate-200 rounded-md px-2 py-1.5 bg-white text-slate-700"
          >
            {WINDOW_OPTIONS.map((o) => (
              <option key={o.days} value={o.days}>{o.label}</option>
            ))}
          </select>
          <button
            onClick={() => selectedPM && propose.mutate(selectedPM)}
            disabled={propose.isPending || !selectedPM}
            title={!selectedPM ? "Select a PM first" : `Propose update for ${selectedPM}`}
            className="px-3 py-1.5 text-sm rounded-md bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {propose.isPending ? "Proposing…" : "Propose update"}
          </button>
        </div>
      </div>

      <p className="text-sm text-slate-600">
        Select a PM, pick a lookback window, then click Propose update. Fetches their
        recent notes from Productboard and asks Claude to suggest minimal edits to the
        scope YAML. Override corrections from the Reviewer are flagged as extra signal.
        You approve before anything lands.
      </p>

      {/* ── progress indicator ── */}
      {propose.isPending && (
        <div className="flex items-center gap-3 px-4 py-3 bg-slate-50 border border-slate-200 rounded-md">
          <Spinner />
          <div className="flex-1 min-w-0">
            <div className="text-sm text-slate-700">{progressLabel}</div>
            <div className="mt-1.5 h-1 bg-slate-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-slate-700 rounded-full transition-all duration-1000"
                style={{ width: `${Math.min((elapsed / 40) * 100, 95)}%` }}
              />
            </div>
          </div>
          <span className="text-xs text-slate-400 tabular-nums">{elapsed}s</span>
        </div>
      )}

      {propose.isError && (
        <div className="text-rose-700 text-sm bg-rose-50 border border-rose-200 rounded-md px-3 py-2">
          Failed: {(propose.error as Error).message}
        </div>
      )}

      {/* ── two-column layout ── */}
      <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-4">
        {/* PM list */}
        <div className="bg-white border border-slate-200 rounded-md divide-y divide-slate-100">
          {pmEmails.length === 0 && (
            <div className="p-3 text-sm text-slate-400">Loading…</div>
          )}
          {pmEmails.map((email) => {
            const cached = qc.getQueryData<ScopeProposal[] | null>(["training-proposal", email]);
            const proposal = cached?.find((p) => p.pm_email === email) ?? null;
            return (
              <button
                key={email}
                onClick={() => setSelectedPM(email)}
                className={[
                  "w-full text-left px-3 py-2 text-sm hover:bg-slate-50",
                  selectedPM === email ? "bg-slate-100" : "",
                ].join(" ")}
              >
                <div className="text-slate-900 truncate">{email}</div>
                {proposal && (
                  <div className="text-xs mt-0.5 flex gap-2">
                    <span className="text-slate-400">{proposal.sample_size} notes</span>
                    {proposal.changed
                      ? <span className="text-amber-700">changes proposed</span>
                      : <span className="text-emerald-700">no changes</span>}
                  </div>
                )}
              </button>
            );
          })}
        </div>

        {/* Right panel: proposal if exists, otherwise current scope */}
        {activeProposal ? (
          <ProposalView
            proposal={activeProposal}
            onApply={() => apply.mutate(activeProposal)}
            applying={apply.isPending}
          />
        ) : (
          <ScopePanel selectedPM={selectedPM} isPending={propose.isPending} />
        )}
      </div>
    </div>
  );
}

// ── Scope panel ───────────────────────────────────────────────────────────────

function ScopePanel({
  selectedPM,
  isPending,
}: {
  selectedPM: string | null;
  isPending: boolean;
}) {
  const scope = useQuery({
    queryKey: ["scope", selectedPM],
    queryFn: () => api.scope(selectedPM!),
    enabled: !!selectedPM,
  });

  if (!selectedPM) {
    return (
      <div className="bg-white border border-slate-200 rounded-md p-4 text-sm text-slate-500">
        Select a PM to view their scope YAML, then click Propose update to generate a suggestion.
      </div>
    );
  }
  if (scope.isLoading) {
    return <div className="bg-white border border-slate-200 rounded-md p-4 text-sm text-slate-500">Loading…</div>;
  }
  if (!scope.data) return null;

  return (
    <div className={[
      "bg-white border rounded-md overflow-hidden transition-opacity",
      isPending ? "border-slate-300 opacity-50" : "border-slate-200",
    ].join(" ")}>
      <div className="px-4 py-2 border-b border-slate-100 text-sm font-medium text-slate-700 flex items-center justify-between">
        <span>{scope.data.pm_email}</span>
        <span className="text-xs text-slate-500">
          {scope.data.history.length} version{scope.data.history.length === 1 ? "" : "s"} recorded
        </span>
      </div>
      <pre className="p-4 text-xs whitespace-pre-wrap overflow-auto max-h-[600px] text-slate-800 font-mono">
        {scope.data.yaml_content}
      </pre>
    </div>
  );
}

// ── Proposal view ─────────────────────────────────────────────────────────────

function ProposalView({
  proposal,
  onApply,
  applying,
}: {
  proposal: ScopeProposal;
  onApply: () => void;
  applying: boolean;
}) {
  return (
    <div className="space-y-3">
      <div className="bg-white border border-slate-200 rounded-md p-4 space-y-2">
        <div className="text-sm text-slate-700">
          <span className="font-medium">Rationale:</span>{" "}
          {proposal.rationale_no || <em className="text-slate-400">none provided</em>}
        </div>
        <div className="text-xs text-slate-500">
          model: {proposal.model} · sample: {proposal.sample_size} notes
        </div>
        <div>
          <button
            onClick={onApply}
            disabled={applying || !proposal.changed}
            className="px-3 py-1.5 text-sm rounded-md bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {applying ? "Applying…" : proposal.changed ? "Apply update" : "No changes to apply"}
          </button>
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <YamlBox title="Current" content={proposal.current_yaml} />
        <YamlBox title="Proposed" content={proposal.proposed_yaml} highlight />
      </div>
    </div>
  );
}

function YamlBox({
  title,
  content,
  highlight,
}: {
  title: string;
  content: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={[
        "rounded-md border text-xs font-mono overflow-hidden",
        highlight ? "border-emerald-400" : "border-slate-200",
      ].join(" ")}
    >
      <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-600">
        {title}
      </div>
      <pre className="p-3 whitespace-pre-wrap max-h-[500px] overflow-auto text-slate-800">
        {content}
      </pre>
    </div>
  );
}

function Spinner() {
  return (
    <svg
      className="animate-spin h-4 w-4 text-slate-500 flex-shrink-0"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}
