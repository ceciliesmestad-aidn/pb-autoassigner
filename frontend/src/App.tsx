import { useState } from "react";
import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import Reviewer from "./pages/Reviewer";
import Dashboard from "./pages/Dashboard";
import Training from "./pages/Training";
import ConsolePage from "./pages/Console";
import ConfigPage from "./pages/Config";

type Mode = "manual" | "autopilot";

export default function App() {
  const qc = useQueryClient();
  const [mode, setMode] = useState<Mode>(
    () => (localStorage.getItem("pb-assigner.mode") as Mode) || "manual"
  );
  const run = useMutation({
    mutationFn: api.run,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["suggestions"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const setModePersist = (m: Mode) => {
    setMode(m);
    localStorage.setItem("pb-assigner.mode", m);
  };

  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-6">
          <div className="font-semibold text-slate-900">PB AutoAssigner</div>
          <nav className="flex gap-1 text-sm">
            <TabLink to="/reviewer">Reviewer</TabLink>
            <TabLink to="/dashboard">Dashboard</TabLink>
            <TabLink to="/training">Training</TabLink>
            <TabLink to="/console">Console</TabLink>
            <TabLink to="/config">Config</TabLink>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <ModeToggle mode={mode} onChange={setModePersist} />
            {run.isSuccess && !run.isPending && (
              <span className="text-xs text-slate-500">
                last fetch: {String(run.data?.ingest.inserted)} new, {" "}
                {String(run.data?.ingest.reconciled ?? 0)} reconciled, {" "}
                {String(run.data?.classify.classified)} classified
              </span>
            )}
            <button
              onClick={() => run.mutate()}
              disabled={run.isPending}
              className="text-sm px-3 py-1.5 rounded-md bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {run.isPending ? "Fetching…" : "Fetch notes"}
            </button>
          </div>
        </div>
      </header>
      <main className="flex-1 max-w-7xl mx-auto w-full px-6 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/reviewer" replace />} />
          <Route path="/reviewer" element={<Reviewer />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/training" element={<Training />} />
          <Route path="/console" element={<ConsolePage />} />
          <Route path="/config" element={<ConfigPage />} />
        </Routes>
      </main>
      {run.isError && (
        <div className="fixed bottom-4 right-4 bg-rose-100 border border-rose-300 text-rose-900 text-sm rounded-md px-3 py-2 max-w-md shadow">
          <div className="font-medium mb-0.5">Run failed</div>
          <div className="text-xs mb-1 break-words">{(run.error as Error).message}</div>
          <NavLink
            to="/console"
            className="text-xs underline text-rose-800 hover:text-rose-900"
          >
            Open Console →
          </NavLink>
        </div>
      )}
    </div>
  );
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
}) {
  const base =
    "text-xs px-2.5 py-1 rounded-md transition-colors font-medium";
  return (
    <div
      className="flex items-center gap-0.5 p-0.5 rounded-md bg-slate-100 border border-slate-200"
      role="tablist"
      aria-label="Assignment mode"
    >
      <button
        type="button"
        onClick={() => onChange("manual")}
        className={[
          base,
          mode === "manual"
            ? "bg-white text-slate-900 shadow-sm"
            : "text-slate-500 hover:text-slate-700",
        ].join(" ")}
      >
        Manual
      </button>
      <button
        type="button"
        onClick={() => onChange("autopilot")}
        className={[
          base,
          mode === "autopilot"
            ? "bg-white text-slate-900 shadow-sm"
            : "text-slate-500 hover:text-slate-700",
        ].join(" ")}
      >
        Autopilot
      </button>
    </div>
  );
}

function TabLink({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        [
          "px-3 py-1.5 rounded-md",
          isActive
            ? "bg-slate-100 text-slate-900 font-medium"
            : "text-slate-600 hover:bg-slate-50",
        ].join(" ")
      }
    >
      {children}
    </NavLink>
  );
}
