# QA Swarm — Bug Reports You Can Trust

Give QA Swarm a URL. Five parallel persona-driven Playwright agents explore your app with Set-of-Marks vision grounding, a hybrid detector catches objective and visual failures, and a deterministic 2-of-3 replay verifier filters out flaky noise before filing deduplicated GitHub issues (or Markdown reports) with repro steps, logs, and verification video.

## Architecture

```text
Target URL ─▶ Orchestrator ─▶ 5 Parallel Persona Workers (Playwright + SoM + Gemini Flash)
                                  │
                                  ▼
                          Hybrid Detector (Objective Rules + Selective Gemini Pro)
                                  │
                                  ▼
                          Suspect Queue (Detected)
                                  │
                                  ▼
                    2-of-3 Replay Verifier (Fresh Context + Video Recording)
                       ├── Rejected (<2/3) ──▶ Metrics (False Positive Reduction)
                       └── Verified (>=2/3) ─▶ Deduplicator ─▶ Gemini Pro Report ─▶ GitHub Issues / Markdown
```

## Quick Start (Under 5 Commands)

```powershell
# 1. Python environment & Playwright Chromium
python -m venv .venv
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
.\.venv\Scripts\playwright.exe install chromium

# 2. Frontend dependencies
npm --prefix frontend install

# 3. Configure Gemini API key (optional GitHub token for auto-filing)
Copy-Item .env.example .env

# 4. Start QA Swarm Backend (port 8000), Dashboard (port 5173), and Seeded Test App (port 3100)
Set-Location test-app; ..\.venv\Scripts\python.exe -m uvicorn server:app --port 3100
Set-Location backend;  ..\.venv\Scripts\python.exe cli.py serve --port 8000
npm --prefix frontend run dev
```

Open the QA Swarm Dashboard at **`http://localhost:5173`**.

## CLI & Benchmark Commands

```powershell
# Run full 5-worker swarm headlessly
.\.venv\Scripts\python.exe backend\cli.py run-swarm --url http://127.0.0.1:3100 --steps 10

# Run Single-Agent vs Swarm benchmark against NovaCart (18 seeded bugs)
.\.venv\Scripts\python.exe benchmark\run_benchmark.py --steps 12

# Run test suite
.\.venv\Scripts\python.exe -m pytest backend\tests -v
```
