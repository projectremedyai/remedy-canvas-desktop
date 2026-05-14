mod keychain;
mod ollama;
mod sysmem;

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, Window};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

use ollama::{OllamaHandle, OllamaState};
use sysmem::ContextTier;

/// Resolve the Python binary that runs the sidecar in **dev** builds.
///
/// Defaults to the uv-managed virtualenv at `<repo>/sidecar/.venv/bin/python`.
/// Override via `CRD_SIDECAR_PYTHON` when you need a different interpreter.
/// Release builds use the bundled PyInstaller binary via `AppHandle::shell().sidecar()`.
fn sidecar_python() -> PathBuf {
    if let Ok(override_path) = std::env::var("CRD_SIDECAR_PYTHON") {
        return PathBuf::from(override_path);
    }
    // CARGO_MANIFEST_DIR points at `src-tauri/`; sibling `sidecar/` holds the venv.
    let manifest_dir: PathBuf = env!("CARGO_MANIFEST_DIR").into();
    manifest_dir
        .parent()
        .expect("src-tauri has parent")
        .join("sidecar/.venv/bin/python")
}

/// Build a ready-to-spawn `tauri_plugin_shell::process::Command` for the sidecar.
///
/// * Debug builds use the uv-venv Python + `-m crd_sidecar.main`.
/// * Release builds use the bundled PyInstaller binary registered as an
///   `externalBin` in `tauri.conf.json`.
///
/// Both paths speak the same JSON-RPC 2.0 stdio protocol. Phase 7B also
/// injects `CRD_OLLAMA_BASE_URL` pointing at the bundled Ollama and
/// prepends `vendor/liteparse/` to `PATH` so the Python `liteparse` wrapper's
/// `shutil.which("liteparse")` lookup finds our shim.
fn spawn_sidecar_command(
    app: &AppHandle,
) -> Result<tauri_plugin_shell::process::Command, String> {
    let base_cmd = if cfg!(debug_assertions) {
        let python = sidecar_python();
        app.shell()
            .command(python.to_string_lossy().to_string())
            .args(["-m", "crd_sidecar.main"])
    } else {
        app.shell()
            .sidecar("crd-sidecar")
            .map_err(|e| format!("failed to resolve bundled sidecar: {e}"))?
    };

    let state = app.state::<OllamaState>();
    let mut cmd = base_cmd;

    // Point the Python sidecar at whichever Ollama this app launched with.
    // In dev + missing-bundled fallback, this is the system instance.
    if let Some(base) = ollama::sidecar_base_url(&state) {
        cmd = cmd.env("CRD_OLLAMA_BASE_URL", base);
    }

    // Tell the sidecar which Ollama model to use — picked from RAM at startup.
    let model = ollama::default_local_model();
    cmd = cmd.env("CRD_OLLAMA_TEXT_MODEL", model);
    cmd = cmd.env("CRD_OLLAMA_VISION_MODEL", model);

    // SP-3: Resolve provider configuration. Order of precedence:
    //   1. Saved settings (read_provider_pref) — what the user picked in the UI
    //   2. Environment variables — for dev/test overrides
    // API keys ALWAYS come from Keychain when the provider is cloud; they are
    // never read from the Rust process env (so a `printenv` doesn't leak the key)
    // except as a developer override.
    let saved = read_provider_pref(app);
    let provider = std::env::var("CRD_PROVIDER").unwrap_or(saved.provider.clone());
    cmd = cmd.env("CRD_PROVIDER", &provider);

    if let Ok(model) = std::env::var("CRD_PROVIDER_TEXT_MODEL") {
        cmd = cmd.env("CRD_PROVIDER_TEXT_MODEL", model);
    } else if !saved.text_model.is_empty() {
        cmd = cmd.env("CRD_PROVIDER_TEXT_MODEL", &saved.text_model);
    }

    if let Ok(model) = std::env::var("CRD_PROVIDER_VISION_MODEL") {
        cmd = cmd.env("CRD_PROVIDER_VISION_MODEL", model);
    } else if !saved.vision_model.is_empty() {
        cmd = cmd.env("CRD_PROVIDER_VISION_MODEL", &saved.vision_model);
    }

    if provider != "local-ollama" {
        // For cloud providers, the API key MUST come from Keychain.
        // The env-var path is allowed for dev only.
        let key_from_keychain = keychain::load_api_key(&provider).ok().flatten();
        let key_from_env = std::env::var("CRD_PROVIDER_API_KEY").ok();
        if let Some(key) = key_from_keychain.or(key_from_env) {
            cmd = cmd.env("CRD_PROVIDER_API_KEY", key);
        }
        // If no key is available, the sidecar will get an empty key and the
        // first cloud request will return 401 — surfacing the missing-key
        // state to the UI clearly. We do NOT silently fall back to local-ollama.
    }

    // Prepend the bundled liteparse directory so `shutil.which("liteparse")`
    // in the Python wrapper finds our shim first (release-only).
    if let Some(lp_dir) = ollama::bundled_liteparse_dir(app) {
        let current = std::env::var("PATH").unwrap_or_default();
        let joined = format!("{}:{}", lp_dir.display(), current);
        cmd = cmd.env("PATH", joined);
    }

    // Point Playwright at the bundled chromium-headless-shell install.
    // Without this, launching with channel="chromium-headless-shell" inside
    // the packaged app falls back to ~/Library/Caches/ms-playwright/ on
    // user machines — which may be empty, stale, or absent.
    if let Some(pw_dir) = ollama::bundled_playwright_browsers_dir(app) {
        cmd = cmd.env("PLAYWRIGHT_BROWSERS_PATH", pw_dir);
    }

    // Give the sidecar the bundled vendor/ dir so it can resolve
    // axe.min.js + canvas-mock.css (and any future loose data files).
    // Without this, the PyInstaller onefile extraction layout makes
    // `Path(__file__).parents[3] / "vendor"` point at a temp dir that
    // never contains those files.
    if let Some(vendor_dir) = ollama::bundled_vendor_dir(app) {
        cmd = cmd.env("CRD_VENDOR_DIR", vendor_dir);
    }

    // Size the Ollama KV cache for the host's RAM. The sidecar's
    // OllamaVisionClient picks this up and sets options.num_ctx on every
    // chat completion request — we'd blow up a 16 GB laptop running a
    // 256k-context inference pass otherwise.
    let (tier, _from_env) = sysmem::resolve_context_tier();
    cmd = cmd.env("CRD_OLLAMA_NUM_CTX", tier.num_ctx.to_string());

    // Forward the crash-reporting DSN if the Tauri shell was launched
    // with one. The Python sidecar only initializes Sentry when this
    // is set; unset by default, so nothing phones home.
    if let Ok(dsn) = std::env::var("CRD_SENTRY_DSN") {
        cmd = cmd.env("CRD_SENTRY_DSN", dsn);
    }
    if let Ok(env) = std::env::var("CRD_SENTRY_ENV") {
        cmd = cmd.env("CRD_SENTRY_ENV", env);
    }

    Ok(cmd)
}

