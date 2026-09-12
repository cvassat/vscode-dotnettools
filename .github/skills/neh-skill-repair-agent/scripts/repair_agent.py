#!/usr/bin/env python3
"""NEH Skill-Library Repair Agent — executable loop.

Plan-and-Execute with a Reflection gate:
  scan_library  -> defect register (the plan)
  apply_patch   -> deterministic edits in a working copy (never the live library)
  probe         -> render_probe (engine units) or targeted re-scan
  package_skill -> fail-closed .skill emission
  write_report  -> markdown report + episodic JSON run log (always, even on abort)

Stdlib only. Defect codes, classes, and severities per references/defect-taxonomy.md.
Caps: one repair pass + one re-verify per unit; the run aborts rather than loops.
"""

import argparse
import datetime
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

TEXT_EXTS = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".tmpl", ".csv",
             ".html", ".css", ".js", ".toml", ".cfg", ".ini"}
REQUIRED_FIELDS = ["name", "version", "category", "parent_skills",
                   "activation_policy", "provenance", "description"]
RESERVED_WORDS = {"anthropic", "claude"}
PROV_SURFACES = {"CLD", "LCL"}
PROV_FIELDS = ["last_modified_by", "last_modified_on", "last_modified_at"]
BLACK_SQUARE = "\u25a0"  # escapes, not literals: the agent must scan itself clean
REPLACEMENT_CHARS = {"\ufffd": "U+FFFD", "\ufffc": "U+FFFC"}
MOJIBAKE_MARKER = "\u00e2\u20ac"  # UTF-8 punctuation re-decoded as cp1252
INSTRUCTION_PATTERNS = [
    r"ignore (?:all )?previous instructions",
    r"disregard the above",
    r"you must now",
    r"system prompt override",
    r"you are now (?:a|an|the)\b",
    r"override (?:all|your) (?:previous|prior)",
    r"new instructions\s*:",
    r"do not tell the user",
]
DETERMINISTIC = "DETERMINISTIC"
JUDGMENT = "JUDGMENT"


def result(ok, data=None, error=None):
    """Every tool returns this shape; empty data with no error is itself a defect."""
    return {"ok": ok, "data": data, "error": error}


def defect(unit, code, cls, severity, file, detail):
    return {"unit": unit, "code": code, "class": cls, "severity": severity,
            "file": file, "detail": detail}


# ---------------------------------------------------------------- frontmatter

