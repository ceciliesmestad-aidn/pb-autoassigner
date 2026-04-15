import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ScopeProposal } from "../api";

export default function Training() {
  const qc = useQueryClient();
  const scopes = useQuery({ queryKey: ["scopes"], queryFn: api.scopesList });
  const [proposals, setProposals] = useState<ScopeProposal[] | null>(null);
  const [selectedPM, setSelectedPM] = useState<string | null>(null);

  const propose = useMutation({
    mutationFn: api.proposeTraining,
    onSuccess: (d) => setProposals(d.proposals),
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
      setProposals((prev) =>
        prev ? prev.filter((x) => x.pm_email !== p.pm_email) : prev,
      );
    },
  });

  const activeProposal = useMemo(
    () => proposals?.find((p) => p.pm_email === selectedPM) ?? null,
    [proposals, selectedPM],
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-medium text-slate-900">Scope training</h2>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-slate-500">
            {scopes.data ? `${scopes.data.pm_emails.length} scope files loaded` : ""}
          </span>
          <button
            onClick={() => propose.mutate()}
            disabled={propose.isPending}
            className="px-3 py-1.5 text-sm rounded-md bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {propose.isPending ? "Proposing…" : "Propose updates"}
          </button>
        </div>
      </div>

      <p className="text-sm text-slate-600">
        Fetches notes currently owned by each PM in Productboard (last 6 months)
        and asks Claude to propose a minimal update to their scope YAML. If
        you've used the Reviewer to override suggestions, those corrections are
        highlighted as extra signal. You approve each change before it lands.
      </p>

      {propose.isError && (
        <div className="text-rose-700 text-sm">
          Failed: {(propose.error as Error).message}
        </div>
      )}

      {proposals && proposals.length === 0 && (
        <div className="text-slate-500 text-sm italic">
          No proposals generated. Either no PMs have enough recently-assigned
          notes, or the model thinks the current scopes already capture the
          pattern.
        </div>
      )}

      {proposals && proposals.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-4">
          <div className="bg-white border border-slate-200 rounded-md divide-y divide-slate-100">
            {proposals.map((p) => (
              <button
                key={p.pm_email}
                onClick={() => setSelectedPM(p.pm_email)}
                className={[
                  "w-full text-left px-3 py-2 text-sm hover:bg-slate-50",
                  selectedPM === p.pm_email ? "bg-slate-100" : "",
                ].join(" ")}
              >
                <div className="font-medium text-slate-900">{p.pm_email}</div>
                <div className="text-xs text-slate-500 flex gap-2 mt-0.5">
                  <span>{p.sample_size} notes</span>
                  {p.changed ? (
                    <span className="text-amber-700">changes proposed</span>
                  ) : (
                    <span className="text-emerald-700">no changes</span>
                  )}
                </div>
              </button>
            ))}
          </div>

          {activeProposal ? (
            <ProposalView
              proposal={activeProposal}
              onApply={() => apply.mutate(activeProposal)}
              applying={apply.isPending}
            />
          ) : (
            <div className="bg-white border border-slate-200 rounded-md p-4 text-sm text-slate-500">
              Select a proposal to review.
            </div>
          )}
        </div>
      )}

      {!proposals && (
        <ScopeBrowser
          pmEmails={scopes.data?.pm_emails ?? []}
          selectedPM={selectedPM}
          onSelect={setSelectedPM}
        />
      )}
    </div>
  );
}

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
          model: {proposal.model} · sample size: {proposal.sample_size}
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

function ScopeBrowser({
  pmEmails,
  selectedPM,
  onSelect,
}: {
  pmEmails: string[];
  selectedPM: string | null;
  onSelect: (pm: string) => void;
}) {
  const scope = useQuery({
    queryKey: ["scope", selectedPM],
    queryFn: () => api.scope(selectedPM!),
    enabled: !!selectedPM,
  });
  return (
    <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-4">
      <div className="bg-white border border-slate-200 rounded-md divide-y divide-slate-100">
        {pmEmails.map((e) => (
          <button
            key={e}
            onClick={() => onSelect(e)}
            className={[
              "w-full text-left px-3 py-2 text-sm hover:bg-slate-50",
              selectedPM === e ? "bg-slate-100" : "",
            ].join(" ")}
          >
            {e}
          </button>
        ))}
      </div>
      <div className="bg-white border border-slate-200 rounded-md">
        {scope.isLoading && (
          <div className="p-4 text-sm text-slate-500">Loading…</div>
        )}
        {scope.data && (
          <div>
            <div className="px-4 py-2 border-b border-slate-100 text-sm font-medium text-slate-700 flex items-center justify-between">
              <span>{scope.data.pm_email}</span>
              <span className="text-xs text-slate-500">
                {scope.data.history.length} version
                {scope.data.history.length === 1 ? "" : "s"} recorded
              </span>
            </div>
            <pre className="p-4 text-xs whitespace-pre-wrap overflow-auto max-h-[600px] text-slate-800 font-mono">
              {scope.data.yaml_content}
            </pre>
          </div>
        )}
        {!selectedPM && (
          <div className="p-4 text-sm text-slate-500">
            Select a PM to view their scope YAML.
          </div>
        )}
      </div>
    </div>
  );
}
