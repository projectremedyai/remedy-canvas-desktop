import { useCallback, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { FixSuggestion } from "../types";
import { isMissingInputError, promptForSourceIMSCC } from "../sourceFile";

type ApplyResult = {
  input_path: string;
  output_path: string;
  applied_count: number;
  not_applied_count: number;
  pages_modified: number;
};

type Props = {
  suggestions: FixSuggestion[];
  /** Path to the IMSCC the suggestions were generated against. */
  sourcePath: string;
};

function confidenceBand(c: number): "high" | "med" | "low" {
  if (c >= 0.7) return "high";
  if (c >= 0.4) return "med";
  return "low";
}

function pageLabel(s: FixSuggestion): string {
  return s.page_title || s.page_id || "untitled page";
}

function proposeOutputPath(sourcePath: string): string {
  const lastSlash = Math.max(
    sourcePath.lastIndexOf("/"),
    sourcePath.lastIndexOf("\\"),
  );
  const dir = lastSlash >= 0 ? sourcePath.slice(0, lastSlash + 1) : "";
  const name = lastSlash >= 0 ? sourcePath.slice(lastSlash + 1) : sourcePath;
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return `${dir}${name}-reviewed.imscc`;
  return `${dir}${name.slice(0, dot)}-reviewed${name.slice(dot)}`;
}

export function SuggestionReview({ suggestions, sourcePath }: Props) {
  const [accepted, setAccepted] = useState<Set<string>>(() => new Set());
  const [expanded, setExpanded] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  const groups = useMemo(() => {
    const byRule = new Map<string, FixSuggestion[]>();
    for (const s of suggestions) {
      const list = byRule.get(s.rule_id) ?? [];
      list.push(s);
      byRule.set(s.rule_id, list);
    }
    return Array.from(byRule.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [suggestions]);

  const allSelected = accepted.size === suggestions.length;
  const noneSelected = accepted.size === 0;

  const toggleOne = (id: string) => {
    setAccepted((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (allSelected) setAccepted(new Set());
    else setAccepted(new Set(suggestions.map((s) => s.id)));
  };

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => (prev === id ? null : id));
  };

  const applySelected = useCallback(async () => {
    if (accepted.size === 0 || applying) return;
    setApplyError(null);
    setApplyResult(null);

    // Save dialog first — user clicked Apply, expects to pick a
    // destination. Source-locate only fires if the cached path is gone.
    const chosen = suggestions.filter((s) => accepted.has(s.id));
    const suggested = proposeOutputPath(sourcePath);
    let outputPath: string | null;
    try {
      outputPath = await save({
        defaultPath: suggested,
        filters: [{ name: "IMSCC", extensions: ["imscc"] }],
      });
    } catch (err) {
      setApplyError(String(err));
      return;
    }
    if (!outputPath) return;

    setApplying(true);
    try {
      let resolvedSource: string = sourcePath;
      let result: ApplyResult | null = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          result = await invoke<ApplyResult>("apply_suggestions", {
            inputPath: resolvedSource,
            outputPath,
            suggestions: chosen,
          });
          break;
        } catch (err) {
          if (attempt === 0 && isMissingInputError(err)) {
            const picked = await promptForSourceIMSCC(resolvedSource);
            if (!picked) return;
            resolvedSource = picked;
            continue;
          }
          throw err;
        }
      }
      if (result !== null) setApplyResult(result);
    } catch (err) {
      setApplyError(String(err));
    } finally {
      setApplying(false);
    }
  }, [accepted, applying, sourcePath, suggestions]);

  if (suggestions.length === 0) return null;

  return (
    <section className="suggestions">
      <header className="suggestions__head">
        <div>
          <h3>Review {suggestions.length} AI suggestion{suggestions.length === 1 ? "" : "s"}</h3>
          <p className="suggestions__hint">
            Residual issues the automated pass couldn&apos;t safely fix. Accept
            the ones that look right — you stay in the loop so a confident-wrong
            AI fix never ships unchecked.
          </p>
        </div>
        <div className="suggestions__actions">
          <label className="suggestions__selectall">
            <input
              type="checkbox"
              checked={allSelected}
              ref={(el) => {
                if (el) el.indeterminate = !allSelected && !noneSelected;
              }}
              onChange={toggleAll}
            />
            Select all
          </label>
          <button
            type="button"
            className="btn-primary"
            disabled={accepted.size === 0 || applying}
            onClick={() => void applySelected()}
          >
            {applying
              ? "Applying…"
              : `Apply ${accepted.size} selected`}
          </button>
        </div>
      </header>

      {applyResult && (
        <div className="suggestions__result" role="status">
          <div>
            Applied <strong>{applyResult.applied_count}</strong> suggestion
            {applyResult.applied_count === 1 ? "" : "s"} across{" "}
            {applyResult.pages_modified} page
            {applyResult.pages_modified === 1 ? "" : "s"}.
            {applyResult.not_applied_count > 0 && (
              <>
                {" "}
                <span className="suggestions__result-warn">
                  {applyResult.not_applied_count} couldn&apos;t be located —
                  page may have changed.
                </span>
              </>
            )}
          </div>
          <button
            type="button"
            className="btn-link"
            onClick={() => void revealItemInDir(applyResult.output_path)}
          >
            Reveal file
          </button>
        </div>
      )}

      {applyError && (
        <div className="suggestions__error" role="alert">
          {applyError}
        </div>
      )}

      {groups.map(([ruleId, list]) => (
        <div key={ruleId} className="suggestions__group">
          <div className="suggestions__group-label">
            {ruleId} · {list.length} suggestion{list.length === 1 ? "" : "s"}
          </div>
          <ul className="suggestions__list">
            {list.map((s) => {
              const isExpanded = expanded === s.id;
              const isAccepted = accepted.has(s.id);
              const band = confidenceBand(s.confidence);
              return (
                <li
                  key={s.id}
                  className={`suggestion ${isExpanded ? "suggestion--expanded" : ""} ${
                    isAccepted ? "suggestion--accepted" : ""
                  }`}
                >
                  <div className="suggestion__row">
                    <input
                      type="checkbox"
                      className="suggestion__check"
                      checked={isAccepted}
                      onChange={() => toggleOne(s.id)}
                      aria-label={`Accept suggestion for ${pageLabel(s)}`}
                    />
                    <button
                      type="button"
                      className="suggestion__body"
                      onClick={() => toggleExpanded(s.id)}
                      aria-expanded={isExpanded}
                    >
                      <div className="suggestion__line">
                        <span className="suggestion__page">{pageLabel(s)}</span>
                        <span className={`suggestion__confidence suggestion__confidence--${band}`}>
                          {Math.round(s.confidence * 100)}%
                        </span>
                      </div>
                      <div className="suggestion__diff">
                        <span
                          className={`suggestion__from${
                            s.original_text ? "" : " suggestion__from--empty"
                          }`}
                        >
                          {s.original_text || "(no alt)"}
                        </span>
                        <span className="suggestion__arrow">→</span>
                        <span className="suggestion__to">{s.proposed_text}</span>
                      </div>
                    </button>
                  </div>

                  {isExpanded && (
                    <div className="suggestion__detail">
                      {s.rationale && (
                        <p className="suggestion__rationale">
                          <span className="suggestion__detail-label">Why:</span> {s.rationale}
                        </p>
                      )}
                      <div className="suggestion__htmls">
                        <div>
                          <span className="suggestion__detail-label">Before</span>
                          <pre className="suggestion__html">{s.original_html}</pre>
                        </div>
                        <div>
                          <span className="suggestion__detail-label">After</span>
                          <pre className="suggestion__html">{s.proposed_html}</pre>
                        </div>
                      </div>
                      {s.page_file_path && (
                        <div className="suggestion__path" title={s.page_file_path}>
                          {s.page_file_path}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </section>
  );
}
