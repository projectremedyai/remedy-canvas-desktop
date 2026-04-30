import { BundledOllamaStatus, OllamaStatus } from "../types";

type Props = {
  status: OllamaStatus | null;
  bundled: BundledOllamaStatus | null;
  busy: boolean;
  onRefresh: () => void;
};

/**
 * Shows whether the app's Ollama is running and whether the default model
 * has been downloaded yet. The "bundled" status takes precedence over the
 * generic system-health check because that's what actually gets used.
 */
export function OllamaBadge({ status, bundled, busy, onRefresh }: Props) {
  let label = "Checking…";
  let cls = "ollama-badge ollama-badge--unknown";
  let tooltip = "Pinging local Ollama…";

  if (bundled) {
    const runtimeLabel = bundled.bundled ? "bundled" : "system";
    if (bundled.running) {
      if (bundled.default_model_present) {
        label = `Ollama · ${runtimeLabel} · ${bundled.default_model} ready`;
        cls = "ollama-badge ollama-badge--ok";
      } else {
        label = `Ollama · ${runtimeLabel} · model missing`;
        cls = "ollama-badge ollama-badge--warn";
      }
      tooltip = `${bundled.base_url ?? ""}${
        bundled.installed_models.length
          ? ` · ${bundled.installed_models.length} model(s)`
          : ""
      }`;
    } else {
      label = "Ollama starting…";
      cls = "ollama-badge ollama-badge--warn";
      tooltip = bundled.error ?? "Waiting for Ollama to come up";
    }
  } else if (status) {
    // Fallback to the legacy /api/tags check while bundled status is loading.
    if (status.reachable) {
      label = "Ollama ready";
      cls = "ollama-badge ollama-badge--ok";
    } else {
      label = "Ollama not running";
      cls = "ollama-badge ollama-badge--warn";
    }
    tooltip = `${status.base_url}${
      status.installed_models.length
        ? ` · ${status.installed_models.length} model(s)`
        : ""
    }`;
  }

  return (
    <button
      type="button"
      className={cls}
      onClick={onRefresh}
      disabled={busy}
      title={tooltip}
      aria-label="Refresh Ollama status"
    >
      <span className="ollama-badge__dot" aria-hidden="true" />
      <span className="ollama-badge__label">
        {busy ? "Checking…" : label}
      </span>
      <span className="ollama-badge__refresh" aria-hidden="true">
        ↻
      </span>
    </button>
  );
}