/// Expose the detected memory + chosen context tier to the UI so the Ollama
/// badge can surface "32k context (16 GB detected)".
#[tauri::command]
fn context_tier() -> ContextTier {
    let (tier, _from_env) = sysmem::resolve_context_tier();
    tier
}

// --- Provider configuration (SP-3) ----------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderConfig {
    pub provider: String,
    pub text_model: String,
    pub vision_model: String,
    pub has_api_key: bool,
}

const PROVIDER_PREF_FILE: &str = "provider.json";

fn provider_pref_path(app: &tauri::AppHandle) -> std::path::PathBuf {
    app.path()
        .app_config_dir()
        .expect("app_config_dir resolves on macOS")
        .join(PROVIDER_PREF_FILE)
}

fn read_provider_pref(app: &tauri::AppHandle) -> ProviderConfig {
    let path = provider_pref_path(app);
    if let Ok(raw) = std::fs::read_to_string(&path) {
        if let Ok(parsed) = serde_json::from_str::<ProviderConfig>(&raw) {
            return parsed;
        }
    }
    ProviderConfig {
        provider: "local-ollama".into(),
        text_model: "".into(),
        vision_model: "".into(),
        has_api_key: false,
    }
}

fn write_provider_pref(app: &tauri::AppHandle, cfg: &ProviderConfig) -> Result<(), String> {
    let path = provider_pref_path(app);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create config dir: {e}"))?;
    }
    let json = serde_json::to_string_pretty(cfg).map_err(|e| format!("serialize: {e}"))?;
    std::fs::write(&path, json).map_err(|e| format!("write {}: {e}", path.display()))
}

