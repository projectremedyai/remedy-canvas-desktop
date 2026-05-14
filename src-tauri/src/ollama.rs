//! Bundled-Ollama lifecycle.
//!
//! Phase 7B adds the Ollama CLI as a Tauri `bundle.resource`, so the shipped
//! `.app` contains everything needed to run inference without an external
//! install. This module is responsible for:
//!
//! - Locating the bundled binary inside `Contents/Resources/ollama/ollama`
//! - Spawning it on a free localhost port with `OLLAMA_HOST=127.0.0.1:<port>`
//! - Health-checking `/api/tags` until it's live (30-second budget)
//! - Exposing the port via Tauri managed state so other commands (and the
//!   sidecar spawner) can wire `CRD_OLLAMA_BASE_URL` correctly.
//! - Pulling `gemma4:e4b` on demand, streaming progress over a Tauri event.
//! - Killing the child when the app exits.
//!
//! In dev (`cfg!(debug_assertions)`) we do nothing — the user's system Ollama
//! on :11434 keeps working. Same pattern the sidecar uses.

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tokio::process::{Child, Command};

/// Everything we need to know about a live bundled Ollama.
pub struct OllamaHandle {
    pub port: u16,
    pub base_url: String,
    pub bundled: bool,
    child: Option<Child>,
}

impl OllamaHandle {
    pub fn external(base_url: String) -> Self {
        Self {
            port: 0,
            base_url,
            bundled: false,
            child: None,
        }
    }
}

impl Drop for OllamaHandle {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            // Start-kill is intentional; we don't await in Drop.
            let _ = child.start_kill();
        }
    }
}

/// Tauri-managed state that stores the (optional) handle to the spawned Ollama.
pub struct OllamaState(pub Mutex<Option<OllamaHandle>>);

impl Default for OllamaState {
    fn default() -> Self {
        Self(Mutex::new(None))
    }
}

#[derive(Debug, Serialize, Clone)]
pub struct BundledOllamaStatus {
    pub running: bool,
    pub bundled: bool,
    pub port: Option<u16>,
    pub base_url: Option<String>,
    pub default_model: String,
    pub default_model_size: String,
    pub default_model_present: bool,
    pub installed_models: Vec<String>,
    pub error: Option<String>,
}

/// The Ollama tag for the bundled-Ollama default model, picked at runtime
/// from installed RAM. 8 GB systems get `gemma4:e2b`; 16+ GB get `gemma4:e4b`.
pub fn default_local_model() -> &'static str {
    crate::sysmem::ModelSize::for_ram(crate::sysmem::detect_total_memory_gb())
        .ollama_tag()
}

/// Human-readable approximate download size of `default_local_model()`,
/// used in UI copy ("Download AI model (gemma4:e2b, ~7.2 GB)").
pub fn default_local_model_size() -> &'static str {
    crate::sysmem::ModelSize::for_ram(crate::sysmem::detect_total_memory_gb())
        .approx_download_gb()
}

/// Finds the bundled Ollama executable. Returns `None` in dev builds or when
/// the resource isn't present (e.g. the user ran `cargo run` without a
/// `pnpm tauri build`).
fn locate_bundled_binary(app: &AppHandle) -> Option<PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    // Tauri flattens `bundle.resources` globs under `_up_/` in the bundled
    // layout. Try the two likely locations; whichever exists wins.
    let candidates = [
        resource_dir.join("_up_/vendor/ollama/ollama"),
        resource_dir.join("vendor/ollama/ollama"),
        resource_dir.join("ollama/ollama"),
    ];
    candidates.into_iter().find(|p| p.is_file())
}

