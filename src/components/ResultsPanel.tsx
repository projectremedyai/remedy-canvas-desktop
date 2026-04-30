import { useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { AcrExportResult, RemediationSummary } from "../types";
import { isMissingInputError, promptForSourceIMSCC } from "../sourceFile";

type Props = {
  summary: RemediationSummary;
  elapsedMs: number;
  onReset: () => void;
};

type AcrFormat = "html" | "markdown" | "json";

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m * 60);
  return `${m}m ${rem}s`;
}

function defaultAcrFileName(courseTitle: string, format: AcrFormat): string {
  const safe = (courseTitle || "course")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "course";
  const ext = format === "markdown" ? "md" : format;
  return `${safe}-acr.${ext}`;
}

function acrExtension(format: AcrFormat): string {
  return format === "markdown" ? "md" : format;
}

export function ResultsPanel({ summary, elapsedMs, onReset }: Props) {
  const sorted = useMemo(() => {
    const rows = [...summary.pages];
    rows.sort((a, b) => {
      const delta = (b.issues_before - b.issues_after) - (a.issues_before - a.issues_after);
      if (delta !== 0) return delta;
      return b.issues_before - a.issues_before;
    });
    return rows;
  }, [summary.pages]);

  const removed = summary.issues_before - summary.issues_after;

  const [acrBusy, setAcrBusy] = useState<AcrFormat | null>(null);
  const [acrError, setAcrError] = useState<string | null>(null);
  const [acrResult, setAcrResult] = useState<AcrExportResult | null>(null);

  const handleExportAcr = async (format: AcrFormat) => {
    setAcrBusy(format);
    setAcrError(null);
    try {
      // Save dialog first — matches the "I clicked Export, ask me
      // where to save" expectation. Only prompt to locate the source
      // IMSCC afterwards, and only if it's actually missing.
      const defaultName = defaultAcrFileName(summary.course_title, format);
      const target = await save({
        title: "Save ACR",
        defaultPath: defaultName,
        filters: [
          {
            name: format.toUpperCase(),
            extensions: [acrExtension(format)],
          },
        ],
      });
      if (!target) {
        setAcrBusy(null);
        return;
      }

      // Try the sidecar call optimistically. Only fall back to the
      // locate-source prompt if the sidecar tells us the input path
      // isn't readable — that's the authoritative answer, not a
      // pre-flight fs check (which can trip over macOS TCC).
      let sourcePath: string = summary.output_path;
      let result: AcrExportResult | null = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          result = await invoke<AcrExportResult>("export_acr", {
            inputPath: sourcePath,
            outputPath: target,
            format,
            courseName: summary.course_title,
            evaluator: "Remedy Canvas Desktop",
          });
          break;
        } catch (err) {
          if (attempt === 0 && isMissingInputError(err)) {
            const picked = await promptForSourceIMSCC(sourcePath);
            if (!picked) {
              setAcrBusy(null);
              return;
            }
            sourcePath = picked;
            continue;
          }
          throw err;
        }
      }
      if (result === null) {
        setAcrBusy(null);
        return;
      }
      setAcrResult(result!);
    } catch (err) {
      setAcrError(err instanceof Error ? err.message : String(err));
    } finally {
      setAcrBusy(null);
    }
  };

  return (
    <section className="results">
      <header className="results__head">
        <h2>
          {removed >= 0 ? removed : 0} issue{removed === 1 ? "" : "s"} removed
          across {summary.pages_modified} page
          {summary.pages_modified === 1 ? "" : "s"}
        </h2>
        <p className="results__course">
          {summary.course_title} · {summary.page_count} page
          {summary.page_count === 1 ? "" : "s"} total
        </p>
      </header>

      <div className="results__actions">
        <button
          type="button"
          onClick={() => {
            void revealItemInDir(summary.output_path);
          }}
        >
          Open output folder
        </button>
        <button type="button" className="btn-secondary" onClick={onReset}>
          Start over
        </button>
      </div>

      <div className="results__acr">
        <div className="results__acr-head">
          <strong>Accessibility Conformance Report</strong>
          <span className="results__acr-hint">
            VPAT 2.5 / WCAG 2.2 AA summary built from the remediated IMSCC.
          </span>
        </div>
        <div className="results__acr-buttons">
          <button
            type="button"
            disabled={acrBusy !== null}
            onClick={() => void handleExportAcr("html")}
          >
            {acrBusy === "html" ? "Generating…" : "Export ACR (HTML)"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={acrBusy !== null}
            onClick={() => void handleExportAcr("markdown")}
          >
            {acrBusy === "markdown" ? "Generating…" : "Markdown"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            disabled={acrBusy !== null}
            onClick={() => void handleExportAcr("json")}
          >
            {acrBusy === "json" ? "Generating…" : "JSON"}
          </button>
        </div>
        {acrResult && (
          <div className="results__acr-result">
            <span>
              <strong>{acrResult.conformance_percentage.toFixed(1)}%</strong>{" "}
              conformance · {acrResult.overall_status} · {acrResult.total_issues}{" "}
              total issues
            </span>
            <button
              type="button"
              className="btn-link"
              onClick={() => void revealItemInDir(acrResult.output_path)}
            >
              Reveal file
            </button>
          </div>
        )}
        {acrError && <div className="results__acr-error">ACR export failed: {acrError}</div>}
      </div>

      <div className="results__tablewrap">
        <table className="results__table">
          <thead>
            <tr>
              <th>Page</th>
              <th>Type</th>
              <th className="num">Before</th>
              <th className="num">After</th>
              <th className="num">Δ</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => {
              const delta = p.issues_before - p.issues_after;
              const skipped = !!p.skipped_reason;
              return (
                <tr key={p.identifier} className={skipped ? "row-skipped" : ""}>
                  <td>
                    <div className="cell-title">{p.title || p.identifier}</div>
                    {skipped && (
                      <div className="cell-skipreason">
                        skipped: {p.skipped_reason}
                      </div>
                    )}
                  </td>
                  <td className="mono">{p.content_type}</td>
                  <td className="num">{p.issues_before}</td>
                  <td className="num">{p.issues_after}</td>
                  <td className={`num ${delta > 0 ? "delta-good" : ""}`}>
                    {delta > 0 ? `−${delta}` : delta < 0 ? `+${-delta}` : "0"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <footer className="results__foot">
        <span>
          <strong>{summary.pages_modified}</strong> modified
        </span>
        {summary.documents_converted > 0 && (
          <span>
            <strong>{summary.documents_converted}</strong> documents converted
            {summary.documents_skipped > 0 && (
              <> ({summary.documents_skipped} skipped)</>
            )}
          </span>
        )}
        <span>Elapsed {formatDuration(elapsedMs)}</span>
      </footer>
    </section>
  );
}
