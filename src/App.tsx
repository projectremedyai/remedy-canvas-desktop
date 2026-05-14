import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, UnlistenFn } from "@tauri-apps/api/event";
import { save } from "@tauri-apps/plugin-dialog";
import "./App.css";

import { DropZone } from "./components/DropZone";
import { OptionsPanel } from "./components/OptionsPanel";
import { ProgressView } from "./components/ProgressView";
import { ResultsPanel } from "./components/ResultsPanel";
import { OllamaBadge } from "./components/OllamaBadge";
import { SuggestionReview } from "./components/SuggestionReview";
import { ThemeToggle } from "./components/ThemeToggle";
import {
  BundledOllamaStatus,
  DEFAULT_OPTIONS,
  JobProgress,
  OllamaStatus,
  PullProgressEvent,
  RemediationOptions,
  RemediationSummary,
} from "./types";

type Phase = "idle" | "running" | "done" | "error";

function deriveOutputPath(inputPath: string): {
  dir: string;
  defaultName: string;
} {
  const sepMatch = inputPath.match(/[\\/]/g);
  const sep = sepMatch ? sepMatch[sepMatch.length - 1] : "/";
  const idx = inputPath.lastIndexOf(sep);
  const dir = idx >= 0 ? inputPath.slice(0, idx) : "";
  const name = idx >= 0 ? inputPath.slice(idx + 1) : inputPath;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : ".imscc";
  return { dir, defaultName: `${stem}-remediated${ext}` };
}

