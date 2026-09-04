#!/usr/bin/env python3
"""Regression self-test for repair_agent.py — SKILL.md section 10 in executable form.

Builds a fixture library in a temp directory, runs scan-only and repair, and
asserts every QA self-test outcome. Stdlib only; exit 0 = all pass.

    python3 scripts/selftest.py
"""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repair_agent  # noqa: E402

DESC = ("Fixture skill used by the repair-agent regression suite. " * 5).strip()
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


def frontmatter(name, desc=DESC):
    return f"""---
name: {name}
version: 1.0.0
category: fixture
parent_skills:
- ai-agent-patterns
activation_policy: on-demand
provenance:
  last_modified_by: CJV
  last_modified_on: CLD
  last_modified_at: 2026-07-24
description: >-
  {desc}
---

# {name}

Body text.

## Changelog

### v1.0.0
Initial.
"""


def build_fixture_library(root):
    """One unit per SKILL.md section 10 scenario."""
    def unit(folder, name=None, desc=DESC):
        d = root / folder
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(frontmatter(name or folder, desc), encoding="utf-8")
        return d

    # Scenario: JPEG brand asset whose bytes decode to glyph codepoints
    d = unit("good-skill")
    (d / "assets").mkdir()
    (d / "assets" / "brand.jpg").write_bytes(
        b"\xff\xd8\xff\xe0\x00JFIF" + "\u25a0\ufffd".encode() + b"\xff\xd9")

    # Scenario: engine missing the Later PageTemplate. The ReportLab tokens are
    # split in THIS source so the scanner never classifies the selftest itself
    # as an engine — only the written fixture file carries them contiguously.
    bdt, pt = "BaseDoc" + "Template", "Page" + "Template"
    d = unit("pillar-engine")
    (d / "scripts").mkdir()
    (d / "scripts" / "build_doc.py").write_text(
        f"from reportlab.platypus import {bdt}, {pt}, Frame\n\n"
        "def decorate(canvas, doc):\n"
        "    canvas.saveState(); canvas.restoreState()\n\n"
        "def build(path):\n"
        f"    doc = {bdt}(path)\n"
        "    frame = Frame(72.0, 72.0, 468.0, 540.0, id='body')\n"
        f"    first_tmpl = {pt}(id='First', frames=[frame], onPage=decorate)\n"
        f"    doc.add{pt}s([first_tmpl])\n"
        "    return doc\n", encoding="utf-8")

    # Scenario: frontmatter name mismatching folder
    unit("wrong-name", name="old-name")

    # Scenario: description over 1,024 chars
    unit("long-desc", desc=("An exhaustively long description. " * 40).strip())

    # Scenario: skill containing a PDF
    d = unit("pdf-skill")
    (d / "output.pdf").write_bytes(b"%PDF-1.4\n%%EOF")

    # Scenario: U+25A0 glyph corruption in a text artifact
    d = unit("glyph-skill")
    (d / "references").mkdir()
    (d / "references" / "guide.md").write_text(
        "# Guide\n\n\u25a0 item one\n\u25a0 item two\n", encoding="utf-8")


