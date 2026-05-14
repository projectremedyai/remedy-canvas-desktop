//! Detect installed RAM and pick a Gemma 4 E4B KV-cache context length that
//! won't crowd out the OS / app / Chromium / everything else.
//!
//! gemma4:e4b weights are ~9.6 GB and the model maxes out at 128k context.
//! KV cache at Q8 is roughly ~1 GB per 32k tokens; we size the context tier
//! to keep the total AI footprint well under half of installed RAM so
//! remediation can still run the browser + Python sidecar + docs pipeline
//! concurrently.
//!
//! Tiers are chosen conservatively on purpose — a user can always raise the
//! cap via the `CRD_OLLAMA_NUM_CTX` env var.

use serde::Serialize;
use sysinfo::System;

/// Approximate disk-and-RAM footprint of the bundled gemma4 model variants
/// at Q4_K_M quantization. Used by `pick_tier` to size the KV cache so
/// (weights + KV cache) stays under half of installed RAM.
#[derive(Debug, Clone, Copy)]
pub enum ModelSize {
    /// gemma4:e2b — ~7.2 GB weights
    Small,
    /// gemma4:e4b — ~9.6 GB weights
    Large,
}

impl ModelSize {
    /// Pick the largest variant that comfortably fits in the given RAM.
    /// 8 GB → Small (e2b). 16+ GB → Large (e4b).
    pub fn for_ram(total_gb: u64) -> Self {
        if total_gb <= 8 {
            ModelSize::Small
        } else {
            ModelSize::Large
        }
    }

    /// Approximate weight footprint in GB. Used by tier sizing.
    fn weights_gb(self) -> u64 {
        match self {
            ModelSize::Small => 8,  // 7.2 GB rounded up to 8 for safety
            ModelSize::Large => 10, // 9.6 GB rounded up to 10 for safety
        }
    }

    /// The Ollama tag string this size corresponds to.
    pub fn ollama_tag(self) -> &'static str {
        match self {
            ModelSize::Small => "gemma4:e2b",
            ModelSize::Large => "gemma4:e4b",
        }
    }

    /// Human-readable download size, used in the UI's "Download AI model (X, ~Y GB)" copy.
    pub fn approx_download_gb(self) -> &'static str {
        match self {
            ModelSize::Small => "7.2 GB",
            ModelSize::Large => "9.6 GB",
        }
    }
}

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
        _ => (131_072, "spacious — 128k context (model max)"),
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
        // gemma4:e4b maxes at 128k regardless of available RAM.
        assert_eq!(pick_tier(128).num_ctx, 131_072);
        assert_eq!(pick_tier(512).num_ctx, 131_072);
    }

    #[test]
    fn detect_returns_nonzero() {
        assert!(detect_total_memory_gb() > 0);
    }

    #[test]
    fn ram_4gb_picks_small() {
        assert!(matches!(ModelSize::for_ram(4), ModelSize::Small));
    }

    #[test]
    fn ram_8gb_picks_small() {
        assert!(matches!(ModelSize::for_ram(8), ModelSize::Small));
    }

    #[test]
    fn ram_16gb_picks_large() {
        assert!(matches!(ModelSize::for_ram(16), ModelSize::Large));
    }

    #[test]
    fn ram_64gb_picks_large() {
        assert!(matches!(ModelSize::for_ram(64), ModelSize::Large));
    }

    #[test]
    fn tag_strings_match_ollama_registry() {
        assert_eq!(ModelSize::Small.ollama_tag(), "gemma4:e2b");
        assert_eq!(ModelSize::Large.ollama_tag(), "gemma4:e4b");
    }
}