fn pick_free_port() -> Result<u16, String> {
    // TcpListener with port 0 = OS assigns a free port; drop releases it.
    let listener = std::net::TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("bind failed: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("local_addr failed: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

async fn wait_for_ready(base_url: &str, budget: Duration) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| e.to_string())?;
    let tags_url = format!("{}/api/tags", base_url.trim_end_matches('/'));
    let deadline = Instant::now() + budget;
    let mut last_err: Option<String> = None;
    while Instant::now() < deadline {
        match client.get(&tags_url).send().await {
            Ok(resp) if resp.status().is_success() => return Ok(()),
            Ok(resp) => last_err = Some(format!("HTTP {}", resp.status())),
            Err(e) => last_err = Some(e.to_string()),
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    Err(format!(
        "ollama did not become ready within {:?}: {}",
        budget,
        last_err.unwrap_or_else(|| "no response".into())
    ))
}

/// Launch the bundled Ollama on a free localhost port. In dev, skip the spawn
/// and return a handle pointing at the user's system Ollama on :11434.
pub async fn start_bundled_ollama(app: &AppHandle) -> Result<OllamaHandle, String> {
    // SP-2: if the user has selected a cloud provider, the bundled local
    // Ollama is unnecessary — return an "external" handle pointing nowhere
    // and let the sidecar talk to the cloud directly.
    let provider = std::env::var("CRD_PROVIDER").unwrap_or_default();
    let provider_norm = provider.trim().to_lowercase();
    if provider_norm == "ollama-cloud" || provider_norm == "openrouter" {
        return Ok(OllamaHandle::external(
            // base URL is a no-op for the Rust side — the sidecar reads its own
            // CRD_PROVIDER_API_KEY and routes directly. We use a sentinel here.
            "cloud-provider".into(),
        ));
    }
    if cfg!(debug_assertions) {
        let base = std::env::var("CRD_OLLAMA_BASE_URL")
            .ok()
            .map(|s| {
                let trimmed = s.trim_end_matches('/').to_string();
                trimmed
                    .strip_suffix("/v1")
                    .map(|s| s.to_string())
                    .unwrap_or(trimmed)
            })
            .unwrap_or_else(|| "http://127.0.0.1:11434".into());
        return Ok(OllamaHandle::external(base));
    }

    let binary = match locate_bundled_binary(app) {
        Some(p) => p,
        None => {
            // Release build but no bundled binary — fall back to system Ollama
            // so the app doesn't brick itself on a bad install.
            return Ok(OllamaHandle::external("http://127.0.0.1:11434".into()));
        }
    };

    let port = pick_free_port()?;
    let base_url = format!("http://127.0.0.1:{port}");

    // Store models inside the per-user app-data dir so they survive restarts
    // and reside outside the bundle (which is read-only on macOS).
    let models_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))?
        .join("ollama-models");
    if !models_dir.exists() {
        std::fs::create_dir_all(&models_dir)
            .map_err(|e| format!("create models dir: {e}"))?;
    }

    let mut cmd = Command::new(&binary);
    cmd.arg("serve")
        .env("OLLAMA_HOST", format!("127.0.0.1:{port}"))
        .env("OLLAMA_MODELS", &models_dir)
        // Parent-level ignore of stdin/out/err so Ollama can chatter without
        // holding our pipes open; this also avoids eventual ENOBUFS.
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);

    let child = cmd
        .spawn()
        .map_err(|e| format!("spawn bundled ollama: {e}"))?;

    // Give the daemon up to 30s to be reachable; kill it if it isn't.
    let ready_timeout = Duration::from_secs(30);
    match wait_for_ready(&base_url, ready_timeout).await {
        Ok(()) => Ok(OllamaHandle {
            port,
            base_url,
            bundled: true,
            child: Some(child),
        }),
        Err(err) => {
            let mut child = child;
            let _ = child.start_kill();
            Err(err)
        }
    }
}

#[derive(Debug, Deserialize)]
struct TagModel {
    name: String,
}

#[derive(Debug, Deserialize)]
struct TagsResponse {
    models: Vec<TagModel>,
}

async fn fetch_tags(base_url: &str) -> Result<Vec<String>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;
    let url = format!("{}/api/tags", base_url.trim_end_matches('/'));
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("connect: {e}"))?;
    let body: TagsResponse = resp
        .json()
        .await
        .map_err(|e| format!("parse tags: {e}"))?;
    Ok(body.models.into_iter().map(|m| m.name).collect())
}