def _normalize_yaml(value):
    """Coerce PyYAML scalars to the string shapes the checks expect."""
    if isinstance(value, dict):
        return {str(k): _normalize_yaml(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_yaml(v) for v in value]
    if value is None:
        return ""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y-%m-%d")
    return value if isinstance(value, str) else str(value)


def parse_frontmatter(text):
    """Parse skill frontmatter: PyYAML when importable, else the minimal
    stdlib subset parser. Returns (dict, None) or (None, error)."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None, "no frontmatter block"
    try:
        import yaml
        loaded = yaml.safe_load(m.group(1))
        if isinstance(loaded, dict):
            return _normalize_yaml(loaded), None
    except ImportError:
        pass
    except Exception:
        pass  # malformed for PyYAML too; let the minimal parser report it
    return _parse_frontmatter_minimal(m.group(1))


def _parse_frontmatter_minimal(body):
    """Minimal YAML-subset parser (stdlib fallback).

    Handles: `key: value`, `key: >-` folded blocks, `key:` + `- item` lists,
    and one level of nested `key: value` maps (provenance).
    """
    fm, lines = {}, body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t", "-")):
            return None, f"unexpected indent at line {i + 1}: {line!r}"
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not km:
            return None, f"unparseable line {i + 1}: {line!r}"
        key, val = km.group(1), km.group(2).strip()
        i += 1
        if val in (">-", ">", "|", "|-"):
            block = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            fm[key] = " ".join(b for b in block if b)
        elif val == "":
            items, sub = [], {}
            while i < len(lines) and (lines[i].startswith(("-", " ")) and lines[i].strip()):
                s = lines[i].strip()
                if s.startswith("- "):
                    items.append(s[2:].strip())
                else:
                    sm = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", s)
                    if sm:
                        sub[sm.group(1)] = sm.group(2).strip().strip("'\"")
                i += 1
            fm[key] = items if items else sub
        else:
            fm[key] = val.strip("'\"")
    return fm, None


def is_text_file(path):
    if path.suffix.lower() not in TEXT_EXTS:
        return False
    try:
        return b"\x00" not in path.read_bytes()[:8192]
    except OSError:
        return False


# --------------------------------------------------------------------- scan

def scan_unit(unit_dir):
    """All checks for one unit folder. Returns a list of defect dicts."""
    unit = unit_dir.name
    defects = []

    # Tree lint
    skill_mds = sorted(unit_dir.rglob("SKILL.md"))
    top_skill = unit_dir / "SKILL.md"
    if not top_skill.is_file():
        defects.append(defect(unit, "TREE_NO_SKILL_MD", JUDGMENT, "S2", ".",
                              "no SKILL.md at unit top level"))
        return defects  # precedence rule 2: nothing downstream is meaningful
    if len(skill_mds) > 1:
        defects.append(defect(unit, "TREE_MULTIPLE_SKILL_MD", JUDGMENT, "S2", ".",
                              f"{len(skill_mds)} SKILL.md files"))
    for d in sorted(p for p in unit_dir.rglob("*") if p.is_dir()):
        if "{" in d.name or "}" in d.name or d.name != d.name.strip():
            defects.append(defect(unit, "TREE_STRAY_BRACE_DIR",
                                  DETERMINISTIC if not any(d.iterdir()) else JUDGMENT,
                                  "S2", str(d.relative_to(unit_dir)),
                                  "stray/brace directory"
                                  + ("" if not any(d.iterdir()) else " (non-empty)")))
    for p in sorted(unit_dir.rglob("*.pdf")):
        defects.append(defect(unit, "TREE_PDF_PRESENT", DETERMINISTIC, "S0",
                              str(p.relative_to(unit_dir)),
                              "PDF inside skill folder; will be excluded at packaging"))

    # Frontmatter
    text = top_skill.read_text(encoding="utf-8", errors="replace")
    fm, err = parse_frontmatter(text)
    if fm is None:
        defects.append(defect(unit, "FM_UNPARSEABLE", JUDGMENT, "S2", "SKILL.md", err))
        return defects
    for f in REQUIRED_FIELDS:
        if f not in fm or fm[f] in ("", [], {}):
            defects.append(defect(unit, "FM_MISSING_FIELD", JUDGMENT, "S2",
                                  "SKILL.md", f"missing required field: {f}"))
    name = fm.get("name", "")
    if name and name != unit:
        defects.append(defect(unit, "FM_NAME_MISMATCH", DETERMINISTIC, "S2",
                              "SKILL.md", f"name {name!r} != folder {unit!r}"))
    name_tokens = set(re.split(r"[^a-z0-9]+", name.lower())) if name else set()
    if name_tokens & RESERVED_WORDS:
        defects.append(defect(unit, "FM_RESERVED_WORD", JUDGMENT, "S2",
                              "SKILL.md", f"reserved word in name {name!r}"))
    desc = fm.get("description", "")
    if desc and len(desc) < 200:
        defects.append(defect(unit, "FM_DESC_TOO_SHORT", JUDGMENT, "S1",
                              "SKILL.md", f"description {len(desc)} chars (< 200)"))
    if desc and len(desc) > 1024:
        defects.append(defect(unit, "FM_DESC_TOO_LONG", JUDGMENT, "S1",
                              "SKILL.md", f"description {len(desc)} chars (> 1024)"))
    version = fm.get("version", "")
    if version and not re.fullmatch(r"\d+\.\d+\.\d+", version):
        cls = DETERMINISTIC if re.fullmatch(r"v?\d+(\.\d+){0,2}", version) else JUDGMENT
        defects.append(defect(unit, "FM_BAD_VERSION", cls, "S2", "SKILL.md",
                              f"version {version!r} not MAJOR.MINOR.PATCH"))

    # Provenance (§1.8)
    prov = fm.get("provenance", {})
    if isinstance(prov, dict) and prov:
        for f in PROV_FIELDS:
            if f not in prov or not prov[f]:
                defects.append(defect(unit, "PROV_MISSING_FIELD", JUDGMENT, "S2",
                                      "SKILL.md", f"provenance missing {f}"))
        at = prov.get("last_modified_at", "")
        if at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", at):
            cls = DETERMINISTIC if _reformat_date(at) else JUDGMENT
            defects.append(defect(unit, "PROV_BAD_DATE", cls, "S0", "SKILL.md",
                                  f"last_modified_at {at!r} not ISO"))
        on = prov.get("last_modified_on", "")
        if on and on not in PROV_SURFACES:
            defects.append(defect(unit, "PROV_BAD_SURFACE", JUDGMENT, "S0",
                                  "SKILL.md", f"last_modified_on {on!r} outside vocabulary"))

    # Glyph scan — TEXT artifacts only; binaries never read as text
    for p in sorted(unit_dir.rglob("*")):
        if not p.is_file() or not is_text_file(p):
            continue
        content = p.read_text(encoding="utf-8", errors="surrogateescape")
        rel = str(p.relative_to(unit_dir))
        if BLACK_SQUARE in content:
            defects.append(defect(unit, "GLYPH_BLACK_SQUARE", DETERMINISTIC, "S0",
                                  rel, f"{content.count(BLACK_SQUARE)} x U+25A0"))
        hits = [f"{content.count(ch)} x {label}"
                for ch, label in REPLACEMENT_CHARS.items() if ch in content]
        if hits:
            defects.append(defect(unit, "GLYPH_REPLACEMENT_CHAR", JUDGMENT, "S2",
                                  rel, ", ".join(hits)))
        if MOJIBAKE_MARKER in content:
            defects.append(defect(unit, "GLYPH_MOJIBAKE", JUDGMENT, "S2", rel,
                                  f"{content.count(MOJIBAKE_MARKER)} x mojibake marker "
                                  "(UTF-8 punctuation re-decoded as cp1252)"))

    # Instruction-like text aimed at the agent — data, never executed
    body = text[text.find("---", 3) + 3:] if "---" in text[3:] else text
    for pat in INSTRUCTION_PATTERNS:
        if re.search(pat, body, re.IGNORECASE):
            defects.append(defect(unit, "BODY_INSTRUCTION_LIKE", JUDGMENT, "S3",
                                  "SKILL.md", f"instruction-like text: /{pat}/"))

    # Render defects — engine units only
    defects.extend(scan_engine(unit_dir))
    return defects


def _engine_scripts(unit_dir):
    """[(path, source)] for each ReportLab build script in the unit (read once)."""
    scripts = []
    for p in sorted(unit_dir.rglob("*.py")):
        if not is_text_file(p):
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if "BaseDocTemplate" in src and "PageTemplate" in src:
            scripts.append((p, src))
    return scripts


def _template_ids(src):
    return re.findall(r"PageTemplate\s*\(\s*id\s*=\s*['\"](\w+)['\"]", src)


def _frame_geometry_ok(src):
    """True when some Frame(...) call carries both 72.0 and 540.0 as arguments —
    parsed from the call, not matched anywhere in the file."""
    for args in re.findall(r"Frame\s*\(([^)]*)\)", src):
        nums = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", args)}
        if 72.0 in nums and 540.0 in nums:
            return True
    return False


def scan_engine(unit_dir):
    unit, defects = unit_dir.name, []
    for script, src in _engine_scripts(unit_dir):
        rel = str(script.relative_to(unit_dir))
        ids = _template_ids(src)
        if "First" in ids and "Later" not in ids:
            cls = DETERMINISTIC if len(ids) == 1 else JUDGMENT
            defects.append(defect(unit, "RENDER_MISSING_LATER", cls, "S2", rel,
                                  f"PageTemplate ids {ids}; no Later — page 2+ loses "
                                  "brand treatment and 72.0/540.0 frame"))
    return defects


def scan_library(root, only=None):
    """Tool: enumerate units and build the defect register. `only` limits the
    scan to the named unit folders."""
    root = Path(root)
    if not root.is_dir():
        return result(False, error=f"library root not a directory: {root}")
    units = sorted(d for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith(".")
                   and (only is None or d.name in only))
    if not units:
        return result(False, error=f"no matching skill folders under {root}")
    register = {u.name: scan_unit(u) for u in units}
    return result(True, {"units": [u.name for u in units], "register": register})


# -------------------------------------------------------------------- probes

def _live_render(script_path):
    """Execute the engine's build(path) against a temp target. Returns None on a
    rendered PDF, else a failure string. Only called when ReportLab imports."""
    import importlib.util
    import tempfile
    spec = importlib.util.spec_from_file_location("_neh_probe_engine", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = getattr(module, "build", None)
    if not callable(build):
        return "skipped: no build(path) entry point"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.pdf"
        doc = build(str(out))
        if hasattr(doc, "build") and not out.exists():
            return "skipped: build(path) returned an unbuilt doc"
        data = out.read_bytes() if out.exists() else b""
    if not data.startswith(b"%PDF"):
        return "FAIL: build(path) produced no PDF"
    return None


def render_probe(unit_dir):
    """Tool: render verification for engine units. Static checks always run
    (template inventory + Frame geometry); when ReportLab is importable the
    engine's build(path) is also executed and must emit a PDF — a live failure
    fails the probe. The result records which mode actually ran."""
    scripts = _engine_scripts(unit_dir)
    if not scripts:
        return result(False, error="not an engine unit (no ReportLab build found)")
    try:
        import reportlab  # noqa: F401
        live = True
    except ImportError:
        live = False
    checks, live_failures = [], []
    for script, src in scripts:
        rel = str(script.relative_to(unit_dir))
        ids = _template_ids(src)
        entry = {
            "script": rel,
            "templates": ids,
            "has_first_and_later": "First" in ids and "Later" in ids,
            "geometry_72_540": _frame_geometry_ok(src),
        }
        if live:
            try:
                outcome = _live_render(script)
            except Exception as exc:  # engine raised: probe must fail closed
                outcome = f"FAIL: {type(exc).__name__}: {exc}"
            entry["live_render"] = outcome or "PASS"
            if outcome and outcome.startswith("FAIL"):
                live_failures.append(f"{rel}: {outcome}")
        checks.append(entry)
    ok = (all(c["has_first_and_later"] and c["geometry_72_540"] for c in checks)
          and not live_failures)
    detail = "; ".join(live_failures) if live_failures else \
        "probe FAIL: template inventory or geometry"
    return result(ok, {"mode": "live" if live else "static", "checks": checks},
                  None if ok else detail)


def rescan_probe(unit_dir, patched_codes):
    """Tool: targeted re-scan — the patched defect codes must be absent."""
    remaining = [d for d in scan_unit(unit_dir) if d["code"] in patched_codes]
    return result(not remaining, {"remaining": remaining},
                  None if not remaining else "probe FAIL: defect codes still present")


# ------------------------------------------------------------------- patches

def _reformat_date(raw):
    """Unambiguous non-ISO date -> YYYY-MM-DD, else None."""
    m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", raw)
    if m and int(m.group(1)) > 12:  # day-first is only unambiguous when day > 12
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    for fmt in ("%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _normalize_version(raw):
    v = raw.lstrip("v")
    if re.fullmatch(r"\d+(\.\d+){0,2}", v):
        parts = v.split(".")
        return ".".join(parts + ["0"] * (3 - len(parts)))
    return None


def _bump_patch(version):
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def _balanced_end(src, open_idx):
    """Index just past the (...)/[...] group opening at open_idx, or -1.
    Heuristic: does not account for brackets inside string literals — a
    mis-parse simply fails the patch, which escalates (fail-closed)."""
    pairs = {"(": ")", "[": "]"}
    opener = src[open_idx]
    closer = pairs[opener]
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def patch_clone_later_template(script_path):
    """Clone the First PageTemplate registration as Later (same frames, same
    onPage hook) and register it. Handles multi-line calls via balanced-paren
    parsing. Returns error string or None."""
    src = script_path.read_text(encoding="utf-8")
    for m in re.finditer(r"^([ \t]*)(\w+)\s*=\s*PageTemplate\s*(\()",
                         src, re.MULTILINE):
        end = _balanced_end(src, m.start(3))
        if end < 0:
            continue
        call = src[m.start(3):end]
        if not re.search(r"id\s*=\s*['\"]First['\"]", call):
            continue
        indent, var = m.group(1), m.group(2)
        later_var = var.replace("first", "later").replace("First", "Later")
        if later_var == var:
            later_var = var + "_later"
        later_call = re.sub(r"(id\s*=\s*['\"])First(['\"])", r"\g<1>Later\g<2>",
                            call, count=1)
        src = (src[:end] + f"\n{indent}{later_var} = PageTemplate{later_call}"
               + src[end:])
        reg = re.search(r"addPageTemplates\s*\(\s*(\[)", src)
        if not reg:
            return "addPageTemplates registration not found"
        list_end = _balanced_end(src, reg.start(1))
        if list_end < 0:
            return "addPageTemplates list not parseable"
        src = src[:list_end - 1] + f", {later_var}" + src[list_end - 1:]
        script_path.write_text(src, encoding="utf-8")
        return None
    return "First PageTemplate assignment not found in known pattern"


def apply_patch(unit_dir, spec, run_date):
    """Tool: apply one deterministic patch spec inside the working copy, then the
    bookkeeping (PATCH version bump + changelog entry + provenance re-stamp)."""
    code, file = spec["code"], spec["file"]
    target = unit_dir / file
    changed = []
    if code == "GLYPH_BLACK_SQUARE":
        content = target.read_text(encoding="utf-8")
        target.write_text(content.replace(BLACK_SQUARE, "-"), encoding="utf-8")
        changed.append(file)
    elif code == "FM_NAME_MISMATCH":
        _edit_frontmatter_line(unit_dir / "SKILL.md", "name", unit_dir.name)
        changed.append("SKILL.md")
    elif code == "FM_BAD_VERSION":
        fm, _ = parse_frontmatter((unit_dir / "SKILL.md").read_text(encoding="utf-8"))
        fixed = _normalize_version(fm.get("version", ""))
        if not fixed:
            return result(False, error=f"version not trivially normalizable")
        _edit_frontmatter_line(unit_dir / "SKILL.md", "version", fixed)
        changed.append("SKILL.md")
    elif code == "PROV_BAD_DATE":
        fm, _ = parse_frontmatter((unit_dir / "SKILL.md").read_text(encoding="utf-8"))
        fixed = _reformat_date(fm.get("provenance", {}).get("last_modified_at", ""))
        if not fixed:
            return result(False, error="date ambiguous; escalate")
        _edit_prov_line(unit_dir / "SKILL.md", "last_modified_at", fixed)
        changed.append("SKILL.md")
    elif code == "TREE_STRAY_BRACE_DIR":
        if any(target.iterdir()):
            return result(False, error="stray dir non-empty; escalate")
        target.rmdir()
        changed.append(file)
    elif code == "RENDER_MISSING_LATER":
        err = patch_clone_later_template(target)
        if err:
            return result(False, error=err)
        changed.append(file)
    elif code == "TREE_PDF_PRESENT":
        return result(True, {"files_changed": [], "version": None,
                             "note": "handled at packaging (exclusion), not patched"})
    else:
        return result(False, error=f"no deterministic patch for {code}")

    # Bookkeeping: version bump + changelog + provenance stamp
    skill_md = unit_dir / "SKILL.md"
    fm, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    old_v = fm.get("version", "0.0.0")
    new_v = _bump_patch(old_v) if re.fullmatch(r"\d+\.\d+\.\d+", old_v) else old_v
    if new_v != old_v:
        _edit_frontmatter_line(skill_md, "version", new_v)
    _edit_prov_line(skill_md, "last_modified_on", "CLD")
    _edit_prov_line(skill_md, "last_modified_at", run_date)
    entry = (f"\n### v{new_v} — {run_date} (repair agent)\n"
             f"Mechanical repair: {code} in `{file}`. {spec['detail']}\n")
    text = skill_md.read_text(encoding="utf-8")
    if re.search(r"^##+\s+.*Changelog", text, re.MULTILINE):
        text = re.sub(r"(^##+\s+.*Changelog.*$)", r"\1\n" + entry, text,
                      count=1, flags=re.MULTILINE)
    else:
        text += f"\n## Changelog\n{entry}"
    skill_md.write_text(text, encoding="utf-8")
    if "SKILL.md" not in changed:
        changed.append("SKILL.md")
    return result(True, {"files_changed": changed, "version": new_v})


def _edit_frontmatter_line(skill_md, key, value):
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"\A(---\s*\n.*?\n---\s*\n)", text, re.DOTALL)
    head, tail = m.group(1), text[m.end():]
    head = re.sub(rf"^{key}:\s*.*$", f"{key}: {value}", head, count=1, flags=re.MULTILINE)
    skill_md.write_text(head + tail, encoding="utf-8")


def _edit_prov_line(skill_md, key, value):
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"\A(---\s*\n.*?\n---\s*\n)", text, re.DOTALL)
    head, tail = m.group(1), text[m.end():]
    head = re.sub(rf"^(\s+){key}:\s*.*$", rf"\g<1>{key}: {value}", head,
                  count=1, flags=re.MULTILINE)
    skill_md.write_text(head + tail, encoding="utf-8")


# ------------------------------------------------------------------ package

def package_skill(unit_dir, packages_dir):
    """Tool: fail-closed packaging. PDFs excluded (logged), archive re-opened and
    re-asserted clean; a STOP deletes the emission."""
    fm, err = parse_frontmatter((unit_dir / "SKILL.md").read_text(encoding="utf-8"))
    if fm is None:
        return result(False, error=f"frontmatter unparseable at packaging: {err}")
    name, version = fm.get("name", unit_dir.name), fm.get("version", "0.0.0")
    pkg = packages_dir / f"{name}-v{version}.skill"
    exclusions = []
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(unit_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() == ".pdf":
                exclusions.append(str(p.relative_to(unit_dir)))
                continue
            zf.write(p, f"{name}/{p.relative_to(unit_dir)}")
    # Preflight on re-open
    stops = []
    with zipfile.ZipFile(pkg) as zf:
        if zf.testzip() is not None:
            stops.append("zip integrity")
        names = zf.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if tops != {name}:
            stops.append(f"top-level folders {sorted(tops)} != [{name}]")
        skill_mds = [n for n in names if n.split("/")[-1] == "SKILL.md"]
        if len(skill_mds) != 1 or skill_mds[0] != f"{name}/SKILL.md":
            stops.append(f"SKILL.md count/placement: {skill_mds}")
        if any(n.lower().endswith(".pdf") for n in names):
            stops.append("PDF inside archive")
        elif f"{name}/SKILL.md" in names:
            md = zf.read(f"{name}/SKILL.md").decode("utf-8", errors="replace")
            if (BLACK_SQUARE in md or MOJIBAKE_MARKER in md
                    or any(ch in md for ch in REPLACEMENT_CHARS)):
                stops.append("glyph in packaged SKILL.md")
    if stops:
        pkg.unlink()
        return result(False, error="packaging STOP: " + "; ".join(stops))
    return result(True, {"package": str(pkg), "exclusions": exclusions,
                         "preflight": "PASS"})


# ------------------------------------------------------------------- report

# Fallback copy of templates/repair-report.tmpl.md so the script also runs
# standalone (copied out of the skill folder). The on-disk template wins when
# present; keep the two in sync when editing either.
EMBEDDED_REPORT_TMPL = """\
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
"""


def write_report(run, out_dir):
    """Tool: render the report template and the episodic JSON run log."""
    tmpl_path = Path(__file__).resolve().parent.parent / "templates" / "repair-report.tmpl.md"
    tmpl = (tmpl_path.read_text(encoding="utf-8") if tmpl_path.is_file()
            else EMBEDDED_REPORT_TMPL)

    def rows(items, fmt, empty):
        return "\n".join(fmt(i) for i in items) if items else empty

    register = [d for ds in run["register"].values() for d in ds]
    fields = {
        "run_date": run["run_date"], "root": run["root"], "out": run["out"],
        "mode": run["mode"], "units_scanned": len(run["register"]),
        "summary_line": run["summary"],
        "register_rows": rows(register, lambda d: (
            f"| {d['unit']} | {d['code']} | {d['class']} | {d['severity']} "
            f"| `{d['file']}`: {d['detail']} |"), "| — | clean pass | | | |"),
        "patch_rows": rows(run["patches"], lambda p: (
            f"| {p['patch_id']} | {p['unit']} | {p['code']} | {p['edit']} "
            f"| {p['version']} | {p['probe']} |"), "| — | none | | | | |"),
        "probe_rows": rows(run["probes"], lambda p: (
            f"| {p['unit']} | {p['probe']} | {p['mode']} | {p['result']} "
            f"| {p['detail']} |"), "| — | none | | | |"),
        "package_rows": rows(run["packages"], lambda p: (
            f"| `{p['package']}` | {p['unit']} | {p['version']} | {p['preflight']} "
            f"| {', '.join(p['exclusions']) or '—'} |"), "| — | none | | | |"),
        "escalation_rows": rows(run["escalations"], lambda d: (
            f"| {d['unit']} | {d['code']} | {d['severity']} "
            f"| `{d['file']}`: {d['detail']} | {d['class']}-class per taxonomy |"),
            "| — | none | | | |"),
        "incident_rows": rows(run["incidents"], lambda s: f"- {s}", "None."),
        "saveback_rows": rows(run["saveback"], lambda s: (
            f"| {s['unit']} | {s['before']} | {s['after']} | `{s['package']}` |"),
            "| — | no packages this run | | |"),
        "run_log_path": str(Path(run["out"]) / "run-log.json"),
    }
    report = tmpl
    for k, v in fields.items():
        report = report.replace("{" + k + "}", str(v))
    report_path = out_dir / "repair-report.md"
    report_path.write_text(report, encoding="utf-8")
    log_path = out_dir / "run-log.json"
    log_path.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return result(True, {"report": str(report_path), "run_log": str(log_path)})


# --------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="NEH skill-library repair agent")
    ap.add_argument("--root", required=True, help="library root (opened read-only)")
    ap.add_argument("--out", required=True, help="output dir (working copies, packages, report)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan-only", action="store_true")
    mode.add_argument("--repair", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="scan-only: exit 1 when any defect is found (CI gate)")
    ap.add_argument("--only", metavar="UNIT[,UNIT...]",
                    help="limit the run to the named unit folders")
    args = ap.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None

    run_date = datetime.date.today().isoformat()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run = {"run_date": run_date, "root": str(args.root), "out": str(out_dir),
           "mode": "scan-only" if args.scan_only else "repair",
           "register": {}, "patches": [], "probes": [], "packages": [],
           "escalations": [], "incidents": [], "saveback": [], "summary": ""}

    scan = scan_library(args.root, only=only)
    if not scan["ok"]:
        run["summary"] = f"ABORT: {scan['error']}"
        run["incidents"].append(f"S1 scan_library: {scan['error']}")
        write_report(run, out_dir)
        print(run["summary"], file=sys.stderr)
        return 1
    run["register"] = scan["data"]["register"]
    all_defects = [d for ds in run["register"].values() for d in ds]
    run["escalations"] = [d for d in all_defects if d["class"] == JUDGMENT]

    if args.scan_only:
        n = len(all_defects)
        run["summary"] = ("clean pass — zero defects" if n == 0 else
                          f"{n} defect(s) across "
                          f"{len({d['unit'] for d in all_defects})} unit(s); no patches applied (scan-only)")
        wr = write_report(run, out_dir)
        print(f"{run['summary']}\nreport: {wr['data']['report']}")
        return 1 if args.strict and all_defects else 0

    # Repair mode: work on copies, never the live library.
    work = out_dir / "work"
    pristine = out_dir / "pristine"
    packages = out_dir / "packages"
    for d in (work, pristine, packages):
        d.mkdir(exist_ok=True)

    for unit, defects in run["register"].items():
        det = [d for d in defects if d["class"] == DETERMINISTIC]
        if not det:
            continue
        if any(d["code"] in ("TREE_NO_SKILL_MD", "FM_UNPARSEABLE") for d in defects):
            continue  # precedence rule 2
        src = Path(args.root) / unit
        fm, _ = parse_frontmatter((src / "SKILL.md").read_text(encoding="utf-8"))
        before_v = fm.get("version", "?")
        # A content patch must carry a version bump: never emit a changed
        # package under the installed version string. PDF-only exclusion
        # keeps the version (nothing in the content changes).
        content_codes = [d["code"] for d in det if d["code"] != "TREE_PDF_PRESENT"]
        if content_codes and not (re.fullmatch(r"\d+\.\d+\.\d+", before_v)
                                  or _normalize_version(before_v)):
            run["incidents"].append(
                f"S1 {unit}: version {before_v!r} not bumpable; deterministic "
                f"defects {content_codes} left unpatched")
            continue
        wcopy, pcopy = work / unit, pristine / unit
        for c in (wcopy, pcopy):
            if c.exists():
                shutil.rmtree(c)
            shutil.copytree(src, c)

        patched_codes, failed = [], False
        for seq, d in enumerate(det, 1):  # one repair pass per unit — no retries
            spec = {"code": d["code"], "file": d["file"], "detail": d["detail"]}
            res = apply_patch(wcopy, spec, run_date)
            pid = f"{unit}-{d['code']}-{seq:02d}"
            if not res["ok"]:
                run["incidents"].append(f"S1 apply_patch {pid}: {res['error']}")
                run["escalations"].append({**d, "detail": d["detail"] + f" [patch failed: {res['error']}]"})
                continue
            if res["data"]["files_changed"]:
                patched_codes.append(d["code"])
                run["patches"].append({"patch_id": pid, "unit": unit, "code": d["code"],
                                       "edit": d["file"], "version": res["data"]["version"],
                                       "probe": "pending"})
        pdf_only = any(d["code"] == "TREE_PDF_PRESENT" for d in det)
        if not patched_codes and not pdf_only:
            shutil.rmtree(wcopy)
            continue

        if patched_codes:
            # Reflection gate: one re-verify per unit
            if any(c == "RENDER_MISSING_LATER" for c in patched_codes):
                probe = render_probe(wcopy)
                pname = "render_probe"
            else:
                probe = rescan_probe(wcopy, patched_codes)
                pname = "rescan"
            pmode = (probe["data"] or {}).get("mode", "static")
            presult = "PASS" if probe["ok"] else "FAIL"
            run["probes"].append({"unit": unit, "probe": pname, "mode": pmode,
                                  "result": presult, "detail": probe["error"] or "—"})
            for p in run["patches"]:
                if p["unit"] == unit:
                    p["probe"] = presult
            if not probe["ok"]:
                shutil.rmtree(wcopy)
                shutil.copytree(pcopy, wcopy)  # revert; keep working copy for inspection
                run["incidents"].append(f"S1 probe FAIL on {unit}: reverted, not packaged "
                                        f"({probe['error']})")
                continue

        pkg = package_skill(wcopy, packages)
        if not pkg["ok"]:
            run["incidents"].append(f"S1 package_skill {unit}: {pkg['error']}")
            continue
        fm2, _ = parse_frontmatter((wcopy / "SKILL.md").read_text(encoding="utf-8"))
        after_v = fm2.get("version", "?")
        run["packages"].append({"unit": unit, "package": pkg["data"]["package"],
                                "version": after_v, "preflight": "PASS",
                                "exclusions": pkg["data"]["exclusions"]})
        run["saveback"].append({"unit": unit, "before": before_v, "after": after_v,
                                "package": pkg["data"]["package"]})

    n_pkg, n_esc = len(run["packages"]), len(run["escalations"])
    if not all_defects:
        run["summary"] = "clean pass — zero defects, nothing to repair"
    else:
        run["summary"] = (f"{len(all_defects)} defect(s); {len(run['patches'])} patch(es); "
                          f"{n_pkg} package(s) emitted; {n_esc} escalation(s); "
                          f"{len(run['incidents'])} incident(s)")
    wr = write_report(run, out_dir)
    print(f"{run['summary']}\nreport: {wr['data']['report']}")
    return 0 if not run["incidents"] else 2


if __name__ == "__main__":
    sys.exit(main())
