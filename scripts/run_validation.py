#!/usr/bin/env python3
"""Phase 8 — End-to-end validation sweep across sample IMSCC courses.

Runs the full remediation orchestrator on a set of real Canvas exports
with a deterministic baseline config (body scan + transform + sanitize;
NO AI alt-text, NO render scan, NO document conversion). Captures
per-course deltas plus an aggregate report.

Extras (AI, render, document conversion) are toggled off so the sweep
is reproducible across runs without a live Ollama or a warm Playwright
cache. A follow-up run with the heavier toggles on would test the AI
quality dimensions — out of scope for Phase 8 baseline.

Usage:
    python scripts/run_validation.py [--output docs/phase-8-validation.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

SAMPLES_ROOT = Path.home() / "Desktop" / "sample_export_courses"
OUTPUT_ROOT = Path("/tmp/remedy-canvas-desktop-phase-8")

# Diverse fixture set: minimal → sandbox → real faculty → CVC-OEI canonical
# → richest content-type coverage (assignments + discussions + quizzes).
COURSES: list[tuple[str, str]] = [
    ("digital-literacy", "digital-literacy-2016-export.imscc"),
    ("c-dot-sweeney-sandbox", "c-dot-sweeney-sandbox-export.imscc"),
    ("eng-101-dev-k-dawson", "eng-101-dev-k-dawson-fa18-export.imscc"),
    ("cvc-oei-advanced-techniques", "cvc-oei-advanced-techniques-with-canvas-oms-export.imscc"),
    ("english-102-accessible-template", "english-102-accessible-template-export.imscc"),
]


def _ensure_sidecar_on_path() -> None:
    sidecar = Path(__file__).resolve().parent.parent / "sidecar"
    if not (sidecar / "crd_sidecar").is_dir():
        raise RuntimeError(f"sidecar package not found under {sidecar}")
    sys.path.insert(0, str(sidecar))


async def _run_single(label: str, input_path: Path, output_path: Path) -> dict:
    from crd_sidecar.imscc import parse
    from crd_sidecar.orchestrator import (
        RemediationOptions,
        remediate_course,
    )

    # Baseline: body scan + transform + sanitize. Everything else opt-in off.
    options = RemediationOptions(
        include_rendered_scan=False,
        include_quiz_pages=False,
        sanitize_after_transform=True,
        include_document_conversion=False,
        generate_alt_text=False,
    )

    events: list[dict] = []

    start = time.monotonic()
    summary = await remediate_course(
        input_path,
        output_path,
        job_id=f"val-{label}",
        options=options,
        on_progress=events.append,
    )
    elapsed = time.monotonic() - start

    # Spot-check the output archive is well-formed.
    reparsed = parse(output_path)

    content_type_counts: dict[str, int] = {}
    for p in reparsed.pages:
        content_type_counts[p.content_type.value] = content_type_counts.get(
            p.content_type.value, 0
        ) + 1

    # Top 5 most-improved pages
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
        "top_fixes": [
            {"title": t, "content_type": str(ct), "before": b, "after": a}
            for t, ct, b, a in top_fixes
        ],
        "reparse_ok": reparsed.page_count_matches_summary(summary) if hasattr(
            reparsed, "page_count_matches_summary"
        ) else len(reparsed.pages) >= summary.page_count,
    }


async def main(output_md: Path) -> int:
    _ensure_sidecar_on_path()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for label, filename in COURSES:
        input_path = SAMPLES_ROOT / filename
        if not input_path.is_file():
            print(f"  SKIP {label}: {input_path} not present", file=sys.stderr)
            continue
        output_path = OUTPUT_ROOT / f"{label}-remediated.imscc"
        print(f"  {label}: {input_path.name} ({input_path.stat().st_size // 1024} KB)")
        sys.stdout.flush()
        try:
            result = await _run_single(label, input_path, output_path)
            results.append(result)
            print(
                f"    ✓ {result['page_count']} pages, "
                f"{result['issues_before']} → {result['issues_after']} issues "
                f"({result['reduction_pct']}% reduction), "
                f"{result['elapsed_sec']}s"
            )
        except Exception as exc:
            print(f"    ✗ FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append({"label": label, "error": f"{type(exc).__name__}: {exc}"})

    _write_report(output_md, results)
    _write_json_sidecar(output_md.with_suffix(".json"), results)

    # Aggregate
    completed = [r for r in results if "error" not in r]
    total_before = sum(r["issues_before"] for r in completed)
    total_after = sum(r["issues_after"] for r in completed)
    total_fixed = total_before - total_after
    print()
    print(f"Aggregate: {len(completed)}/{len(results)} courses completed")
    print(f"  issues: {total_before} → {total_after} ({total_fixed} fixed)")
    print(f"  report: {output_md}")

    return 0 if all("error" not in r for r in results) else 1


def _write_report(path: Path, results: list[dict]) -> None:
    lines: list[str] = []
    lines.append("# Phase 8 — Validation Sweep")
    lines.append("")
    lines.append(
        "Baseline end-to-end run of the remediation orchestrator across "
        "a diverse set of real Canvas IMSCC exports. No AI, no render "
        "scan, no document conversion — deterministic body-scan + "
        "transformer + Canvas sanitizer only."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    completed = [r for r in results if "error" not in r]
    if completed:
        total_before = sum(r["issues_before"] for r in completed)
        total_after = sum(r["issues_after"] for r in completed)
        total_pages = sum(r["page_count"] for r in completed)
        total_modified = sum(r["pages_modified"] for r in completed)
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
        lines.append(f"- **Total wall time:** {round(total_elapsed, 1)}s")
    else:
        lines.append("_No courses completed successfully._")
    lines.append("")

    lines.append("| Course | Pages | Before | After | Fixed | % | Time |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        if "error" in r:
            lines.append(
                f"| {r['label']} | ERROR | ERROR | ERROR | ERROR | ERROR | "
                f"_{r['error']}_ |"
            )
            continue
        lines.append(
            f"| {r['label']} | {r['page_count']} | {r['issues_before']} | "
            f"{r['issues_after']} | {r['issues_fixed']} | "
            f"{r['reduction_pct']}% | {r['elapsed_sec']}s |"
        )
    lines.append("")

    for r in results:
        if "error" in r:
            continue
        lines.append(f"## {r['label']}")
        lines.append("")
        lines.append(f"- **Input:** `{Path(r['input_path']).name}` "
                     f"({r['input_size_bytes'] // 1024} KB)")
        lines.append(f"- **Output:** `{Path(r['output_path']).name}` "
                     f"({r['output_size_bytes'] // 1024} KB)")
        lines.append(f"- **Course title:** {r['course_title']}")
        lines.append(f"- **Pages by content type:** "
                     f"{dict(sorted(r['content_type_counts'].items()))}")
        lines.append(
            f"- **Issues:** {r['issues_before']} → {r['issues_after']} "
            f"({r['issues_fixed']} fixed, {r['reduction_pct']}%)"
        )
        lines.append(f"- **Pages modified:** {r['pages_modified']}")
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
        / "phase-8-validation.md",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args.output)))
