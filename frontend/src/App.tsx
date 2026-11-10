import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  BarChart3,
  Bug,
  CheckCircle2,
  ExternalLink,
  FileDown,
  Play,
  RotateCcw,
  X,
  XCircle,
} from "lucide-react";
import type {
  BenchmarkResults,
  BugDetailResponse,
  BugReportItem,
  PersonaInfo,
  RunRow,
  RunSummary,
  SuspectItem,
  WorkerTile,
} from "./types";

const DEFAULT_PERSONAS = [
  "impatient_user",
  "hostile_fuzzer",
  "confused_first_timer",
  "mobile_viewport_user",
  "edge_explorer",
];

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "Verified"
      ? "badge badge-verified"
      : status === "Rejected"
      ? "badge badge-rejected"
      : status === "Verifying"
      ? "badge badge-verifying"
      : "badge badge-detected";
  return <span className={cls}>{status}</span>;
}

export default function App() {
  const [tab, setTab] = useState<"live" | "summary">("live");
  const [health, setHealth] = useState<{ gemini_configured: boolean; nav_model: string } | null>(null);
  const [personas, setPersonas] = useState<PersonaInfo[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [benchmarks, setBenchmarks] = useState<BenchmarkResults | null>(null);

  // Launcher form state
  const [targetUrl, setTargetUrl] = useState("http://127.0.0.1:3100");
  const [mode, setMode] = useState<"swarm" | "single">("swarm");
  const [stepsPerWorker, setStepsPerWorker] = useState(8);
  const [githubRepo, setGithubRepo] = useState("");
  const [selectedPersonas, setSelectedPersonas] = useState<string[]>(DEFAULT_PERSONAS);
  const [mockLlm, setMockLlm] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [credEmail, setCredEmail] = useState("");
  const [credPassword, setCredPassword] = useState("");
  const [launching, setLaunching] = useState(false);

  // Active run live state
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runMeta, setRunMeta] = useState<RunRow | null>(null);
  const [workers, setWorkers] = useState<WorkerTile[]>([]);
  const [suspects, setSuspects] = useState<SuspectItem[]>([]);
  const [bugReports, setBugReports] = useState<BugReportItem[]>([]);
  const [summary, setSummary] = useState<RunSummary | null>(null);

  // Bug detail modal
  const [bugDetail, setBugDetail] = useState<BugDetailResponse | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  async function refreshMetadata() {
    const [h, p, r, b] = await Promise.all([
      fetch("/api/health").then((x) => x.json()),
      fetch("/api/personas").then((x) => x.json()),
      fetch("/api/runs").then((x) => x.json()),
      fetch("/api/benchmarks").then((x) => x.json()),
    ]);
    setHealth(h);
    setPersonas(p);
    setRuns(r);
    setBenchmarks(b);
    if (!activeRunId && r.length > 0) {
      loadRunSnapshot(r[0].id);
    }
  }

  async function loadRunSnapshot(runId: string) {
    setActiveRunId(runId);
    const data = await fetch(`/api/runs/${runId}`).then((x) => x.json());
    setRunMeta(data.run);
    setWorkers(data.workers || []);
    setSuspects(data.suspects || []);
    setBugReports(data.bug_reports || []);
    setSummary(data.summary || null);
  }

  useEffect(() => {
    refreshMetadata();
  }, []);

  // Poll / WebSocket sync for active run
  useEffect(() => {
    if (!activeRunId) return;
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/runs/${activeRunId}`);
    wsRef.current = ws;

    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      const p = msg.payload || {};

      if (msg.type === "worker_frame") {
        setWorkers((prev) => {
          const idx = prev.findIndex((w) => w.id === p.worker_id);
          const updated: WorkerTile = {
            id: p.worker_id,
            persona: p.persona,
            viewport: p.viewport,
            status: "running",
            steps_used: p.step_index,
            max_steps: p.max_steps,
            current_url: p.url,
            last_action: p.last_action,
            thought: p.thought,
            latest_image_url: p.image_url,
            frame_b64: p.frame_b64,
          };
          if (idx === -1) return [...prev, updated];
          const copy = [...prev];
          copy[idx] = { ...copy[idx], ...updated };
          return copy;
        });
      } else if (
        msg.type === "suspect_detected" ||
        msg.type === "suspect_verifying" ||
        msg.type === "suspect_verified" ||
        msg.type === "suspect_rejected"
      ) {
        loadRunSnapshot(activeRunId);
      } else if (msg.type === "bug_reported" || msg.type === "run_completed") {
        loadRunSnapshot(activeRunId);
        fetch("/api/runs")
          .then((x) => x.json())
          .then(setRuns);
      }
    };

    const timer = window.setInterval(() => {
      if (runMeta?.status === "running") {
        loadRunSnapshot(activeRunId);
      }
    }, 3000);

    return () => {
      window.clearInterval(timer);
      ws.close();
    };
  }, [activeRunId, runMeta?.status]);

  async function handleLaunch(e: React.FormEvent) {
    e.preventDefault();
    setLaunching(true);
    try {
      const res = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_url: targetUrl,
          mode,
          personas: selectedPersonas,
          steps_per_worker: stepsPerWorker,
          github_repo: githubRepo,
          credentials: credEmail ? { email: credEmail, password: credPassword } : null,
          mock_llm: mockLlm,
        }),
      });
      const data = await res.json();
      setTab("live");
      await loadRunSnapshot(data.run_id);
    } finally {
      setLaunching(false);
    }
  }

  async function handleDemoReplay() {
    setLaunching(true);
    try {
      const res = await fetch("/api/demo/replay", { method: "POST" });
      const data = await res.json();
      setTab("live");
      await loadRunSnapshot(data.run_id);
    } finally {
      setLaunching(false);
    }
  }

  async function openBugForSuspect(suspectId: string) {
    const matching = bugReports.find((b) => b.primary_suspect_id === suspectId);
    if (!matching) return;
    const detail: BugDetailResponse = await fetch(`/api/bugs/${matching.id}`).then((x) => x.json());
    setBugDetail(detail);
  }

  const reportBySuspect = useMemo(() => {
    const map = new Map<string, BugReportItem>();
    for (const b of bugReports) map.set(b.primary_suspect_id, b);
    return map;
  }, [bugReports]);

  function togglePersona(name: string) {
    setSelectedPersonas((prev) =>
      prev.includes(name) ? prev.filter((p) => p !== name) : [...prev, name]
    );
  }

  return (
    <div>
      <header className="topbar">
        <div className="brand-wrap">
          <Bug size={24} color="#38bdf8" />
          <div>
            <div className="brand-title">QA Swarm</div>
            <div className="brand-sub">
              Parallel persona testing + deterministic replay verification
            </div>
          </div>
        </div>

        <div className="tabs">
          <button
            className={`tab-btn ${tab === "live" ? "active" : ""}`}
            onClick={() => setTab("live")}
          >
            <Activity size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
            Swarm Grid &amp; Feed
          </button>
          <button
            className={`tab-btn ${tab === "summary" ? "active" : ""}`}
            onClick={() => {
              setTab("summary");
              fetch("/api/benchmarks")
                .then((x) => x.json())
                .then(setBenchmarks);
            }}
          >
            <BarChart3 size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
            Summary &amp; Benchmarks
          </button>
        </div>

        <div style={{ fontSize: "0.82rem", color: "#94a3b8" }}>
          {health?.gemini_configured ? (
            <span className="badge badge-verified">Gemini: {health.nav_model}</span>
          ) : (
            <span className="badge badge-detected">Offline / Mock Mode</span>
          )}
        </div>
      </header>

      <main className="shell">
        {/* Run Launcher */}
        <section className="panel" style={{ marginBottom: "1rem" }}>
          <form onSubmit={handleLaunch}>
            <div className="launcher-grid">
              <div className="field">
                <label>Target Web App URL</label>
                <input
                  value={targetUrl}
                  onChange={(e) => setTargetUrl(e.target.value)}
                  placeholder="http://127.0.0.1:3100"
                  required
                />
              </div>

              <div className="field">
                <label>Execution Mode</label>
                <select value={mode} onChange={(e) => setMode(e.target.value as "swarm" | "single")}>
                  <option value="swarm">Swarm (5 Parallel Workers)</option>
                  <option value="single">Single Agent Baseline</option>
                </select>
              </div>

              <div className="field">
                <label>Steps Per Worker</label>
                <input
                  type="number"
                  min={2}
                  max={40}
                  value={stepsPerWorker}
                  onChange={(e) => setStepsPerWorker(Number(e.target.value))}
                />
              </div>

              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button className="btn-primary" type="submit" disabled={launching}>
                  <Play size={15} /> {launching ? "Starting..." : "Launch Swarm"}
                </button>
                <button
                  className="btn-secondary"
                  type="button"
                  onClick={handleDemoReplay}
                  disabled={launching}
                >
                  <RotateCcw size={14} /> Replay Demo
                </button>
              </div>
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: "0.75rem",
                flexWrap: "wrap",
                gap: "0.6rem",
              }}
            >
              <div style={{ display: "flex", gap: "0.55rem", flexWrap: "wrap", alignItems: "center" }}>
                <span style={{ fontSize: "0.78rem", color: "#94a3b8" }}>Personas:</span>
                {(personas.length > 0
                  ? personas.map((p) => p.name)
                  : DEFAULT_PERSONAS
                ).map((pName) => (
                  <label
                    key={pName}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.3rem",
                      fontSize: "0.8rem",
                      background: "#0b1220",
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: "0.2rem 0.5rem",
                      cursor: "pointer",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selectedPersonas.includes(pName)}
                      onChange={() => togglePersona(pName)}
                    />
                    {pName}
                  </label>
                ))}
              </div>

              <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
                <label style={{ fontSize: "0.8rem", display: "inline-flex", gap: "0.35rem", alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={mockLlm}
                    onChange={(e) => setMockLlm(e.target.checked)}
                  />
                  Offline Heuristic (No API Quota)
                </label>
                <button
                  type="button"
                  className="btn-secondary"
                  style={{ padding: "0.28rem 0.65rem", fontSize: "0.78rem" }}
                  onClick={() => setShowAdvanced((v) => !v)}
                >
                  {showAdvanced ? "Hide Options" : "GitHub & Auth"}
                </button>
                {runs.length > 0 && (
                  <select
                    value={activeRunId || ""}
                    onChange={(e) => loadRunSnapshot(e.target.value)}
                    style={{
                      background: "#0b1220",
                      color: "#e2e8f0",
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: "0.28rem 0.5rem",
                      fontSize: "0.8rem",
                    }}
                  >
                    {runs.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.id} ({r.mode} · {r.status})
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </div>

            {showAdvanced && (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr 1fr",
                  gap: "0.75rem",
                  marginTop: "0.75rem",
                  paddingTop: "0.75rem",
                  borderTop: "1px solid var(--border)",
                }}
              >
                <div className="field">
                  <label>GitHub Repo (owner/repo)</label>
                  <input
                    value={githubRepo}
                    onChange={(e) => setGithubRepo(e.target.value)}
                    placeholder="suhas/my-app (optional)"
                  />
                </div>
                <div className="field">
                  <label>Test Login Email (optional)</label>
                  <input
                    value={credEmail}
                    onChange={(e) => setCredEmail(e.target.value)}
                    placeholder="demo@novacart.test"
                  />
                </div>
                <div className="field">
                  <label>Test Login Password (optional)</label>
                  <input
                    type="password"
                    value={credPassword}
                    onChange={(e) => setCredPassword(e.target.value)}
                    placeholder="Password123!"
                  />
                </div>
              </div>
            )}
          </form>
        </section>

        {/* KPI Bar */}
        {summary && (
          <section className="kpi-strip">
            <div className="kpi-card">
              <div className="kpi-label">Run Status</div>
              <div className="kpi-val" style={{ fontSize: "1.1rem", color: "#38bdf8" }}>
                {runMeta?.status?.toUpperCase() || "IDLE"} ({runMeta?.mode})
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">Steps Executed</div>
              <div className="kpi-val">{summary.steps_used}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">Verified Bugs</div>
              <div className="kpi-val" style={{ color: "#34d399" }}>
                {summary.verified_count}
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">Rejected Noise</div>
              <div className="kpi-val" style={{ color: "#f87171" }}>
                {summary.rejected_count}
              </div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">LLM Cost</div>
              <div className="kpi-val">${summary.total_cost_usd.toFixed(4)}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">First Verified</div>
              <div className="kpi-val">
                {summary.time_to_first_verified_sec !== null
                  ? `${summary.time_to_first_verified_sec}s`
                  : "—"}
              </div>
            </div>
          </section>
        )}

        {tab === "live" ? (
          <div className="workspace-split">
            {/* 5-Browser Live Grid */}
            <section>
              <h2 style={{ margin: "0 0 0.7rem", fontSize: "1rem", color: "#cbd5e1" }}>
                Live Swarm Browsers ({workers.length})
              </h2>
              {workers.length === 0 ? (
                <div className="panel" style={{ color: "#94a3b8" }}>
                  Click <strong>Launch Swarm</strong> above to start 5 parallel Playwright persona workers.
                </div>
              ) : (
                <div className="browser-grid">
                  {workers.map((w) => {
                    const imgSrc = w.frame_b64
                      ? `data:image/jpeg;base64,${w.frame_b64}`
                      : w.latest_image_url || undefined;
                    return (
                      <div className="worker-card" key={w.id}>
                        <div className="worker-head">
                          <strong>{w.persona}</strong>
                          <span style={{ color: "#94a3b8" }}>
                            {w.viewport} · {w.steps_used}/{w.max_steps}
                          </span>
                        </div>
                        <div className="worker-frame">
                          {imgSrc ? (
                            <img src={imgSrc} alt={w.persona} />
                          ) : (
                            <span style={{ color: "#64748b", fontSize: "0.8rem" }}>
                              Booting browser...
                            </span>
                          )}
                        </div>
                        <div className="worker-foot">
                          <div className="truncate" style={{ color: "#38bdf8" }}>
                            {w.current_url || "about:blank"}
                          </div>
                          <div className="truncate" style={{ color: "#94a3b8", marginTop: 2 }}>
                            {w.thought || w.last_action || "Initializing..."}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>

            {/* Live Bug Feed */}
            <section className="panel">
              <h2 style={{ margin: "0 0 0.75rem", fontSize: "1rem", color: "#cbd5e1" }}>
                Live Verification Feed ({suspects.length})
              </h2>
              {suspects.length === 0 ? (
                <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>
                  No suspected bugs detected yet. Workers are exploring...
                </p>
              ) : (
                <div className="feed-list">
                  {suspects.map((s) => {
                    const report = reportBySuspect.get(s.id);
                    return (
                      <div
                        key={s.id}
                        className={`bug-card ${s.status === "Rejected" ? "rejected" : ""}`}
                        onClick={() => openBugForSuspect(s.id)}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            gap: "0.5rem",
                          }}
                        >
                          <StatusBadge status={s.status} />
                          <span style={{ fontSize: "0.74rem", color: "#94a3b8" }}>
                            {s.category} · {s.persona}
                          </span>
                        </div>
                        <div style={{ fontWeight: 600, fontSize: "0.88rem", marginTop: "0.45rem" }}>
                          {report ? report.title : s.title_hint}
                        </div>
                        <div
                          className="truncate"
                          style={{ fontSize: "0.78rem", color: "#94a3b8", marginTop: 3 }}
                        >
                          {s.url}
                        </div>
                        {s.status === "Verified" && (
                          <div
                            style={{
                              fontSize: "0.76rem",
                              color: "#34d399",
                              marginTop: "0.35rem",
                              display: "flex",
                              alignItems: "center",
                              gap: 4,
                            }}
                          >
                            <CheckCircle2 size={13} /> Reproduced {s.repro_count}/3 ·{" "}
                            {report ? "Click for video & logs" : "Deduplicated"}
                          </div>
                        )}
                        {s.status === "Rejected" && (
                          <div
                            style={{
                              fontSize: "0.76rem",
                              color: "#f87171",
                              marginTop: "0.35rem",
                              display: "flex",
                              alignItems: "center",
                              gap: 4,
                            }}
                          >
                            <XCircle size={13} /> {s.rejection_reason || "Failed 2/3 replay"}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          </div>
        ) : (
          /* Summary & Benchmark Tab */
          <section style={{ display: "grid", gap: "1rem" }}>
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Filed Verified Bug Reports ({bugReports.length})</h2>
              {bugReports.length === 0 ? (
                <p style={{ color: "#94a3b8" }}>No verified bug reports filed for the selected run yet.</p>
              ) : (
                <div style={{ display: "grid", gap: "0.6rem" }}>
                  {bugReports.map((b) => (
                    <div
                      key={b.id}
                      className="bug-card"
                      onClick={async () => {
                        const detail = await fetch(`/api/bugs/${b.id}`).then((x) => x.json());
                        setBugDetail(detail);
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <strong>
                          [{b.severity}] {b.title}
                        </strong>
                        <span className="badge badge-verified">{b.category}</span>
                      </div>
                      <div style={{ fontSize: "0.82rem", color: "#94a3b8", marginTop: 4 }}>
                        {b.github_issue_url ? `GitHub: ${b.github_issue_url}` : `Exported: ${b.markdown_export_path}`}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Benchmark Results (NovaCart Ground Truth)</h2>
              {!benchmarks || benchmarks.available === false ? (
                <p style={{ color: "#94a3b8" }}>
                  Run <code>.\.venv\Scripts\python.exe benchmark\run_benchmark.py</code> to generate benchmark metrics and charts.
                </p>
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.25rem" }}>
                  <div>
                    <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>
                      Recall at Equal Step Budget (Swarm vs. Single Agent)
                    </h3>
                    <div style={{ marginBottom: "0.9rem" }}>
                      <div style={{ fontSize: "0.84rem" }}>
                        QA Swarm ({benchmarks.swarm_with_verifier?.steps_used} steps):{" "}
                        <strong>{benchmarks.swarm_with_verifier?.recall_pct}%</strong>
                      </div>
                      <div className="bar-track">
                        <div
                          className="bar-fill"
                          style={{
                            width: `${Math.min(100, benchmarks.swarm_with_verifier?.recall_pct || 0)}%`,
                            background: "#34d399",
                          }}
                        >
                          {benchmarks.swarm_with_verifier?.matched_bug_ids?.length} / {benchmarks.total_seeded_bugs}
                        </div>
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: "0.84rem" }}>
                        Single Agent ({benchmarks.single_agent?.steps_used} steps):{" "}
                        <strong>{benchmarks.single_agent?.recall_pct}%</strong>
                      </div>
                      <div className="bar-track">
                        <div
                          className="bar-fill"
                          style={{
                            width: `${Math.min(100, benchmarks.single_agent?.recall_pct || 0)}%`,
                            background: "#38bdf8",
                          }}
                        >
                          {benchmarks.single_agent?.matched_bugs} / {benchmarks.total_seeded_bugs}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div>
                    <h3 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>
                      False-Positive Rate (Without vs. With Replay Verifier)
                    </h3>
                    <div style={{ marginBottom: "0.9rem" }}>
                      <div style={{ fontSize: "0.84rem" }}>
                        Without Verifier:{" "}
                        <strong>{benchmarks.swarm_no_verifier?.false_positive_rate_pct}%</strong>
                      </div>
                      <div className="bar-track">
                        <div
                          className="bar-fill"
                          style={{
                            width: `${Math.min(100, benchmarks.swarm_no_verifier?.false_positive_rate_pct || 0)}%`,
                            background: "#f87171",
                          }}
                        >
                          {benchmarks.swarm_no_verifier?.false_positives} FP
                        </div>
                      </div>
                    </div>
                    <div>
                      <div style={{ fontSize: "0.84rem" }}>
                        With 2-of-3 Verifier:{" "}
                        <strong>{benchmarks.swarm_with_verifier?.false_positive_rate_pct}%</strong>
                      </div>
                      <div className="bar-track">
                        <div
                          className="bar-fill"
                          style={{
                            width: `${Math.max(4, Math.min(100, benchmarks.swarm_with_verifier?.false_positive_rate_pct || 0))}%`,
                            background: "#34d399",
                          }}
                        >
                          {benchmarks.swarm_with_verifier?.false_positives} FP ({benchmarks.swarm_with_verifier?.rejected_count} rejected)
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </section>
        )}
      </main>

      {/* Bug Detail Modal */}
      {bugDetail && (
        <div className="modal-backdrop" onClick={() => setBugDetail(null)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start", gap: "1rem" }}>
              <div>
                <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: "0.4rem" }}>
                  <span className="badge badge-verified">{bugDetail.bug.severity}</span>
                  <span className="badge badge-verifying">{bugDetail.bug.category}</span>
                  {bugDetail.suspect && (
                    <span style={{ fontSize: "0.8rem", color: "#34d399" }}>
                      Verified {bugDetail.suspect.repro_count}/3 replays
                    </span>
                  )}
                </div>
                <h2 style={{ margin: 0 }}>{bugDetail.bug.title}</h2>
              </div>
              <button className="btn-secondary" onClick={() => setBugDetail(null)}>
                <X size={16} /> Close
              </button>
            </div>

            <div style={{ display: "flex", gap: "0.65rem", margin: "1rem 0", flexWrap: "wrap" }}>
              {bugDetail.bug.github_issue_url && (
                <a
                  href={bugDetail.bug.github_issue_url}
                  target="_blank"
                  rel="noreferrer"
                  className="btn-primary"
                >
                  <ExternalLink size={14} /> GitHub Issue #{bugDetail.bug.github_issue_number}
                </a>
              )}
              <a
                href={`/api/bugs/${bugDetail.bug.id}/export`}
                download={`${bugDetail.bug.id}.md`}
                className="btn-secondary"
              >
                <FileDown size={14} /> Download Markdown
              </a>
            </div>

            {bugDetail.bug.video_url && (
              <div style={{ marginBottom: "1rem" }}>
                <h3 style={{ fontSize: "0.9rem", margin: "0 0 0.45rem" }}>
                  Fresh-Browser Replay Recording
                </h3>
                <video
                  src={bugDetail.bug.video_url}
                  controls
                  autoPlay
                  muted
                  style={{ width: "100%", maxHeight: 360, borderRadius: 10, background: "#000" }}
                />
              </div>
            )}

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
              <div>
                <h3 style={{ fontSize: "0.9rem" }}>Reproduction Steps</h3>
                <ol style={{ paddingLeft: "1.2rem", fontSize: "0.84rem", color: "#cbd5e1" }}>
                  {(bugDetail.suspect?.trace_slice || []).map((st) => (
                    <li key={st.step_index} style={{ marginBottom: "0.45rem" }}>
                      <code>{st.action_type}</code>{" "}
                      {st.input_value ? `(${st.input_value}) ` : ""}
                      at <span style={{ color: "#38bdf8" }}>{st.url}</span>
                    </li>
                  ))}
                </ol>

                <h3 style={{ fontSize: "0.9rem" }}>Verification Attempts</h3>
                {bugDetail.verification_attempts.map((a) => (
                  <div
                    key={a.id}
                    style={{
                      fontSize: "0.8rem",
                      padding: "0.4rem 0.6rem",
                      borderRadius: 6,
                      background: "#0b1220",
                      marginBottom: "0.35rem",
                    }}
                  >
                    <strong>Attempt #{a.attempt_num}:</strong>{" "}
                    {a.reproduced ? (
                      <span style={{ color: "#34d399" }}>Reproduced</span>
                    ) : (
                      <span style={{ color: "#f87171" }}>Not reproduced</span>
                    )}{" "}
                    — {a.notes}
                  </div>
                ))}
              </div>

              <div>
                <h3 style={{ fontSize: "0.9rem" }}>Issue Markdown</h3>
                <pre style={{ maxHeight: 220 }}>{bugDetail.bug.body_markdown}</pre>

                <h3 style={{ fontSize: "0.9rem" }}>Console &amp; Network Excerpt</h3>
                <pre style={{ maxHeight: 180 }}>
                  {JSON.stringify(
                    {
                      console: bugDetail.suspect?.console_logs || [],
                      network: bugDetail.suspect?.network_logs || [],
                    },
                    null,
                    2
                  )}
                </pre>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
