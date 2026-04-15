/**
 * Typed client for the FastAPI backend.
 *
 * Dev: Vite proxies /api → http://127.0.0.1:8765 (see vite.config.ts).
 * Prod: FastAPI serves the built SPA and the API from the same origin.
 */

export type PM = {
  email: string;
  name: string;
  team: string;
};

export type Suggestion = {
  note_id: number;
  pb_uuid: string;
  title: string;
  content: string;
  tags_json: string;
  company: string;
  source: string;
  display_url: string;
  pb_created_at: string;
  state: string;
  suggested_pm: string | null;
  confidence: number | null;
  reasoning: string | null;
  model: string | null;
  escalated: number | null;
  suggested_at: string | null;
};

export type AppConfig = {
  needs_attention_below: number;
  autopilot_min_confidence: number;
  autopilot_enabled: boolean;
  model_default: string;
  model_escalate: string;
};

export type DashboardStats = {
  notes_by_state: Record<string, number>;
  assignments_7d: { pm_email: string; n: number }[];
  assignments_30d: { pm_email: string; n: number }[];
  confidence_distribution: Record<string, number>;
  weekly_volume: { week: string; n: number }[];
};

export type LogTail = {
  path: string;
  lines: string[];
  size?: number;
};

export type RunRow = {
  run_id: string;
  kind: string;
  started_at: string;
  finished_at: string | null;
  stats: Record<string, unknown>;
};

export type ScopeProposal = {
  pm_email: string;
  current_yaml: string;
  proposed_yaml: string;
  rationale_no: string;
  changed: boolean;
  sample_size: number;
  model: string;
};

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let body = "";
    try {
      body = await res.text();
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as T;
}

export const api = {
  config: () => fetch("/api/config").then(json<AppConfig>),
  pms: () => fetch("/api/pms").then(json<PM[]>),

  suggestions: (params: {
    pm_email?: string;
    min_confidence?: number;
    max_confidence?: number;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params.pm_email) qs.set("pm_email", params.pm_email);
    if (params.min_confidence != null)
      qs.set("min_confidence", String(params.min_confidence));
    if (params.max_confidence != null)
      qs.set("max_confidence", String(params.max_confidence));
    if (params.limit != null) qs.set("limit", String(params.limit));
    return fetch(`/api/suggestions?${qs}`).then(
      json<{ items: Suggestion[] }>,
    );
  },

  assign: (noteId: number, pm_email: string) =>
    fetch(`/api/notes/${noteId}/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pm_email }),
    }).then(json<{ note_id: number; pm_email: string; pb_status: number | null; pb_error: string | null; was_override: boolean }>),

  skip: (noteId: number) =>
    fetch(`/api/notes/${noteId}/skip`, { method: "POST" }).then(
      json<{ note_id: number; state: string }>,
    ),

  run: () =>
    fetch("/api/run", { method: "POST" }).then(
      json<{ ingest: Record<string, number>; classify: Record<string, number | string> }>,
    ),

  dashboard: () => fetch("/api/dashboard").then(json<DashboardStats>),

  scopesList: () =>
    fetch("/api/scopes").then(
      json<{ combined_hash: string; pm_emails: string[] }>,
    ),

  scope: (pm_email: string) =>
    fetch(`/api/scopes/${encodeURIComponent(pm_email)}`).then(
      json<{
        pm_email: string;
        yaml_content: string;
        history: { id: number; source: string; notes: string; created_at: string }[];
      }>,
    ),

  logsTail: (lines = 300) =>
    fetch(`/api/logs/tail?lines=${lines}`).then(json<LogTail>),

  runs: (limit = 50) =>
    fetch(`/api/runs?limit=${limit}`).then(json<{ runs: RunRow[] }>),

  proposeTraining: () =>
    fetch("/api/train/propose", { method: "POST" }).then(
      json<{ proposals: ScopeProposal[] }>,
    ),

  applyTraining: (body: {
    pm_email: string;
    yaml_content: string;
    rationale_no: string;
    sample_size: number;
  }) =>
    fetch("/api/train/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<{ ok: boolean }>),
};

export function parseTags(tagsJson: string | null | undefined): string[] {
  if (!tagsJson) return [];
  try {
    const v = JSON.parse(tagsJson) as unknown;
    if (Array.isArray(v)) return v.filter((t): t is string => typeof t === "string");
    return [];
  } catch {
    return [];
  }
}

export function confidenceColor(c: number | null | undefined): string {
  if (c == null) return "bg-slate-200 text-slate-700";
  if (c >= 0.8) return "bg-emerald-100 text-emerald-800";
  if (c >= 0.6) return "bg-sky-100 text-sky-800";
  if (c >= 0.4) return "bg-amber-100 text-amber-800";
  return "bg-rose-100 text-rose-800";
}
