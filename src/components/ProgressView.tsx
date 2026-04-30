import { JobProgress } from "../types";

type Props = {
  progress: JobProgress | null;
  pagesTotal: number | null;
};

function describe(p: JobProgress): string {
  switch (p.phase) {
    case "parse.start":
      return "Parsing IMSCC archive…";
    case "parse.done":
      return `Parsed "${p.course_title ?? "course"}" (${p.page_count ?? "?"} pages)`;
    case "docs.start":
      return "Converting bundled documents…";
    case "docs.progress":
      return `Converting document ${p.index ?? "?"}/${p.total ?? "?"}…`;
    case "docs.done":
      return `Converted ${p.converted ?? 0} document(s); skipped ${p.skipped ?? 0}`;
    case "page.start":
      return `Analyzing page ${p.index ?? "?"} of ${p.total ?? "?"}: ${p.title ?? p.identifier ?? ""}`;
    case "page.transform":
      return `Transforming page ${p.index ?? "?"} of ${p.total ?? "?"}…`;
    case "page.done":
      return `Finished page ${p.index ?? "?"} of ${p.total ?? "?"}`;
    case "page.render_error":
      return `Rendered-scan warning on page ${p.index ?? "?"}: ${p.error ?? "error"}`;
    case "write.start":
      return "Writing output archive…";
    case "write.done":
      return "Output archive written.";
    default:
      return p.phase;
  }
}

export function ProgressView({ progress, pagesTotal }: Props) {
  if (!progress) {
    return (
      <div className="progress" role="status" aria-live="polite">
        <div className="progress__label">Starting…</div>
        <div
          className="progress__bar progress__bar--indeterminate"
          role="progressbar"
          aria-label="Remediation progress"
          aria-valuetext="Starting"
        />
      </div>
    );
  }

  const total = pagesTotal ?? progress.total ?? null;
  const index = progress.index ?? null;
  const percent =
    total && index && total > 0
      ? Math.min(100, Math.round((index / total) * 100))
      : null;

  const description = describe(progress);

  return (
    <div className="progress" role="status" aria-live="polite">
      <div className="progress__label">{description}</div>
      {percent !== null ? (
        <>
          <div
            className="progress__bar"
            role="progressbar"
            aria-label="Remediation progress"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={`${description} — page ${index} of ${total}, ${percent}%`}
          >
            <div
              className="progress__fill"
              style={{ width: `${percent}%` }}
            />
          </div>
          <div className="progress__count">
            {index} / {total} ({percent}%)
          </div>
        </>
      ) : (
        <div
          className="progress__bar progress__bar--indeterminate"
          role="progressbar"
          aria-label="Remediation progress"
          aria-valuetext={description}
        />
      )}
    </div>
  );
}
