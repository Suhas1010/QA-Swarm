# QA Swarm — Demo Video Script (2.5–3 min)

Target structure matching Section 11 of the PRD:

1. **0:00–0:15 — Hook & Launch**
   - *"AI bug reports are noisy. Ours are verified."*
   - Open `http://127.0.0.1:5173/`. Enter target URL (`http://127.0.0.1:3100`), select all 5 persona workers, and click **Launch Swarm** (or **Replay Demo** for zero-risk recorded playback).
   - Show the 5 live Playwright browser tiles rendering in real time.

2. **0:15–1:15 — Live Verification & Bug Report Proof**
   - Point out the live bug feed transitioning badges: `Detected` → `Verifying` → `Verified`.
   - Click a **Verified** bug (e.g., `SAVE50` HTTP 500 or Catalog XSS) to open the Bug Detail modal.
   - Play the **fresh-browser `.webm` verification recording**, show the numbered repro steps with step screenshots, console/network excerpts, and GitHub Issue / Markdown export.

3. **1:15–2:00 — Rejecting a False Positive On Screen**
   - Highlight a **`Rejected`** item in the live verification feed (e.g., transient analytics console error or simulated 1-off noise).
   - Point out the `0/3` / `1/3` reproduction reason explaining why QA Swarm refused to file a flaky bug report.

4. **2:00–2:30 — Benchmark Charts (`Summary & Benchmarks` Tab)**
   - Switch to **Summary & Benchmarks**.
   - Show **Swarm vs. Single Agent Recall** at equal total step budget, **False-Positive Rate (Without vs. With 2-of-3 Verifier)**, total cost (`< $2`), and time to first verified bug (`< 3 min`).

5. **2:30–3:00 — Real Open-Source App Run**
   - Point QA Swarm at the local open-source target (`docs/oss_run_report.md`) and show a verified, reproducible finding that was not seeded.

