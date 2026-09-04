# Agent Design Record — NEH Skill-Library Repair Agent

Design record per `ai-agent-patterns`. Machine-readable mirror:
`schemas/agent-design-spec.json`.

## 1. Agent type

**Task-Automation agent.** Bounded input (a library root on disk), bounded output
(`.skill` packages + report + run log), deterministic tools, no open-ended goals.
Not a conversational agent, not a researcher: it acts on evidence an algorithm can
prove and refuses everything else.

## 2. Patterns

### Plan-and-Execute
One planning phase — the full-library scan — produces the complete defect register
before any patch is applied. Executors then work the register unit by unit. Rationale:
patch decisions must see the whole library state (e.g. a name collision only shows up
across units), and a scan-then-act shape makes the run resumable and auditable: the
register *is* the plan.

### Reflection gate
No patch reaches packaging without passing its verification probe:

- Render patches → `render_probe` (template inventory + frame geometry + font check
  against the brand standard; per-page when a live render is possible, static
  otherwise).
- All other patches → targeted re-scan of exactly the defect codes that were patched.

A failed probe reverts the patch (the working copy is restored from the pristine
copy), logs S1, and blocks packaging for that unit. The gate is fail-closed: probe
error = probe fail.

### Explicitly rejected patterns
- **ReAct free-loop** — rejected: the defect space is enumerable; an open loop invites
  patch-thrash. Caps: one repair pass + one re-verify per unit; the run aborts rather
  than loops.
- **Multi-agent crew** — rejected: one process, five tools; coordination overhead buys
  nothing.
- **Vector memory** — rejected per the ai-agent-patterns memory rule: no measured
  context-overflow case. In-context register during the run; episodic JSON run log on
  disk after it.

## 3. Tool boundaries

Every tool returns `{ok, data, error}`. An empty `data` with no `error` is itself an
S1 defect (`TOOL_EMPTY_RESULT`).

### `scan_library(root)`
Enumerates unit folders; runs tree lint, frontmatter checks, text-only glyph scan,
provenance checks, and engine detection; emits the defect register.
**WHEN NOT TO USE:** never point it at a directory that is not a skill library root
(it will report every subfolder as a broken unit); never use it to scan generated
output directories.

### `render_probe(unit)`
Static verification of a ReportLab engine script: PageTemplate inventory (`First` +
`Later`), 72.0/540.0 frame geometry, decoration hook on every template, font
references vs the brand allowlist. Runs a live import-and-render only when ReportLab
is installed; otherwise records `mode: static` in its result — it never silently
upgrades a static pass into a claim about rendered pages.
**WHEN NOT TO USE:** non-engine units (no ReportLab build present) — a probe on them
is meaningless and its PASS would be misread as render evidence.

### `apply_patch(unit, spec)`
Applies exactly one patch spec: the mechanical edit, the PATCH-level version bump,
a changelog entry, and the §1.8 provenance re-stamp. Writes only inside the working
copy under `--out`; the live library path is opened read-only by design — there is no
code path that writes under `--root`.
**WHEN NOT TO USE:** any JUDGMENT-class defect; any defect without a spec; a second
repair pass on a unit whose first pass already ran.

### `package_skill(unit)`
Zips the repaired working copy to `<name>-v<version>.skill`, excluding PDFs (logged),
then re-opens the archive and re-asserts the packaging invariants (single top-level
folder, exactly one SKILL.md, no PDFs, zip integrity, glyph-clean SKILL.md).
Fail-closed: any STOP defect on re-open deletes the emitted archive and logs S1.
**WHEN NOT TO USE:** a unit with an unprobed or probe-failed patch; a unit skipped by
precedence rule 2 (no SKILL.md / unparseable frontmatter).

### `write_report(run)`
Renders `templates/repair-report.tmpl.md` with the register, patches, probes,
packages, escalations, and the save-back instruction; writes the episodic JSON run
log beside it. Zero-defect runs still produce both files.
**WHEN NOT TO USE:** never skipped — every run ends here, including aborted ones.

## 4. State

| State | Where | Lifetime |
|---|---|---|
| Defect register | In-context (in-process dict) | The run |
| Working copies | `<out>/work/<unit>/` | Kept after the run for inspection |
| Pristine copies (revert source) | `<out>/pristine/<unit>/` | The run |
| Packages | `<out>/packages/*.skill` | Deliverable |
| Report + run log | `<out>/repair-report.md`, `<out>/run-log.json` | Deliverable |

## 5. Pitfall map (ai-agent-patterns → this agent)

| Pitfall | Mitigation here |
|---|---|
| Unbounded loops | Hard cap: one repair pass + one re-verify per unit; abort beyond. |
| Silent tool failure | Structured `{ok, data, error}` everywhere; empty-on-failure is S1. |
| Prompt injection via processed content | Skill bodies are data. Instruction-like text is logged `BODY_INSTRUCTION_LIKE` S3 and never executed. |
| Overreach into judgment | Taxonomy is a closed list; unclassified → escalate; JUDGMENT → never patched. |
| Destructive writes | No write path to `--root`; PDFs excluded, never deleted; reverts restore from pristine copies. |
| False compliance claims | Report language asserts only "named mechanical invariant now passes". |
| Version drift claims | Installed version is only asserted after an on-disk check; otherwise the report states the drift risk. |

## 6. Escalation contract

Escalations go to the Medical Director, in the report's escalation table, each with:
unit, defect code, class, severity (S0–S4), evidence (file + location), and why the
fix is judgment. Brand, clinical, and legal rulings are always escalations regardless
of how mechanical the edit would be.

## 7. Handoff

The agent's output boundary is `--out`. Save-back into the live library is a **human
action**; the report's final section states it, with a verify-first instruction:
confirm the on-disk installed version matches the report's "before" version for each
unit prior to overwriting. Router/graph rewiring after save-back belongs to
`neh-skill-registrar`; drift monitoring of repair reports belongs to
`neh-drift-sentinel`.
