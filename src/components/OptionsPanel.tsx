import { RemediationOptions } from "../types";

type Props = {
  options: RemediationOptions;
  onChange: (opts: RemediationOptions) => void;
  ollamaReady: boolean;
  disabled?: boolean;
};

type ToggleSpec = {
  key: keyof RemediationOptions;
  label: string;
  hint: string;
  needsOllama?: boolean;
};

const TOGGLES: ToggleSpec[] = [
  {
    key: "sanitize_after_transform",
    label: "Sanitize HTML for Canvas",
    hint: "Strip disallowed tags/attributes so output re-imports cleanly.",
  },
  {
    key: "include_rendered_scan",
    label: "Rendered scan (axe-core)",
    hint: "Also run a headless-browser axe pass. Slower, needs Playwright browsers.",
  },
  {
    key: "include_quiz_pages",
    label: "Include quiz HTML",
    hint: "Scan and transform the HTML inside quiz descriptions/questions.",
  },
  {
    key: "include_document_conversion",
    label: "Convert PDFs / DOCX / PPTX",
    hint: "Convert bundled documents into accessible wiki pages via LiteParse + local LLM.",
    needsOllama: true,
  },
];

export function OptionsPanel({
  options,
  onChange,
  ollamaReady,
  disabled,
}: Props) {
  return (
    <fieldset className="options" disabled={disabled}>
      <legend>Options</legend>
      <div className="options__grid">
        {TOGGLES.map((t) => {
          const gated = t.needsOllama && !ollamaReady;
          const effectiveDisabled = disabled || gated;
          const checked = !!options[t.key] && !gated;
          return (
            <label
              key={t.key}
              className={`option ${effectiveDisabled ? "option--disabled" : ""}`}
              title={gated ? "Ollama is not running — enable it to use this option." : t.hint}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={effectiveDisabled}
                onChange={(e) =>
                  onChange({ ...options, [t.key]: e.target.checked })
                }
              />
              <span className="option__body">
                <span className="option__label">{t.label}</span>
                <span className="option__hint">
                  {gated ? "Needs Ollama — not running." : t.hint}
                </span>
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
