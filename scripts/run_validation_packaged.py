#!/usr/bin/env python3
"""Phase 8.2 — heavy-toggle validation sweep against the PACKAGED sidecar.

Unlike scripts/run_validation_heavy.py (which imports the sidecar Python
in-process from the dev venv), this runner subprocess.Popens the actual
PyInstaller binary inside Remedy Canvas Desktop.app and drives remediate_imscc via
JSON-RPC 2.0 over stdio. The goal is to prove the bundled flow works
end-to-end under real load — bundled chromium-headless-shell, bundled
LiteParse shim, bundled axe.min.js / canvas-mock.css resolved via
CRD_VENDOR_DIR — not just the dev codepath.

Bundled Ollama lifecycle is out of scope here (that's owned by the Rust
shell at Tauri startup); this run points at the user's system Ollama on
127.0.0.1:11434 with qwen3.5:cloud for both text + vision, same as
Phase 8.1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
APP = _REPO_ROOT / "src-tauri/target/release/bundle/macos/Remedy Canvas Desktop.app"
SIDECAR = APP / "Contents/MacOS/crd-sidecar"
VENDOR = APP / "Contents/Resources/vendor"

SAMPLES_ROOT = Path.home() / "Desktop" / "sample_export_courses"
OUTPUT_ROOT = Path("/tmp/remedy-canvas-desktop-phase-8.2")

# Same courses Phase 8.1 ran so deltas are comparable.
COURSES = [
    ("digital-literacy", "digital-literacy-2016-export.imscc"),
    ("cvc-oei-advanced", "cvc-oei-advanced-techniques-with-canvas-oms-export.imscc"),
]

# Per-course wall-clock cap (seconds). 8.1's longest was 697s; 20 min gives
# us slack for Ollama Cloud latency without pinning the test machine forever.
PER_COURSE_BUDGET = 1200


def _tier_num_ctx() -> int:
    """Replicate the Rust sysmem tier table."""
    try:
        bytes_ = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 32768
    gb = (bytes_ + 512 * 1024 * 1024) // (1024 * 1024 * 1024)
    if gb <= 8:
        return 8192
    if gb <= 16:
        return 32768
    if gb <= 32:
        return 65536
    if gb <= 64:
        return 131072
    return 262144


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CRD_VENDOR_DIR"] = str(VENDOR)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(VENDOR / "chromium")
    env["PATH"] = f"{VENDOR / 'liteparse'}:{env.get('PATH', '')}"
    # System Ollama — not bundled for this run (that needs full Rust lifecycle).
    env["CRD_OLLAMA_BASE_URL"] = "http://127.0.0.1:11434/v1"
    env["CRD_OLLAMA_TEXT_MODEL"] = "qwen3.5:cloud"
    env["CRD_OLLAMA_VISION_MODEL"] = "qwen3.5:cloud"
    env["CRD_OLLAMA_NUM_CTX"] = str(_tier_num_ctx())
    return env


def _run_course(label: str, input_path: Path, output_path: Path) -> dict:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "remediate_imscc",
        "params": {
            "input_path": str(input_path),
            "output_path": str(output_path),
            "job_id": f"val-{label}",
            "options": {
                "include_rendered_scan": True,
                "generate_alt_text": True,
                "include_document_conversion": True,
                "sanitize_after_transform": True,
            },
        },
    }

    env = _build_env()
    proc = subprocess.Popen(
        [str(SIDECAR)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None

    start = time.monotonic()
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    notifications = 0
    phases: set[str] = set()
    page_count_seen: int | None = None
    last_phase = ""
    final: dict | None = None
    timed_out = False

    while True:
        if time.monotonic() - start > PER_COURSE_BUDGET:
            timed_out = True
            break
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            final = msg
            break
        # Notification
        notifications += 1
        params = msg.get("params") or {}
        phase = params.get("phase", "")
        if phase:
            phases.add(phase)
            last_phase = phase
        if phase == "parse.done":
            page_count_seen = params.get("page_count")

    if timed_out:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {
            "label": label,
            "error": f"TIMEOUT after {PER_COURSE_BUDGET}s at phase={last_phase}",
            "notifications": notifications,
            "last_phase": last_phase,
            "page_count_seen": page_count_seen,
        }

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    # Capture any stderr the sidecar emitted so skipped-doc diagnostics surface.
    stderr_tail = ""
    if proc.stderr is not None:
        try:
            stderr_tail = (proc.stderr.read() or "")[-4000:]
        except Exception:  # noqa: BLE001
            pass

    elapsed = round(time.monotonic() - start, 2)

    if final is None or "result" not in final:
        err_msg = final.get("error") if final else "no response from sidecar"
        return {
            "label": label,
            "error": f"no result: {err_msg}",
            "notifications": notifications,
            "last_phase": last_phase,
        }

    result = final["result"]
    page_reports = result.get("pages", [])
    render_rule_ids = sorted(
        {
            rid
            for p in page_reports
            for rid in (p.get("rendered_issue_rule_ids") or [])
        }
    )

    return {
        "label": label,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "elapsed_sec": elapsed,
        "course_title": result.get("course_title"),
        "page_count": result.get("page_count"),
        "pages_modified": result.get("pages_modified"),
        "issues_before": result.get("issues_before"),
        "issues_after": result.get("issues_after"),
        "issues_fixed": result.get("issues_before", 0) - result.get("issues_after", 0),
        "reduction_pct": round(
            100
            * (result.get("issues_before", 0) - result.get("issues_after", 0))
            / max(result.get("issues_before", 1), 1),
            1,
        ),
        "documents_converted": result.get("documents_converted", 0),
        "documents_skipped": result.get("documents_skipped", 0),
        "alt_texts_generated": result.get("alt_texts_generated", 0),
        "progress_event_count": notifications,
        "phases_seen": sorted(phases),
        "rendered_rule_ids_seen": render_rule_ids,
        "stderr_tail": stderr_tail,
    }


def _load_phase_8_1() -> dict:
    path = Path("docs/phase-8.1-heavy-validation.json")
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return {r.get("label"): r for r in raw if isinstance(r, dict)}
    return {}


def _write_report(path: Path, results: list[dict]) -> None:
    baseline = _load_phase_8_1()
    lines: list[str] = []
    lines.append("# Phase 8.2 — Packaged-Sidecar Validation Sweep")
    lines.append("")
    lines.append(
        "First run of the heavy-toggle pipeline driving the PyInstaller "
        "binary inside `Remedy Canvas Desktop.app` — Phase 8.1 used the dev venv, "
        "which never exercises `CRD_VENDOR_DIR` or the bundled "
        "`PLAYWRIGHT_BROWSERS_PATH`. Every other parameter matches 8.1 "
        "for apples-to-apples comparability."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(
        "| Course | Pages | Before → After | Δ | Alts | Docs | Rendered rules | Time |"
    )
    lines.append(
        "| --- | ---: | --- | ---: | ---: | ---: | --- | ---: |"
    )
    for r in results:
        if "error" in r:
            lines.append(
                f"| {r['label']} | — | ERROR | — | — | — | — | _{r['error']}_ |"
            )
            continue
        rendered = ", ".join(r["rendered_rule_ids_seen"]) or "—"
        lines.append(
            f"| {r['label']} | {r['page_count']} | "
            f"{r['issues_before']} → {r['issues_after']} | "
            f"{r['reduction_pct']}% | {r['alt_texts_generated']} | "
            f"{r['documents_converted']} | {rendered} | "
            f"{r['elapsed_sec']}s |"
        )
    lines.append("")

    lines.append("## vs Phase 8.1 (dev venv)")
    lines.append("")
    lines.append(
        "| Course | 8.1 Δ% | 8.2 Δ% | 8.1 fixed | 8.2 fixed | 8.1 time | 8.2 time |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        if "error" in r:
            continue
        prev = baseline.get(r["label"], {})
        prev_pct = prev.get("reduction_pct", "n/a")
        prev_fixed = prev.get("issues_fixed", "n/a")
        prev_time = prev.get("elapsed_sec", "n/a")
        lines.append(
            f"| {r['label']} | {prev_pct}% | {r['reduction_pct']}% | "
            f"{prev_fixed} | {r['issues_fixed']} | {prev_time}s | "
            f"{r['elapsed_sec']}s |"
        )
    lines.append("")

    lines.append("## Packaged-flow sanity checks")
    lines.append("")
    for r in results:
        if "error" in r:
            continue
        phases = r["phases_seen"]
        render_ok = any("rendered" in rid.lower() or rid.startswith(("CLR", "CTR"))
                        for rid in r["rendered_rule_ids_seen"])
        lines.append(f"**{r['label']}:**")
        lines.append(f"- Phases observed: {', '.join(phases)}")
        lines.append(
            f"- Rendered scan fired: "
            f"{'yes — ' + ', '.join(r['rendered_rule_ids_seen']) if r['rendered_rule_ids_seen'] else 'NO (regression?)'}"
        )
        lines.append(
            f"- Document conversion: "
            f"{r['documents_converted']} converted / {r['documents_skipped']} skipped"
        )
        lines.append(f"- Alt-text generations: {r['alt_texts_generated']}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if not SIDECAR.is_file():
        print(f"ERROR: packaged sidecar missing at {SIDECAR}", file=sys.stderr)
        return 1
    if not VENDOR.is_dir():
        print(f"ERROR: bundled vendor dir missing at {VENDOR}", file=sys.stderr)
        return 1
    if shutil.which("ollama") is None:
        print("WARNING: `ollama` CLI not on PATH (sidecar uses HTTP, not CLI)")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Runner env: num_ctx={_tier_num_ctx()} vendor={VENDOR}")
    results: list[dict] = []

    for label, filename in COURSES:
        input_path = SAMPLES_ROOT / filename
        if not input_path.is_file():
            print(f"  SKIP {label}: {input_path}", file=sys.stderr)
            continue
        output_path = OUTPUT_ROOT / f"{label}-remediated.imscc"
        size_kb = input_path.stat().st_size // 1024
        print(f"  {label}: {filename} ({size_kb} KB)")
        sys.stdout.flush()
        r = _run_course(label, input_path, output_path)
        results.append(r)
        if "error" in r:
            print(f"    ✗ {r['error']}")
        else:
            print(
                f"    ✓ {r['page_count']} pages, "
                f"{r['issues_before']} → {r['issues_after']} "
                f"({r['reduction_pct']}%), "
                f"docs={r['documents_converted']} alts={r['alt_texts_generated']} "
                f"{r['elapsed_sec']}s"
            )
        sys.stdout.flush()

    report_path = Path("docs/phase-8.2-packaged-validation.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(report_path, results)
    (report_path.with_suffix(".json")).write_text(json.dumps(results, indent=2))
    print(f"\nReport: {report_path}")

    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