fn model_present(models: &[String], target: &str) -> bool {
    // Ollama's /api/tags returns names like "gemma4:e4b"; accept the exact
    // tag or any name that shares the stem (`gemma4:e4b-foo`).
    let stem = target.split(':').next().unwrap_or(target);
    let target_tag = target.to_string();
    models.iter().any(|m| {
        m == &target_tag
            || m == stem
            || m.split(':').next().unwrap_or("") == stem
    })
}

#[tauri::command]
pub async fn bundled_ollama_status(
    state: tauri::State<'_, OllamaState>,
) -> Result<BundledOllamaStatus, String> {
    let (base_url, bundled, port) = {
        let guard = state.0.lock().unwrap();
        match guard.as_ref() {
            Some(h) => (
                Some(h.base_url.clone()),
                h.bundled,
                if h.bundled { Some(h.port) } else { None },
            ),
            None => (None, false, None),
        }
    };

    let Some(base) = base_url else {
        return Ok(BundledOllamaStatus {
            running: false,
            bundled: false,
            port: None,
            base_url: None,
            default_model: default_local_model().to_string(),
            default_model_size: default_local_model_size().to_string(),
            default_model_present: false,
            installed_models: vec![],
            error: Some("ollama not initialized".into()),
        });
    };

    match fetch_tags(&base).await {
        Ok(models) => {
            let present = model_present(&models, default_local_model());
            Ok(BundledOllamaStatus {
                running: true,
                bundled,
                port,
                base_url: Some(base),
                default_model: default_local_model().to_string(),
                default_model_size: default_local_model_size().to_string(),
                default_model_present: present,
                installed_models: models,
                error: None,
            })
        }
        Err(e) => Ok(BundledOllamaStatus {
            running: false,
            bundled,
            port,
            base_url: Some(base),
            default_model: default_local_model().to_string(),
            default_model_size: default_local_model_size().to_string(),
            default_model_present: false,
            installed_models: vec![],
            error: Some(e),
        }),
    }
}

#[derive(Debug, Serialize, Clone)]
pub struct PullProgressEvent {
    pub model: String,
    pub status: Option<String>,
    pub digest: Option<String>,
    pub total: Option<u64>,
    pub completed: Option<u64>,
    pub percent: Option<f64>,
    pub done: bool,
    pub error: Option<String>,
}

/// Streams `POST /api/pull` line-by-line to the frontend as
/// `ollama.model.pull.progress` events. Returns the terminal status string.
#[tauri::command]
pub async fn pull_default_model(
    app: AppHandle,
    state: tauri::State<'_, OllamaState>,
    model: Option<String>,
) -> Result<String, String> {
    let base = {
        let guard = state.0.lock().unwrap();
        guard
            .as_ref()
            .map(|h| h.base_url.clone())
            .ok_or_else(|| "ollama not initialized".to_string())?
    };
    let model_name = model.unwrap_or_else(|| default_local_model().to_string());

    let url = format!("{}/api/pull", base.trim_end_matches('/'));
    let client = reqwest::Client::builder()
        // Pulling a 3.4 GB model over a slow link shouldn't time out.
        .timeout(Duration::from_secs(60 * 60))
        .build()
        .map_err(|e| e.to_string())?;

    let body = serde_json::json!({ "model": model_name, "stream": true });
    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("pull request failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("pull HTTP {status}: {text}"));
    }

    // Ollama returns a sequence of JSON objects separated by newlines.
    use futures_util::StreamExt;
    let mut stream = resp.bytes_stream();
    let mut buf = Vec::<u8>::new();
    let mut last_status = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("stream chunk error: {e}"))?;
        buf.extend_from_slice(&chunk);
        // Emit any complete lines we have accumulated.
        while let Some(idx) = buf.iter().position(|b| *b == b'\n') {
            let line = buf.drain(..=idx).collect::<Vec<u8>>();
            let text = String::from_utf8_lossy(&line).trim().to_string();
            if text.is_empty() {
                continue;
            }
            let parsed: serde_json::Value = match serde_json::from_str(&text) {
                Ok(v) => v,
                Err(_) => continue,
            };
            let status = parsed
                .get("status")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let digest = parsed
                .get("digest")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let total = parsed.get("total").and_then(|v| v.as_u64());
            let completed = parsed.get("completed").and_then(|v| v.as_u64());
            let error = parsed
                .get("error")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let percent = match (total, completed) {
                (Some(t), Some(c)) if t > 0 => Some((c as f64 / t as f64) * 100.0),
                _ => None,
            };
            if let Some(s) = &status {
                last_status = s.clone();
            }
            let done = status.as_deref() == Some("success") || error.is_some();

            let event = PullProgressEvent {
                model: model_name.clone(),
                status,
                digest,
                total,
                completed,
                percent,
                done,
                error: error.clone(),
            };
            let _ = app.emit("ollama.model.pull.progress", event);

            if let Some(err) = error {
                return Err(err);
            }
            if done {
                return Ok(last_status.clone());
            }
        }
    }

    Ok(last_status)
}