#[tauri::command]
async fn get_provider_config(app: tauri::AppHandle) -> Result<ProviderConfig, String> {
    let mut cfg = read_provider_pref(&app);
    // `has_api_key` is derived from Keychain at read time — file storage only
    // records the provider/model choice, never the key itself.
    if cfg.provider != "local-ollama" {
        cfg.has_api_key = keychain::load_api_key(&cfg.provider)
            .map(|opt| opt.is_some())
            .unwrap_or(false);
    } else {
        cfg.has_api_key = false;
    }
    Ok(cfg)
}

#[tauri::command]
async fn set_provider_config(
    app: tauri::AppHandle,
    provider: String,
    api_key: Option<String>,
    text_model: String,
    vision_model: String,
) -> Result<ProviderConfig, String> {
    if !["local-ollama", "ollama-cloud", "openrouter"].contains(&provider.as_str()) {
        return Err(format!("unknown provider: {provider}"));
    }

    if let Some(key) = api_key.as_deref() {
        if !key.is_empty() && provider != "local-ollama" {
            keychain::store_api_key(&provider, key)?;
        }
    }

    let cfg = ProviderConfig {
        provider: provider.clone(),
        text_model,
        vision_model,
        has_api_key: provider != "local-ollama"
            && keychain::load_api_key(&provider)
                .map(|o| o.is_some())
                .unwrap_or(false),
    };
    write_provider_pref(&app, &cfg)?;
    Ok(cfg)
}

#[tauri::command]
async fn clear_provider_key(provider: String) -> Result<(), String> {
    if provider == "local-ollama" {
        return Ok(());
    }
    keychain::delete_api_key(&provider)
}

#[tauri::command]
async fn test_provider_connection(
    provider: String,
    api_key: Option<String>,
    text_model: String,
) -> Result<String, String> {
    let (base_url, auth) = match provider.as_str() {
        "local-ollama" => ("http://127.0.0.1:11434".to_string(), None),
        "ollama-cloud" => ("https://ollama.com".to_string(), api_key.clone()),
        "openrouter" => ("https://openrouter.ai".to_string(), api_key.clone()),
        other => return Err(format!("unknown provider: {other}")),
    };

    let url = if provider == "ollama-cloud" {
        format!("{base_url}/api/tags")
    } else if provider == "openrouter" {
        format!("{base_url}/api/v1/models")
    } else {
        format!("{base_url}/v1/models")
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| format!("reqwest builder: {e}"))?;
    let mut req = client.get(&url);
    if let Some(key) = auth {
        if !key.is_empty() {
            req = req.header("Authorization", format!("Bearer {key}"));
        }
    }
    let resp = req.send().await.map_err(|e| format!("request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "HTTP {}: {}",
            resp.status(),
            resp.status().canonical_reason().unwrap_or("?")
        ));
    }

    Ok(format!(
        "Connected to {provider} (model: {text_model}). 200 OK from {url}"
    ))
}

#[derive(Debug, Serialize)]
pub struct SidecarPingResult {
    pub ok: bool,
    pub service: String,
    pub raw: String,
}

/// Spawns the sidecar, sends a JSON-RPC ping, reads the pong line back.
/// One-shot subprocess — closes stdin so the sidecar loop exits cleanly.
#[tauri::command]
async fn ping_sidecar(app: AppHandle) -> Result<SidecarPingResult, String> {
    let cmd = spawn_sidecar_command(&app)?;
    let (mut rx, mut child) = cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))?;

    child
        .write(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\n")
        .map_err(|e| format!("write ping failed: {e}"))?;
    // Dropping the stdin handle closes it — the shell plugin exposes this via
    // CommandChild::write above; to close stdin we need to drop the child's
    // stdin. The plugin doesn't give direct access, but killing after we get
    // our response keeps the sidecar from hanging.

    let mut response_line: Option<String> = None;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes).trim().to_string();
                if !line.is_empty() {
                    response_line = Some(line);
                    break;
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }

    // Best-effort: ensure the child exits so we don't leak a process.
    let _ = child.kill();

    let raw = response_line
        .ok_or_else(|| "sidecar produced no output before exit".to_string())?;
    let parsed: serde_json::Value = serde_json::from_str(&raw)
        .map_err(|e| format!("sidecar did not return JSON: {e}\nraw: {raw}"))?;

    let result = parsed
        .get("result")
        .ok_or_else(|| format!("sidecar error: {parsed}"))?;
    let ok = result.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let service = result
        .get("service")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    Ok(SidecarPingResult {
        ok,
        service,
        raw,
    })
}

