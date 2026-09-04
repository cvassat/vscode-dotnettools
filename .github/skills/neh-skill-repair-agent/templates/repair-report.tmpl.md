# NEH Skill-Library Repair Report

- **Run date:** {run_date}
- **Library root:** `{root}` (opened read-only)
- **Output dir:** `{out}`
- **Mode:** {mode}
- **Units scanned:** {units_scanned}
- **Result:** {summary_line}

> Scope note: this report asserts only that named mechanical invariants pass or fail.
> It makes no brand, clinical, legal, or regulatory compliance assertions.

## 1. Defect register

| Unit | Code | Class | Severity | Evidence |
|---|---|---|---|---|
{register_rows}

## 2. Patches applied

| Patch ID | Unit | Defect | Edit | Version | Probe |
|---|---|---|---|---|---|
{patch_rows}

## 3. Verification probes

| Unit | Probe | Mode | Result | Detail |
|---|---|---|---|---|
{probe_rows}

## 4. Packages emitted

| Package | Unit | Version | Preflight | Exclusions |
|---|---|---|---|---|
{package_rows}

## 5. Escalations (Medical Director)

JUDGMENT-class defects. None of these were patched.

| Unit | Code | Severity | Evidence | Why judgment |
|---|---|---|---|---|
{escalation_rows}

## 6. Pipeline incidents (S1)

Probe failures, reverts, packaging STOPs, empty tool results.

{incident_rows}

## 7. Save-back — human action required

Nothing in this run wrote to the live library. To install the repaired packages:

1. **Verify first:** for each unit below, confirm the installed on-disk version still
   matches the "before" version in this report. If it does not, the library drifted
   since this scan — re-run the scan instead of saving back.
2. Save each `.skill` package from `{out}/packages/` into the library.
3. After save-back is confirmed by an on-disk version check, hand wiring to
   **neh-skill-registrar** (router/graph) and file this report with
   **neh-drift-sentinel**.

| Unit | Before | After | Package |
|---|---|---|---|
{saveback_rows}

---
Run log: `{run_log_path}` (episodic JSON, same run).