function App() {
  const [inputPath, setInputPath] = useState<string | null>(null);
  const [outputPath, setOutputPath] = useState<string | null>(null);
  const [options, setOptions] = useState<RemediationOptions>(DEFAULT_OPTIONS);

  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [pagesTotal, setPagesTotal] = useState<number | null>(null);
  const [summary, setSummary] = useState<RemediationSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState<number>(0);

  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [bundledOllama, setBundledOllama] = useState<BundledOllamaStatus | null>(
    null,
  );
  const [ollamaBusy, setOllamaBusy] = useState(false);
  const [pullInFlight, setPullInFlight] = useState(false);
  const [pullProgress, setPullProgress] = useState<PullProgressEvent | null>(
    null,
  );
  const [pullError, setPullError] = useState<string | null>(null);

  const jobIdRef = useRef<string>("");
  const startRef = useRef<number>(0);

  // --- Ollama status ------------------------------------------------------
  const refreshOllama = useCallback(async () => {
    setOllamaBusy(true);
    try {
      const [sys, bundled] = await Promise.all([
        invoke<OllamaStatus>("check_ollama").catch((e) => ({
          reachable: false,
          base_url: "",
          installed_models: [],
          error: String(e),
        })),
        invoke<BundledOllamaStatus>("bundled_ollama_status").catch(
          () =>
            ({
              running: false,
              bundled: false,
              port: null,
              base_url: null,
              default_model: "gemma4:e4b",
              default_model_size: "9.6 GB",
              default_model_present: false,
              installed_models: [],
              error: "unavailable",
            }) as BundledOllamaStatus,
        ),
      ]);
      setOllama(sys as OllamaStatus);
      setBundledOllama(bundled as BundledOllamaStatus);
    } finally {
      setOllamaBusy(false);
    }
  }, []);

  useEffect(() => {
    void refreshOllama();
    // Poll bundled status for the first ~30s while Ollama warms up.
    const iv = setInterval(() => {
      void refreshOllama();
    }, 3000);
    const stop = setTimeout(() => clearInterval(iv), 30_000);
    return () => {
      clearInterval(iv);
      clearTimeout(stop);
    };
  }, [refreshOllama]);

  // --- Model pull ---------------------------------------------------------
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    (async () => {
      const un = await listen<PullProgressEvent>(
        "ollama.model.pull.progress",
        (event) => {
          setPullProgress(event.payload);
        },
      );
      if (cancelled) un();
      else unlisten = un;
    })();
    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, []);

  const handlePullModel = useCallback(async () => {
    setPullError(null);
    setPullInFlight(true);
    setPullProgress(null);
    try {
      await invoke<string>("pull_default_model", { model: null });
      await refreshOllama();
    } catch (e) {
      setPullError(String(e));
    } finally {
      setPullInFlight(false);
    }
  }, [refreshOllama]);

  // --- Progress listener (registered once) --------------------------------
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    (async () => {
      const un = await listen<JobProgress>("job.progress", (event) => {
        const payload = event.payload;
        if (!payload) return;
        if (payload.job_id && jobIdRef.current && payload.job_id !== jobIdRef.current) {
          return; // stray event from a prior run
        }
        setProgress(payload);
        if (payload.phase === "parse.done" && typeof payload.page_count === "number") {
          setPagesTotal(payload.page_count);
        }
      });
      if (cancelled) un();
      else unlisten = un;
    })();
    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, []);

  // --- Handlers -----------------------------------------------------------
  const handleSelectInput = useCallback((path: string) => {
    setInputPath(path);
    setSummary(null);
    setError(null);
    setProgress(null);
    setPagesTotal(null);
    setPhase("idle");
    const { defaultName } = deriveOutputPath(path);
    // Propose an output path in the same directory; user can override via Save.
    const inputDir = path.replace(/[\\/][^\\/]*$/, "");
    const sep = path.includes("\\") ? "\\" : "/";
    setOutputPath(`${inputDir}${sep}${defaultName}`);
  }, []);

  const handleChooseOutput = useCallback(async () => {
    if (!inputPath) return;
    const { defaultName } = deriveOutputPath(inputPath);
    const chosen = await save({
      title: "Save remediated IMSCC as…",
      defaultPath: outputPath ?? defaultName,
      filters: [{ name: "IMSCC archive", extensions: ["imscc", "zip"] }],
    });
    if (typeof chosen === "string") {
      setOutputPath(chosen);
    }
  }, [inputPath, outputPath]);

  const handleRemediate = useCallback(async () => {
    if (!inputPath || !outputPath) return;
    const jobId = `job-${Date.now()}`;
    jobIdRef.current = jobId;
    startRef.current = performance.now();
    setPhase("running");
    setProgress(null);
    setPagesTotal(null);
    setSummary(null);
    setError(null);
    try {
      const result = await invoke<RemediationSummary>("run_remediation", {
        inputPath,
        outputPath,
        jobId,
        options,
      });
      setSummary(result);
      setElapsedMs(performance.now() - startRef.current);
      setPhase("done");
    } catch (e) {
      setElapsedMs(performance.now() - startRef.current);
      setError(String(e));
      setPhase("error");
    }
  }, [inputPath, outputPath, options]);

  const handleReset = useCallback(() => {
    setInputPath(null);
    setOutputPath(null);
    setSummary(null);
    setProgress(null);
    setPagesTotal(null);
    setError(null);
    setPhase("idle");
    setElapsedMs(0);
  }, []);

  const running = phase === "running";
  const canRemediate = !!inputPath && !!outputPath && !running;

  const showModelPull =
    !!bundledOllama &&
    bundledOllama.running &&
    !bundledOllama.default_model_present;
  const pullPct =
    pullProgress && pullProgress.percent !== null
      ? Math.min(100, Math.max(0, pullProgress.percent))
      : null;

  return (
    <main className="app">
      <header className="app__header">
        <div className="app__brand">Remedy Canvas Desktop</div>
        <div className="app__subtitle">
          Offline IMSCC accessibility remediation
        </div>
        <div className="app__header-right">
          <OllamaBadge
            status={ollama}
            bundled={bundledOllama}
            busy={ollamaBusy}
            onRefresh={() => void refreshOllama()}
          />
          <ThemeToggle />
        </div>
      </header>

      <section className="app__body">
        {phase === "done" && summary ? (
          <>
            <ResultsPanel
              summary={summary}
              elapsedMs={elapsedMs}
              onReset={handleReset}
            />
            <SuggestionReview
              suggestions={summary.suggestions ?? []}
              sourcePath={summary.output_path}
            />
          </>
        ) : (
          <>
            {showModelPull && (
              <div className="banner banner--info" role="status">
                <div className="banner__title">
                  Download AI model ({bundledOllama?.default_model ?? "gemma4:e4b"}, ~{bundledOllama?.default_model_size ?? "9.6 GB"})
                </div>
                <p className="banner__body">
                  Needed for alt-text generation and document conversion. You
                  only need to download this once.
                </p>
                {pullInFlight && pullProgress && (
                  <div className="banner__progress">
                    <div>
                      {pullProgress.status ?? "Pulling…"}
                      {pullPct !== null ? ` · ${pullPct.toFixed(1)}%` : ""}
                    </div>
                    {pullPct !== null && (
                      <div
                        className="progress-bar"
                        role="progressbar"
                        aria-valuenow={Math.round(pullPct)}
                        aria-valuemin={0}
                        aria-valuemax={100}
                      >
                        <div
                          className="progress-bar__fill"
                          style={{ width: `${pullPct}%` }}
                        />
                      </div>
                    )}
                  </div>
                )}
                {pullError && (
                  <pre className="banner__body">{pullError}</pre>
                )}
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => void handlePullModel()}
                  disabled={pullInFlight}
                >
                  {pullInFlight ? "Downloading…" : "Download now"}
                </button>
              </div>
            )}

            <DropZone
              selectedPath={inputPath}
              onSelect={handleSelectInput}
              disabled={running}
            />

            {inputPath && (
              <div className="output-row">
                <label className="output-row__label">Save to</label>
                <div className="output-row__path" title={outputPath ?? ""}>
                  {outputPath ?? "—"}
                </div>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={handleChooseOutput}
                  disabled={running}
                >
                  Change…
                </button>
              </div>
            )}

            <OptionsPanel
              options={options}
              onChange={setOptions}
              ollamaReady={!!ollama?.reachable}
              disabled={running}
            />

            {error && (
              <div className="banner banner--error" role="alert">
                <div className="banner__title">Remediation failed</div>
                <pre className="banner__body">{error}</pre>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    setError(null);
                    setPhase("idle");
                  }}
                >
                  Try again
                </button>
              </div>
            )}

            {running && (
              <ProgressView progress={progress} pagesTotal={pagesTotal} />
            )}

            <div className="app__actions">
              <button
                type="button"
                className="btn-primary"
                disabled={!canRemediate}
                onClick={() => void handleRemediate()}
              >
                {running ? "Remediating…" : "Remediate"}
              </button>
            </div>
          </>
        )}
      </section>
    </main>
  );
}

export default App;
