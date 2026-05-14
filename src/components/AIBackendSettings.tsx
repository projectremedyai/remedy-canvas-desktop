import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";

import { PROVIDERS, type ProviderConfig, type ProviderId } from "../types/provider";

type TestResult =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "ok"; message: string }
  | { kind: "err"; message: string };

export function AIBackendSettings() {
  const [cfg, setCfg] = useState<ProviderConfig | null>(null);
  const [apiKey, setApiKey] = useState<string>("");
  const [showKey, setShowKey] = useState(false);
  const [testResult, setTestResult] = useState<TestResult>({ kind: "idle" });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    invoke<ProviderConfig>("get_provider_config")
      .then((c) => setCfg(c))
      .catch((e) => setSaveError(`Load failed: ${String(e)}`));
  }, []);

  if (!cfg) {
    return <div className="settings-panel">Loading…</div>;
  }

  const meta = PROVIDERS.find((p) => p.id === cfg.provider) ?? PROVIDERS[0];
  const needsKey = meta.requiresApiKey && !cfg.has_api_key && apiKey.trim() === "";

  const updateProvider = (id: ProviderId) => {
    const next = PROVIDERS.find((p) => p.id === id) ?? PROVIDERS[0];
    setCfg({
      provider: id,
      text_model: next.defaultTextModel,
      vision_model: next.defaultVisionModel,
      has_api_key: false,
    });
    setApiKey("");
    setTestResult({ kind: "idle" });
  };

  const onTest = async () => {
    setTestResult({ kind: "running" });
    try {
      const msg = await invoke<string>("test_provider_connection", {
        provider: cfg.provider,
        apiKey: apiKey || null, // backend reads Keychain if null
        textModel: cfg.text_model || meta.defaultTextModel,
      });
      setTestResult({ kind: "ok", message: msg });
    } catch (e) {
      setTestResult({ kind: "err", message: String(e) });
    }
  };

  const onSave = async () => {
    setSaveError(null);
    setSaving(true);
    try {
      const saved = await invoke<ProviderConfig>("set_provider_config", {
        provider: cfg.provider,
        apiKey: apiKey || null,
        textModel: cfg.text_model,
        visionModel: cfg.vision_model,
      });
      setCfg(saved);
      setApiKey(""); // clear in-memory field after save
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const onClearKey = async () => {
    try {
      await invoke<void>("clear_provider_key", { provider: cfg.provider });
      const refreshed = await invoke<ProviderConfig>("get_provider_config");
      setCfg(refreshed);
      setApiKey("");
      setTestResult({ kind: "idle" });
    } catch (e) {
      setSaveError(`Clear failed: ${String(e)}`);
    }
  };

  return (
    <div className="settings-panel">
      <h2>AI Backend</h2>

      <fieldset>
        <legend>Provider</legend>
        {PROVIDERS.map((p) => (
          <label key={p.id} className="provider-option">
            <input
              type="radio"
              name="provider"
              value={p.id}
              checked={cfg.provider === p.id}
              onChange={() => updateProvider(p.id)}
            />
            <div>
              <strong>{p.label}</strong>
              <p className="provider-description">{p.description}</p>
            </div>
          </label>
        ))}
      </fieldset>

      {meta.requiresApiKey && (
        <fieldset>
          <legend>API key</legend>
          {cfg.has_api_key ? (
            <p>
              ✓ A key is stored in your macOS Keychain for {meta.label}.{" "}
              <button type="button" onClick={onClearKey}>
                Remove key
              </button>
            </p>
          ) : (
            <>
              <div className="api-key-row">
                <input
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={`Paste your ${meta.label} API key`}
                  autoComplete="off"
                  spellCheck={false}
                />
                <button type="button" onClick={() => setShowKey((v) => !v)}>
                  {showKey ? "Hide" : "Show"}
                </button>
              </div>
              {meta.signupUrl && (
                <p className="api-key-help">
                  Don't have one?{" "}
                  <a href={meta.signupUrl} target="_blank" rel="noreferrer">
                    Get an API key from {meta.label}
                  </a>
                </p>
              )}
            </>
          )}
        </fieldset>
      )}

      <fieldset>
        <legend>Models</legend>
        <label>
          Text model:{" "}
          <input
            type="text"
            value={cfg.text_model}
            onChange={(e) =>
              setCfg({ ...cfg, text_model: e.target.value })
            }
            placeholder={meta.defaultTextModel || "auto"}
          />
        </label>
        <label>
          Vision model:{" "}
          <input
            type="text"
            value={cfg.vision_model}
            onChange={(e) =>
              setCfg({ ...cfg, vision_model: e.target.value })
            }
            placeholder={meta.defaultVisionModel || "auto"}
          />
        </label>
      </fieldset>

      <div className="settings-actions">
        <button
          type="button"
          onClick={onTest}
          disabled={needsKey || testResult.kind === "running"}
        >
          {testResult.kind === "running" ? "Testing…" : "Test connection"}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={needsKey || saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>

      {testResult.kind === "ok" && (
        <p className="test-result test-result--ok">✓ {testResult.message}</p>
      )}
      {testResult.kind === "err" && (
        <p className="test-result test-result--err">✗ {testResult.message}</p>
      )}
      {saveError && (
        <p className="test-result test-result--err">Save failed: {saveError}</p>
      )}
    </div>
  );
}