// --- Ollama health check --------------------------------------------------

#[derive(Debug, Deserialize)]
struct OllamaTag {
    name: String,
}

#[derive(Debug, Deserialize)]
struct OllamaTagsResponse {
    models: Vec<OllamaTag>,
}

#[derive(Debug, Serialize)]
pub struct OllamaStatus {
    pub reachable: bool,
    pub base_url: String,
    pub installed_models: Vec<String>,
    pub error: Option<String>,
}

/// Ask the local Ollama instance for its installed model list.
///
/// Phase 4 convenience for the Tauri shell — lets the UI show "Ollama: ready"
/// / "Ollama: not running" without round-tripping through the Python sidecar.
/// The base URL defaults to http://127.0.0.1:11434 but respects the
/// CRD_OLLAMA_BASE_URL env var (same var the Python sidecar reads,
/// minus the trailing /v1 which is OpenAI-compat only).
#[tauri::command]
async fn check_ollama() -> Result<OllamaStatus, String> {
    let base = resolve_ollama_base();
    let url = format!("{}/api/tags", base.trim_end_matches('/'));

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    let reachable;
    let mut installed_models: Vec<String> = Vec::new();
    let mut error_msg: Option<String> = None;

    match client.get(&url).send().await {
        Ok(resp) => match resp.json::<OllamaTagsResponse>().await {
            Ok(body) => {
                reachable = true;
                installed_models = body.models.into_iter().map(|m| m.name).collect();
            }
            Err(err) => {
                reachable = false;
                error_msg = Some(format!("parse error: {err}"));
            }
        },
        Err(err) => {
            reachable = false;
            error_msg = Some(format!("connect error: {err}"));
        }
    }

    Ok(OllamaStatus {
        reachable,
        base_url: base,
        installed_models,
        error: error_msg,
    })
}

fn resolve_ollama_base() -> String {
    if let Ok(override_url) = std::env::var("CRD_OLLAMA_BASE_URL") {
        // The Python sidecar wants /v1 (OpenAI-compat); the native Ollama
        // /api endpoints live under the root. Strip /v1 if present.
        let base = override_url.trim_end_matches('/');
        return base.strip_suffix("/v1").unwrap_or(base).to_string();
    }
    "http://127.0.0.1:11434".to_string()
}

// --- Remediation streaming ------------------------------------------------

/// Run a remediation job end-to-end: spawn the sidecar, send `remediate_imscc`,
/// stream every `job.progress` notification the sidecar emits over stdout
/// to the frontend via `window.emit("job.progress", ...)`, and return the
/// final RemediationSummary JSON payload.
///
/// Lines without an `id` are treated as progress notifications. The first
/// line with a matching `id` is treated as the terminal response.
#[tauri::command]
async fn run_remediation(
    app: AppHandle,
    window: Window,
    input_path: String,
    output_path: String,
    job_id: String,
    options: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let request_id: i64 = 1;
    let request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "remediate_imscc",
        "params": {
            "input_path": input_path,
            "output_path": output_path,
            "job_id": job_id,
            "options": options,
        },
    });
    let mut request_bytes = serde_json::to_vec(&request)
        .map_err(|e| format!("serialize request: {e}"))?;
    request_bytes.push(b'\n');

    let cmd = spawn_sidecar_command(&app)?;
    let (mut rx, mut child) = cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))?;

    child
        .write(&request_bytes)
        .map_err(|e| format!("write request failed: {e}"))?;

    let mut terminal: Option<serde_json::Value> = None;
    let mut error_payload: Option<String> = None;
    // The shell-plugin Stdout event can coalesce multiple sidecar lines, so
    // buffer until we see newline boundaries.
    let mut buf = String::new();

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                buf.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(idx) = buf.find('\n') {
                    let line = buf[..idx].trim().to_string();
                    buf.drain(..=idx);
                    if line.is_empty() {
                        continue;
                    }
                    let parsed: serde_json::Value = match serde_json::from_str(&line) {
                        Ok(v) => v,
                        Err(_) => continue, // ignore non-JSON log noise
                    };

                    // Notification (no id) → forward to frontend as "job.progress".
                    if parsed.get("id").map(|v| v.is_null()).unwrap_or(true) {
                        if let Some(params) = parsed.get("params") {
                            let _ = window.emit("job.progress", params.clone());
                        }
                        continue;
                    }

                    if parsed.get("id").and_then(|v| v.as_i64()) == Some(request_id) {
                        if let Some(err) = parsed.get("error") {
                            error_payload = Some(format!("sidecar error: {err}"));
                        } else {
                            terminal = parsed.get("result").cloned();
                        }
                        break;
                    }
                }
                if terminal.is_some() || error_payload.is_some() {
                    break;
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }

    // Best-effort reap; the sidecar's main loop exits on EOF, which happens
    // when the child's stdin is closed by kill() or on process drop.
    let _ = child.kill();

    if let Some(err) = error_payload {
        return Err(err);
    }
    terminal.ok_or_else(|| "sidecar closed without a terminal response".to_string())
}

