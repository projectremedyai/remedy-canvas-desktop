//! Detect installed RAM and pick a Qwen 3.5:4B KV-cache context length that
//! won't crowd out the OS / app / Chromium / everything else.
//!
//! qwen3.5:4b weights are ~3.4 GB. KV cache at Q8 is roughly ~1 GB per 32k
//! tokens; we size the context tier to keep the total AI footprint well
//! under half of installed RAM so remediation can still run the browser +
//! Python sidecar + docs pipeline concurrently.
//!
//! Tiers are chosen conservatively on purpose — a user can always raise the
//! cap via the `CRD_OLLAMA_NUM_CTX` env var.

use serde::Serialize;
use sysinfo::System;

/// Context-length tier derived from installed system memory.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct ContextTier {
    pub total_memory_gb: u64,
    pub num_ctx: u32,
    pub label: &'static str,
}

/// Read the environment override, or pick a tier from total physical memory.
///
/// Returns the tier to use + whether it came from the env (so the UI can
/// disclose "32k (auto-detected)" vs "16k (user override)").
pub fn resolve_context_tier() -> (ContextTier, bool) {
    if let Ok(raw) = std::env::var("CRD_OLLAMA_NUM_CTX") {
        if let Ok(num_ctx) = raw.trim().parse::<u32>() {
            let override_tier = ContextTier {
                total_memory_gb: detect_total_memory_gb(),
                num_ctx,
                label: "user override",
            };
            return (override_tier, true);
        }
    }
    (pick_tier(detect_total_memory_gb()), false)
}

pub fn detect_total_memory_gb() -> u64 {
    let mut system = System::new();
    system.refresh_memory();
    // sysinfo::System::total_memory() is bytes on 0.30+; round to GB with a
    // small floor so 15.9 GB machines report 16, not 15.
    let bytes = system.total_memory();
    (bytes + 512 * 1024 * 1024) / (1024 * 1024 * 1024)
}

/// Memory-tier → context size. Edit with care; each step up roughly doubles
/// the KV-cache footprint.
fn pick_tier(total_gb: u64) -> ContextTier {
    let (num_ctx, label) = match total_gb {
        0..=8 => (8_192, "tight — 8k context"),
        9..=16 => (32_768, "comfortable — 32k context"),
        17..=32 => (65_536, "generous — 64k context"),
        33..=64 => (131_072, "spacious — 128k context"),
        _ => (262_144, "full — 256k context"),
    };
    ContextTier {
        total_memory_gb: total_gb,
        num_ctx,
        label,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn low_mem_pins_to_8k() {
        assert_eq!(pick_tier(4).num_ctx, 8_192);
        assert_eq!(pick_tier(8).num_ctx, 8_192);
    }

    #[test]
    fn macbook_air_tier() {
        // 16 GB is the common target instructor laptop.
        assert_eq!(pick_tier(16).num_ctx, 32_768);
    }

    #[test]
    fn workstation_tier() {
        assert_eq!(pick_tier(32).num_ctx, 65_536);
        assert_eq!(pick_tier(64).num_ctx, 131_072);
    }

    #[test]
    fn max_is_model_ceiling() {
        // Qwen 3.5:4b maxes at 256k regardless of available RAM.
        assert_eq!(pick_tier(128).num_ctx, 262_144);
        assert_eq!(pick_tier(512).num_ctx, 262_144);
    }

    #[test]
    fn detect_returns_nonzero() {
        assert!(detect_total_memory_gb() > 0);
    }
}