def codes_for(register, unit):
    return sorted(d["code"] for d in register.get(unit, []))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="repair-agent-selftest-"))
    lib = tmp / "lib"
    build_fixture_library(lib)
    clean_lib = tmp / "cleanlib"
    (clean_lib / "only-skill").mkdir(parents=True)
    (clean_lib / "only-skill" / "SKILL.md").write_text(frontmatter("only-skill"), encoding="utf-8")

    print("scan-only mode:")
    rc = repair_agent.main(["--root", str(lib), "--out", str(tmp / "scan"), "--scan-only"])
    check("scan-only exits 0", rc == 0)
    import json
    run = json.loads((tmp / "scan" / "run-log.json").read_text(encoding="utf-8"))
    reg = run["register"]
    check("JPEG glyph bytes -> no glyph defect (binaries never read as text)",
          codes_for(reg, "good-skill") == [], str(codes_for(reg, "good-skill")))
    check("missing Later PageTemplate detected",
          codes_for(reg, "pillar-engine") == ["RENDER_MISSING_LATER"])
    check("name mismatch detected", codes_for(reg, "wrong-name") == ["FM_NAME_MISMATCH"])
    check("overlong description flagged", codes_for(reg, "long-desc") == ["FM_DESC_TOO_LONG"])
    check("overlong description is JUDGMENT class",
          all(d["class"] == "JUDGMENT" for d in reg["long-desc"]))
    check("PDF presence detected", codes_for(reg, "pdf-skill") == ["TREE_PDF_PRESENT"])
    check("glyph corruption detected", codes_for(reg, "glyph-skill") == ["GLYPH_BLACK_SQUARE"])

    print("repair mode:")
    rc = repair_agent.main(["--root", str(lib), "--out", str(tmp / "repair"), "--repair"])
    check("repair exits 0 (no incidents)", rc == 0)
    run = json.loads((tmp / "repair" / "run-log.json").read_text(encoding="utf-8"))
    pkgs = {p["unit"]: p for p in run["packages"]}

    check("engine patched, probed PASS, packaged",
          "pillar-engine" in pkgs and all(
              p["probe"] == "PASS" for p in run["patches"] if p["unit"] == "pillar-engine"))
    patched_engine = (tmp / "repair" / "work" / "pillar-engine" / "scripts" / "build_doc.py"
                      ).read_text(encoding="utf-8")
    check("patched engine has First and Later templates",
          "id='First'" in patched_engine and "id='Later'" in patched_engine)
    check("name mismatch patched and repackaged", "wrong-name" in pkgs)
    check("glyphs replaced; unit repackaged",
          "glyph-skill" in pkgs and "\u25a0" not in (
              tmp / "repair" / "work" / "glyph-skill" / "references" / "guide.md"
          ).read_text(encoding="utf-8"))
    check("PDF unit packaged with exclusion logged",
          pkgs.get("pdf-skill", {}).get("exclusions") == ["output.pdf"])
    with zipfile.ZipFile(pkgs["pdf-skill"]["package"]) as zf:
        check("no PDF inside emitted archive",
              not any(n.lower().endswith(".pdf") for n in zf.namelist()))
    check("source PDF untouched", (lib / "pdf-skill" / "output.pdf").exists())
    check("judgment defect escalated, never patched",
          any(d["unit"] == "long-desc" for d in run["escalations"])
          and "long-desc" not in pkgs
          and not any(p["unit"] == "long-desc" for p in run["patches"]))
    check("live library untouched (name)",
          "name: old-name" in (lib / "wrong-name" / "SKILL.md").read_text(encoding="utf-8"))
    check("live library untouched (glyphs)",
          "\u25a0" in (lib / "glyph-skill" / "references" / "guide.md").read_text(encoding="utf-8"))
    check("every patched unit bumped to 1.0.1",
          all(p["version"] == "1.0.1" for p in run["patches"]))

    print("zero-defect run:")
    rc = repair_agent.main(["--root", str(clean_lib), "--out", str(tmp / "clean"), "--repair"])
    check("clean run exits 0 and still reports",
          rc == 0 and (tmp / "clean" / "repair-report.md").is_file())
    check("clean run summary states a clean pass",
          "clean pass" in json.loads(
              (tmp / "clean" / "run-log.json").read_text(encoding="utf-8"))["summary"])

    report = (tmp / "repair" / "repair-report.md").read_text(encoding="utf-8")
    check("report has no unrendered placeholders", "{" not in report)

    print("self-scan:")
    skill_root = Path(__file__).resolve().parent.parent.parent
    res = repair_agent.scan_library(str(skill_root))
    own = res["data"]["register"].get("neh-skill-repair-agent", [{"code": "SCAN_FAILED"}]
                                     ) if res["ok"] else [{"code": "SCAN_FAILED"}]
    check("this skill scans itself clean", own == [], str([d.get("code") for d in own]))

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed"
          + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