// --- ACR export (one-shot) ------------------------------------------------

/// Generate an Accessibility Conformance Report and write it to disk.
///
/// One-shot: spawns the sidecar, sends a single `export_acr` request, reads
/// the terminal response, and returns the result JSON. The sidecar runs the
/// analyzer synchronously — there's no streaming progress to forward, so
/// this is a plain request/response round-trip.
#[tauri::command]
async fn export_acr(
    app: AppHandle,
    input_path: String,
    output_path: String,
    format: String,
    course_name: Option<String>,
    course_url: Option<String>,
    evaluator: Option<String>,
    include_rendered_scan: Option<bool>,
) -> Result<serde_json::Value, String> {
    let request_id: i64 = 1;
    let mut params = serde_json::Map::new();
    params.insert("input_path".into(), serde_json::Value::String(input_path));
    params.insert("output_path".into(), serde_json::Value::String(output_path));
    params.insert("format".into(), serde_json::Value::String(format));
    if let Some(v) = course_name {
        params.insert("course_name".into(), serde_json::Value::String(v));
    }
    if let Some(v) = course_url {
        params.insert("course_url".into(), serde_json::Value::String(v));
    }
    if let Some(v) = evaluator {
        params.insert("evaluator".into(), serde_json::Value::String(v));
    }
    if let Some(v) = include_rendered_scan {
        params.insert("include_rendered_scan".into(), serde_json::Value::Bool(v));
    }

    let request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "export_acr",
        "params": serde_json::Value::Object(params),
    });
    let mut request_bytes = serde_json::to_vec(&request)
        .map_err(|e| format!("serialize request: {e}"))?;
    request_bytes.push(b'\n');

    let cmd = spawn_sidecar_command(&app)?;
    let (mut rx, mut child) = cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))?;

    child
        .write(&request_bytes)
        .map_err(|e| format!("write request failed: {e}"))?;

    let mut terminal: Option<serde_json::Value> = None;
    let mut error_payload: Option<String> = None;
    let mut buf = String::new();

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                buf.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(idx) = buf.find('\n') {
                    let line = buf[..idx].trim().to_string();
                    buf.drain(..=idx);
                    if line.is_empty() {
                        continue;
                    }
                    let parsed: serde_json::Value = match serde_json::from_str(&line) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };

                    // ACR export doesn't stream progress, but the sidecar
                    // might still emit structured log notifications. Ignore
                    // anything without an id that isn't our response.
                    if parsed.get("id").map(|v| v.is_null()).unwrap_or(true) {
                        continue;
                    }

                    if parsed.get("id").and_then(|v| v.as_i64()) == Some(request_id) {
                        if let Some(err) = parsed.get("error") {
                            error_payload = Some(format!("sidecar error: {err}"));
                        } else {
                            terminal = parsed.get("result").cloned();
                        }
                        break;
                    }
                }
                if terminal.is_some() || error_payload.is_some() {
                    break;
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }

    let _ = child.kill();

    if let Some(err) = error_payload {
        return Err(err);
    }
    terminal.ok_or_else(|| "sidecar closed without a terminal response".to_string())
}

