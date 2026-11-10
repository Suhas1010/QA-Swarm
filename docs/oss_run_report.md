# Real Open-Source App Verification Report (`simonw/datasette`)

To satisfy PRD Section 7 (*"run once on a real open-source app and report any unseeded bugs found — report only what actually reproduces"*), QA Swarm was pointed at a clean local instance of **Datasette** (open-source Python/SQLite web explorer) serving a sample database on `http://127.0.0.1:8100`.

- **Run ID:** `run_16e490117e9d`
- **Personas:** `hostile_fuzzer`, `mobile_viewport_user`, `confused_first_timer`, `accessibility_keyboard`, `accessibility_low_vision`
- **Steps per worker:** `10`
- **Results:** `4` suspects detected → `4` verified (`3/3` fresh-context replay) → deduplicated into `2` verified unseeded reports:

## Verified Unseeded Findings

1. **`[Medium]` Missing accessible label on custom SQL `<textarea>` (`WCAG 1.3.1`)**
   - **Route:** `/`
   - **Verification:** Reproduced `3/3` in isolated fresh browser contexts with `.webm` recording.
   - **Evidence:** The SQL query `<textarea>` on the Datasette database index page lacks an associated `<label for="...">` or `aria-label` attribute.

2. **`[Medium]` Missing accessible label on table filter/facet `<select>` (`WCAG 1.3.1`)**
   - **Route:** `/sample`
   - **Verification:** Reproduced `3/3` in isolated fresh browser contexts with `.webm` recording.
   - **Evidence:** The table view `<select>` control lacks an explicit `<label>` or `aria-label`.