/// Return the base URL the Python sidecar should use when spawned.
/// Always appends `/v1` because the OpenAI-compat endpoint lives there.
pub fn sidecar_base_url(state: &OllamaState) -> Option<String> {
    let provider = std::env::var("CRD_PROVIDER").unwrap_or_default();
    let provider_norm = provider.trim().to_lowercase();
    if provider_norm == "ollama-cloud" || provider_norm == "openrouter" {
        // Cloud mode — the sidecar resolves its own base URL from CRD_PROVIDER_*.
        // Don't override.
        return None;
    }
    let guard = state.0.lock().ok()?;
    guard
        .as_ref()
        .map(|h| format!("{}/v1", h.base_url.trim_end_matches('/')))
}

/// Absolute path of the directory containing the bundled LiteParse binary
/// inside the running .app — used to prepend PATH for the sidecar. Returns
/// `None` in dev builds or when the resource is missing.
pub fn bundled_liteparse_dir(app: &AppHandle) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        return None;
    }
    let resource_dir = app.path().resource_dir().ok()?;
    let candidates = [
        resource_dir.join("_up_/vendor/liteparse"),
        resource_dir.join("vendor/liteparse"),
        resource_dir.join("liteparse"),
    ];
    candidates.into_iter().find(|p| p.is_dir())
}

/// Absolute path of the directory containing the bundled Playwright
/// chromium-headless-shell install — used to set PLAYWRIGHT_BROWSERS_PATH
/// on the sidecar child so the Python render/axe_runner locates the
/// bundled shell. Returns `None` in dev builds or when the resource is
/// missing (dev uses the user's ~/Library/Caches/ms-playwright cache).
pub fn bundled_playwright_browsers_dir(app: &AppHandle) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        return None;
    }
    let resource_dir = app.path().resource_dir().ok()?;
    let candidates = [
        resource_dir.join("_up_/vendor/chromium"),
        resource_dir.join("vendor/chromium"),
        resource_dir.join("chromium"),
    ];
    candidates.into_iter().find(|p| p.is_dir())
}

/// Absolute path of the .app's bundled `vendor/` directory — the one that
/// holds axe.min.js, canvas-mock.css, and the three sibling runtime
/// subdirs (ollama/, liteparse/, chromium/). Exported so the Rust shell
/// can hand the Python sidecar a `CRD_VENDOR_DIR` env var that
/// lets it resolve bundled data files without relying on the PyInstaller
/// onefile temp extraction layout (which strips `__file__`-relative
/// sibling dirs).
pub fn bundled_vendor_dir(app: &AppHandle) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        return None;
    }
    let resource_dir = app.path().resource_dir().ok()?;
    let candidates = [
        resource_dir.join("_up_/vendor"),
        resource_dir.join("vendor"),
    ];
    candidates.into_iter().find(|p| p.is_dir())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_present_matches_exact() {
        let models = vec!["gemma4:e4b".to_string(), "nomic-embed-text:v1".into()];
        assert!(model_present(&models, "gemma4:e4b"));
    }

    #[test]
    fn model_present_matches_stem() {
        let models = vec!["gemma4:e4b-q4".to_string()];
        assert!(model_present(&models, "gemma4:e4b"));
    }

    #[test]
    fn model_missing() {
        let models = vec!["llama3.2:1b".to_string()];
        assert!(!model_present(&models, "gemma4:e4b"));
    }
}
