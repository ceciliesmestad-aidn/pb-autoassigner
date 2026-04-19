import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";

export default function Config() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["setup-status"],
    queryFn: api.setupStatus,
  });

  const [pbToken, setPbToken]       = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [showPb, setShowPb]         = useState(false);
  const [showAnth, setShowAnth]     = useState(false);
  const [pbTest, setPbTest]         = useState<{ ok: boolean; error?: string } | null>(null);
  const [anthTest, setAnthTest]     = useState<{ ok: boolean; error?: string } | null>(null);
  const [testingPb, setTestingPb]   = useState(false);
  const [testingAnth, setTestingAnth] = useState(false);
  const [saved, setSaved]           = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api.saveSecrets({
        ...(pbToken.trim()       ? { pb_token: pbToken.trim() }             : {}),
        ...(anthropicKey.trim()  ? { anthropic_api_key: anthropicKey.trim() } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["setup-status"] });
      setPbToken("");
      setAnthropicKey("");
      setPbTest(null);
      setAnthTest(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    },
  });

  const testPb = async () => {
    setTestingPb(true);
    setPbTest(null);
    try { setPbTest(await api.testConnection("productboard")); }
    finally { setTestingPb(false); }
  };

  const testAnth = async () => {
    setTestingAnth(true);
    setAnthTest(null);
    try { setAnthTest(await api.testConnection("anthropic")); }
    finally { setTestingAnth(false); }
  };

  const s = status.data;
  const anyChange = pbToken.trim() || anthropicKey.trim();

  return (
    <div className="max-w-xl space-y-6">
      <div>
        <h2 className="text-lg font-medium text-slate-900">Configuration</h2>
        <p className="mt-1 text-sm text-slate-500">
          API keys are saved to <code className="text-xs bg-slate-100 px-1 py-0.5 rounded">config.toml</code> on
          the server. They never leave your machine.
        </p>
      </div>

      {/* ── Productboard ── */}
      <Section
        title="Productboard"
        badge={s?.pb_token_set ? <Badge ok>Connected</Badge> : <Badge>Not set</Badge>}
      >
        <p className="text-xs text-slate-500 mb-3">
          Found in Productboard → <strong>Settings → Integrations → API keys</strong>.
          Use a <em>live</em> token (<code>pb_live_…</code>).
        </p>

        {s?.pb_token_set && (
          <div className="mb-2 text-xs text-slate-500">
            Current token: <code className="font-mono">{s.pb_token_preview}</code>
          </div>
        )}

        <div className="flex gap-2">
          <div className="relative flex-1">
            <input
              type={showPb ? "text" : "password"}
              value={pbToken}
              onChange={(e) => { setPbToken(e.target.value); setPbTest(null); }}
              placeholder={s?.pb_token_set ? "Enter new token to replace…" : "pb_live_…"}
              className="input w-full pr-10 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => setShowPb((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 text-xs"
            >
              {showPb ? "hide" : "show"}
            </button>
          </div>
          <button
            onClick={testPb}
            disabled={testingPb}
            className="px-3 py-1.5 text-sm rounded-md border border-slate-200 hover:bg-slate-50 disabled:opacity-50"
          >
            {testingPb ? "Testing…" : "Test"}
          </button>
        </div>
        <TestResult result={pbTest} />
      </Section>

      {/* ── Anthropic ── */}
      <Section
        title="Anthropic"
        badge={s?.anthropic_key_set ? <Badge ok>Connected</Badge> : <Badge>Not set</Badge>}
      >
        <p className="text-xs text-slate-500 mb-3">
          Found at <strong>console.anthropic.com → API keys</strong>. Starts with{" "}
          <code>sk-ant-…</code>.
        </p>

        {s?.anthropic_key_set && (
          <div className="mb-2 text-xs text-slate-500">
            Current key: <code className="font-mono">{s.anthropic_key_preview}</code>
          </div>
        )}

        <div className="flex gap-2">
          <div className="relative flex-1">
            <input
              type={showAnth ? "text" : "password"}
              value={anthropicKey}
              onChange={(e) => { setAnthropicKey(e.target.value); setAnthTest(null); }}
              placeholder={s?.anthropic_key_set ? "Enter new key to replace…" : "sk-ant-…"}
              className="input w-full pr-10 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => setShowAnth((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 text-xs"
            >
              {showAnth ? "hide" : "show"}
            </button>
          </div>
          <button
            onClick={testAnth}
            disabled={testingAnth}
            className="px-3 py-1.5 text-sm rounded-md border border-slate-200 hover:bg-slate-50 disabled:opacity-50"
          >
            {testingAnth ? "Testing…" : "Test"}
          </button>
        </div>
        <TestResult result={anthTest} />
      </Section>

      {/* ── Save ── */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || !anyChange}
          className="px-4 py-2 text-sm rounded-md bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {saved && (
          <span className="text-sm text-emerald-700">Saved — config reloaded.</span>
        )}
        {save.isError && (
          <span className="text-sm text-rose-700">
            Error: {(save.error as Error).message}
          </span>
        )}
        {!anyChange && !saved && (
          <span className="text-xs text-slate-400">Enter a value above to save</span>
        )}
      </div>

      {/* ── Overall status ── */}
      {s && !s.fully_configured && (
        <div className="rounded-md bg-amber-50 border border-amber-200 px-4 py-3 text-sm text-amber-800">
          <strong>Setup not complete.</strong> Enter and save both API keys to start using the app.
        </div>
      )}
    </div>
  );
}

// ── helpers ───────────────────────────────────────────────────────────────────

function Section({
  title,
  badge,
  children,
}: {
  title: string;
  badge: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-md p-4 space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-900">{title}</h3>
        {badge}
      </div>
      {children}
    </div>
  );
}

function Badge({ ok, children }: { ok?: boolean; children: React.ReactNode }) {
  return (
    <span
      className={[
        "text-xs px-2 py-0.5 rounded-full font-medium",
        ok
          ? "bg-emerald-100 text-emerald-700"
          : "bg-slate-100 text-slate-500",
      ].join(" ")}
    >
      {children}
    </span>
  );
}

function TestResult({ result }: { result: { ok: boolean; error?: string } | null }) {
  if (!result) return null;
  if (result.ok) {
    return <p className="text-xs text-emerald-700 mt-1.5">✓ Connection successful</p>;
  }
  return (
    <p className="text-xs text-rose-700 mt-1.5 break-words">
      ✗ {result.error || "Connection failed"}
    </p>
  );
}