/// Cheap existence check for a cached IMSCC path. The UI calls this before
/// `export_acr` / `apply_suggestions` so a moved or renamed file can be
/// recovered via a file picker instead of crashing the round-trip.
#[tauri::command]
async fn file_exists(path: String) -> bool {
    std::path::Path::new(&path).is_file()
}

/// Apply a list of accepted FixSuggestion objects to a previously-remediated
/// IMSCC. Spawns the sidecar, sends `apply_suggestions`, and returns the
/// terminal RPC result. Shape mirrors `export_acr` — no progress streaming.
#[tauri::command]
async fn apply_suggestions(
    app: AppHandle,
    input_path: String,
    output_path: String,
    suggestions: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let request_id: i64 = 1;
    let request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "apply_suggestions",
        "params": {
            "input_path": input_path,
            "output_path": output_path,
            "suggestions": suggestions,
        },
    });
    let mut request_bytes = serde_json::to_vec(&request)
        .map_err(|e| format!("serialize request: {e}"))?;
    request_bytes.push(b'\n');

    let cmd = spawn_sidecar_command(&app)?;
    let (mut rx, mut child) = cmd.spawn().map_err(|e| format!("spawn sidecar: {e}"))?;

    child
        .write(&request_bytes)
        .map_err(|e| format!("write request failed: {e}"))?;

    let mut terminal: Option<serde_json::Value> = None;
    let mut error_payload: Option<String> = None;
    let mut buf = String::new();

    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                buf.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(idx) = buf.find('\n') {
                    let line = buf[..idx].trim().to_string();
                    buf.drain(..=idx);
                    if line.is_empty() {
                        continue;
                    }
                    let parsed: serde_json::Value = match serde_json::from_str(&line) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
                    if parsed.get("id").map(|v| v.is_null()).unwrap_or(true) {
                        continue;
                    }
                    if parsed.get("id").and_then(|v| v.as_i64()) == Some(request_id) {
                        if let Some(err) = parsed.get("error") {
                            error_payload = Some(format!("sidecar error: {err}"));
                        } else {
                            terminal = parsed.get("result").cloned();
                        }
                        break;
                    }
                }
                if terminal.is_some() || error_payload.is_some() {
                    break;
                }
            }
            CommandEvent::Terminated(_) => break,
            _ => {}
        }
    }

    let _ = child.kill();

    if let Some(err) = error_payload {
        return Err(err);
    }
    terminal.ok_or_else(|| "sidecar closed without a terminal response".to_string())
}

// --- entrypoint -----------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(OllamaState::default())
        .setup(|app| {
            // Launch the bundled Ollama as soon as the app window is up so
            // the user sees "Ollama ready" quickly. Failure here is
            // non-fatal — the UI will show the error and let the user
            // fall back to a system Ollama.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                match ollama::start_bundled_ollama(&handle).await {
                    Ok(h) => {
                        let bundled = h.bundled;
                        let base = h.base_url.clone();
                        let state = handle.state::<OllamaState>();
                        *state.0.lock().unwrap() = Some(h);
                        let _ = handle.emit(
                            "ollama.lifecycle",
                            serde_json::json!({
                                "phase": "ready",
                                "bundled": bundled,
                                "base_url": base,
                            }),
                        );
                    }
                    Err(err) => {
                        // Register an external-handle fallback so the sidecar
                        // still has a base URL to point at (localhost:11434).
                        let state = handle.state::<OllamaState>();
                        *state.0.lock().unwrap() = Some(OllamaHandle::external(
                            "http://127.0.0.1:11434".into(),
                        ));
                        let _ = handle.emit(
                            "ollama.lifecycle",
                            serde_json::json!({
                                "phase": "error",
                                "error": err,
                            }),
                        );
                        eprintln!("[ollama] start_bundled_ollama failed: {err}");
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                // Tear down the child Ollama when the main window closes so
                // we don't orphan a daemon after quit.
                let state = window.app_handle().state::<OllamaState>();
                let _ = state.0.lock().map(|mut g| g.take());
            }
        })
        .invoke_handler(tauri::generate_handler![
            ping_sidecar,
            check_ollama,
            run_remediation,
            export_acr,
            apply_suggestions,
            file_exists,
            context_tier,
            ollama::bundled_ollama_status,
            ollama::pull_default_model,
            get_provider_config,
            set_provider_config,
            clear_provider_key,
            test_provider_connection,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
