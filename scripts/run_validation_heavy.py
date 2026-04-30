#!/usr/bin/env python3
"""Phase 8.1 — Heavy-toggle validation sweep.

Re-runs the remediation orchestrator on a focused course set with all
three expensive toggles enabled:

    include_rendered_scan=True           (Playwright + axe-core)
    generate_alt_text=True               (Ollama vision model)
    include_document_conversion=True     (LiteParse CLI)

Baseline Phase 8 (`scripts/run_validation.py`) ran with these all OFF
and produced a body-scan-only issue-reduction number; this sweep shows
the real-world numbers once the heavy paths are live.

Course set is kept tight (digital-literacy + cvc-oei-advanced-techniques)
because each heavy-toggle page can take 5–30s; a full 5-course sweep
would take hours. Per-course hard wall-clock budget is enforced so one
runaway course can't tank the whole run.

Usage:
    python scripts/run_validation_heavy.py \\
        [--output docs/phase-8.1-heavy-validation.md] \\
        [--timeout 900]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

SAMPLES_ROOT = Path.home() / "Desktop" / "sample_export_courses"
OUTPUT_ROOT = Path("/tmp/remedy-canvas-desktop-phase-8.1")

# Baseline JSON used for the delta section of the report.
BASELINE_JSON = (
    Path(__file__).resolve().parent.parent / "docs" / "phase-8-validation.json"
)

# Tight set: the smallest real-faculty course and the CVC-OEI canonical.
# Each page can cost 5–30s with render + alt-text + doc conversion on,
# so we cap the footprint to roughly 40 + 82 ~= 122 pages max.
COURSES: list[tuple[str, str]] = [
    ("digital-literacy", "digital-literacy-2016-export.imscc"),
    (
        "cvc-oei-advanced-techniques",
        "cvc-oei-advanced-techniques-with-canvas-oms-export.imscc",
    ),
]

# qwen3.5:cloud is multimodal (vision + text) and already installed on
# the user's Ollama. See project_ai_model_config.md. The text/vision
# knobs are honored via crd_core.config.get_settings — we bust that
# cache below after exporting the env vars.
TEXT_MODEL = "qwen3.5:cloud"
VISION_MODEL = "qwen3.5:cloud"


def _ensure_sidecar_on_path() -> None:
    sidecar = Path(__file__).resolve().parent.parent / "sidecar"
    if not (sidecar / "crd_sidecar").is_dir():
        raise RuntimeError(f"sidecar package not found under {sidecar}")
    sys.path.insert(0, str(sidecar))


def _apply_heavy_env() -> None:
    """Export the Ollama model env vars and bust the settings cache."""
    os.environ["CRD_OLLAMA_TEXT_MODEL"] = TEXT_MODEL
    os.environ["CRD_OLLAMA_VISION_MODEL"] = VISION_MODEL
    # If another import already cached get_settings() before we set env,
    # clear it so the next lookup reads the fresh values.
    from crd_sidecar.crd_core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    print(
        f"  Ollama @ {settings.ollama_base_url} "
        f"(text={settings.ollama_text_model}, vision={settings.ollama_vision_model})"
    )


async def _run_single(
    label: str,
    input_path: Path,
    output_path: Path,
    per_course_timeout: float,
) -> dict:
    from crd_sidecar.imscc import parse
    from crd_sidecar.orchestrator import (
        RemediationOptions,
        remediate_course,
    )

    options = RemediationOptions(
        include_rendered_scan=True,
        include_quiz_pages=False,  # quiz QTI is parse-only; keep off
        sanitize_after_transform=True,
        include_document_conversion=True,
        generate_alt_text=True,
    )

    events: list[dict] = []
    render_errors = 0
    transform_errors = 0
    last_progress_ts = time.monotonic()

    def _on_progress(ev: dict) -> None:
        nonlocal render_errors, transform_errors, last_progress_ts
        last_progress_ts = time.monotonic()
        events.append(ev)
        phase = ev.get("phase", "")
        # Live stdout — user wants to watch progress.
        if phase == "page.done":
            print(
                f"    · page {ev.get('index')}/{ev.get('total')} "
                f"{ev.get('identifier', '')[:40]} "
                f"[{ev.get('issues_before')}→{ev.get('issues_after')}]"
                f"{' *' if ev.get('modified') else ''}"
            )
        elif phase in {"docs.start", "docs.done", "parse.done", "build.done"}:
            extra = {k: v for k, v in ev.items() if k not in ("job_id", "phase")}
            print(f"    · [{phase}] {extra}")
        elif phase == "page.render_error":
            render_errors += 1
        elif phase == "page.transform_error":
            transform_errors += 1
        elif phase == "page.alt_text.start":
            print(
                f"    · alt-text page {ev.get('index')} "
                f"({ev.get('image_count')} images)"
            )
        sys.stdout.flush()

    start = time.monotonic()
    try:
        summary = await asyncio.wait_for(
            remediate_course(
                input_path,
                output_path,
                job_id=f"val-heavy-{label}",
                options=options,
                on_progress=_on_progress,
            ),
            timeout=per_course_timeout,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        return {
            "label": label,
            "input_path": str(input_path),
            "input_size_bytes": input_path.stat().st_size,
            "error": (
                f"TimeoutError: exceeded {per_course_timeout:.0f}s wall budget "
                f"(last progress {time.monotonic() - last_progress_ts:.0f}s ago)"
            ),
            "elapsed_sec": round(elapsed, 2),
            "progress_event_count": len(events),
            "render_errors": render_errors,
            "transform_errors": transform_errors,
        }
    elapsed = time.monotonic() - start

    reparsed = parse(output_path)

    content_type_counts: dict[str, int] = {}
    for p in reparsed.pages:
        content_type_counts[p.content_type.value] = content_type_counts.get(
            p.content_type.value, 0
        ) + 1

    page_deltas = [
        (p.title, p.content_type, p.issues_before, p.issues_after)
        for p in summary.pages
        if p.issues_before > p.issues_after
    ]
    page_deltas.sort(key=lambda t: t[2] - t[3], reverse=True)
    top_fixes = page_deltas[:5]

    return {
        "label": label,
        "input_path": str(input_path),
        "input_size_bytes": input_path.stat().st_size,
        "output_path": str(output_path),
        "output_size_bytes": output_path.stat().st_size,
        "elapsed_sec": round(elapsed, 2),
        "course_title": summary.course_title,
        "page_count": summary.page_count,
        "pages_modified": summary.pages_modified,
        "issues_before": summary.issues_before,
        "issues_after": summary.issues_after,
        "issues_fixed": summary.issues_before - summary.issues_after,
        "reduction_pct": round(
            100 * (summary.issues_before - summary.issues_after)
            / max(summary.issues_before, 1),
            1,
        ),
        "content_type_counts": content_type_counts,
        "progress_event_count": len(events),
        "alt_texts_generated": summary.alt_texts_generated,
        "documents_converted": summary.documents_converted,
        "documents_skipped": summary.documents_skipped,
        "render_errors": render_errors,
        "transform_errors": transform_errors,
        "top_fixes": [
            {"title": t, "content_type": str(ct), "before": b, "after": a}
            for t, ct, b, a in top_fixes
        ],
        "reparse_ok": len(reparsed.pages) >= summary.page_count,
    }


def _load_baseline() -> dict[str, dict]:
    if not BASELINE_JSON.is_file():
        return {}
    data = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
    return {r["label"]: r for r in data if "error" not in r}


async def main(output_md: Path, per_course_timeout: float) -> int:
    _ensure_sidecar_on_path()
    _apply_heavy_env()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    baseline = _load_baseline()
    if baseline:
        print(
            f"  Baseline loaded from {BASELINE_JSON.name} "
            f"({len(baseline)} courses)"
        )
    else:
        print(f"  ⚠ Baseline {BASELINE_JSON} not found — delta section will be empty")

    results: list[dict] = []
    sweep_start = time.monotonic()
    for label, filename in COURSES:
        input_path = SAMPLES_ROOT / filename
        if not input_path.is_file():
            print(f"  SKIP {label}: {input_path} not present", file=sys.stderr)
            continue
        output_path = OUTPUT_ROOT / f"{label}-remediated.imscc"
        print()
        print(
            f"  {label}: {input_path.name} "
            f"({input_path.stat().st_size // 1024} KB) "
            f"[budget: {per_course_timeout:.0f}s]"
        )
        sys.stdout.flush()
        try:
            result = await _run_single(
                label, input_path, output_path, per_course_timeout
            )
            results.append(result)
            if "error" in result:
                print(f"    ✗ {result['error']} ({result['elapsed_sec']}s)")
            else:
                print(
                    f"    ✓ {result['page_count']} pages, "
                    f"{result['issues_before']} → {result['issues_after']} issues "
                    f"({result['reduction_pct']}% reduction), "
                    f"alt_texts={result['alt_texts_generated']}, "
                    f"docs={result['documents_converted']}, "
                    f"{result['elapsed_sec']}s"
                )
        except Exception as exc:
            print(f"    ✗ FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})

    total_sweep = time.monotonic() - sweep_start

    _write_report(output_md, results, baseline, total_sweep)
    _write_json_sidecar(output_md.with_suffix(".json"), results)

    completed = [r for r in results if "error" not in r]
    total_before = sum(r["issues_before"] for r in completed)
    total_after = sum(r["issues_after"] for r in completed)
    total_fixed = total_before - total_after
    print()
    print(f"Aggregate: {len(completed)}/{len(results)} courses completed")
    print(f"  issues: {total_before} → {total_after} ({total_fixed} fixed)")
    print(f"  total wall time: {round(total_sweep, 1)}s")
    print(f"  report: {output_md}")

    return 0 if all("error" not in r for r in results) else 1


def _write_report(
    path: Path,
    results: list[dict],
    baseline: dict[str, dict],
    total_sweep_sec: float,
) -> None:
    lines: list[str] = []
    lines.append("# Phase 8.1 — Heavy-Toggle Validation Sweep")
    lines.append("")
    lines.append(
        "End-to-end remediation sweep with all three heavy toggles "
        "enabled: `include_rendered_scan`, `generate_alt_text`, and "
        "`include_document_conversion`. This is the real-world conformance "
        "number the Phase 8 baseline (body-scan-only) understated."
    )
    lines.append("")
    lines.append(f"- **Ollama text model:** `{TEXT_MODEL}`")
    lines.append(f"- **Ollama vision model:** `{VISION_MODEL}`")
    lines.append("- **Rendered scan:** Playwright + axe-core")
    lines.append("- **Document conversion:** LiteParse CLI")
    lines.append(f"- **Total wall time:** {round(total_sweep_sec, 1)}s")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    completed = [r for r in results if "error" not in r]
    if completed:
        total_before = sum(r["issues_before"] for r in completed)
        total_after = sum(r["issues_after"] for r in completed)
        total_pages = sum(r["page_count"] for r in completed)
        total_modified = sum(r["pages_modified"] for r in completed)
        total_alts = sum(r["alt_texts_generated"] for r in completed)
        total_docs = sum(r["documents_converted"] for r in completed)
        total_elapsed = sum(r["elapsed_sec"] for r in completed)
        lines.append(
            f"- **Courses remediated:** {len(completed)} of {len(results)}"
        )
        lines.append(f"- **Total pages analyzed:** {total_pages}")
        lines.append(f"- **Pages modified:** {total_modified}")
        lines.append(
            f"- **Issues fixed:** {total_before - total_after} "
            f"({total_before} → {total_after}, "
            f"{round(100 * (total_before - total_after) / max(total_before, 1), 1)}% "
            f"reduction)"
        )
        lines.append(f"- **Alt-texts generated (AI):** {total_alts}")
        lines.append(f"- **Documents converted (LiteParse):** {total_docs}")
        lines.append(f"- **Total remediation time:** {round(total_elapsed, 1)}s")
    else:
        lines.append("_No courses completed successfully._")
    lines.append("")

    lines.append(
        "| Course | Pages | Before | After | Fixed | % | Alt | Docs | Time |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for r in results:
        if "error" in r:
            lines.append(
                f"| {r['label']} | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | ERROR | "
                f"_{r['error']}_ |"
            )
            continue
        lines.append(
            f"| {r['label']} | {r['page_count']} | {r['issues_before']} | "
            f"{r['issues_after']} | {r['issues_fixed']} | "
            f"{r['reduction_pct']}% | {r['alt_texts_generated']} | "
            f"{r['documents_converted']} | {r['elapsed_sec']}s |"
        )
    lines.append("")

    # vs baseline
    if baseline:
        lines.append("## vs. Phase 8 baseline")
        lines.append("")
        lines.append(
            "Baseline = body-scan-only (no render, no AI alt, no doc "
            "conversion). Heavy = this sweep. Higher `issues_before` on "
            "heavy is expected — the rendered scanner surfaces CSS/layout "
            "issues that the body-only scan can't see, and document "
            "conversion adds new HTML pages that enter the scan."
        )
        lines.append("")
        lines.append(
            "| Course | Baseline fixed / before (%) | Heavy fixed / before (%) | Δ reduction | Δ issues fixed |"
        )
        lines.append("| --- | --- | --- | ---: | ---: |")
        for r in results:
            if "error" in r or r["label"] not in baseline:
                continue
            b = baseline[r["label"]]
            b_pct = b.get("reduction_pct", 0.0)
            h_pct = r["reduction_pct"]
            delta_pct = round(h_pct - b_pct, 1)
            delta_fixed = r["issues_fixed"] - b.get("issues_fixed", 0)
            lines.append(
                f"| {r['label']} | "
                f"{b.get('issues_fixed', 0)} / {b.get('issues_before', 0)} ({b_pct}%) | "
                f"{r['issues_fixed']} / {r['issues_before']} ({h_pct}%) | "
                f"{delta_pct:+}% | {delta_fixed:+} |"
            )
        lines.append("")

    for r in results:
        if "error" in r:
            continue
        lines.append(f"## {r['label']}")
        lines.append("")
        lines.append(
            f"- **Input:** `{Path(r['input_path']).name}` "
            f"({r['input_size_bytes'] // 1024} KB)"
        )
        lines.append(
            f"- **Output:** `{Path(r['output_path']).name}` "
            f"({r['output_size_bytes'] // 1024} KB)"
        )
        lines.append(f"- **Course title:** {r['course_title']}")
        lines.append(
            f"- **Pages by content type:** "
            f"{dict(sorted(r['content_type_counts'].items()))}"
        )
        lines.append(
            f"- **Issues:** {r['issues_before']} → {r['issues_after']} "
            f"({r['issues_fixed']} fixed, {r['reduction_pct']}%)"
        )
        lines.append(f"- **Pages modified:** {r['pages_modified']}")
        lines.append(f"- **Alt-texts generated:** {r['alt_texts_generated']}")
        lines.append(
            f"- **Documents converted:** {r['documents_converted']} "
            f"(skipped: {r['documents_skipped']})"
        )
        lines.append(
            f"- **Errors:** render={r['render_errors']} "
            f"transform={r['transform_errors']}"
        )
        lines.append(f"- **Progress events emitted:** {r['progress_event_count']}")
        lines.append(f"- **Elapsed:** {r['elapsed_sec']}s")
        if r["top_fixes"]:
            lines.append("")
            lines.append("**Top 5 fixes (biggest issue-count drops):**")
            lines.append("")
            lines.append("| Page | Type | Before | After | Δ |")
            lines.append("| --- | --- | ---: | ---: | ---: |")
            for fix in r["top_fixes"]:
                lines.append(
                    f"| {fix['title'][:70]} | {fix['content_type']} | "
                    f"{fix['before']} | {fix['after']} | "
                    f"{fix['before'] - fix['after']} |"
                )
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_json_sidecar(path: Path, results: list[dict]) -> None:
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "docs"
        / "phase-8.1-heavy-validation.md",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Hard wall-clock budget per course in seconds (default 900).",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args.output, args.timeout)))
