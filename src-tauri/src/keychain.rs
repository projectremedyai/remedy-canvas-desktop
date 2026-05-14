//! macOS Keychain wrapper for storing BYOK API keys.
//!
//! All keys live under a single service identifier
//! (`com.canvasremedy.desktop.api-keys`) with the provider name as the
//! account. On non-macOS targets every function compiles to an `Err`
//! returning a `not supported on this platform` message — the .app is
//! macOS-only today, but Windows/Linux builds shouldn't fail to link
//! just because they imported this module.

const SERVICE: &str = "com.canvasremedy.desktop.api-keys";

/// Store an API key under the given provider account.
/// Overwrites any existing entry for the same provider.
#[cfg(target_os = "macos")]
pub fn store_api_key(provider: &str, key: &str) -> Result<(), String> {
    use security_framework::passwords::set_generic_password;
    set_generic_password(SERVICE, provider, key.as_bytes())
        .map_err(|e| format!("keychain set failed: {e}"))
}

/// Read the API key for the given provider. Returns `Ok(None)` if no entry exists.
#[cfg(target_os = "macos")]
pub fn load_api_key(provider: &str) -> Result<Option<String>, String> {
    use security_framework::passwords::get_generic_password;
    match get_generic_password(SERVICE, provider) {
        Ok(bytes) => {
            let s = String::from_utf8(bytes)
                .map_err(|e| format!("keychain value not UTF-8: {e}"))?;
            Ok(Some(s))
        }
        Err(e) => {
            // The crate returns a specific OSStatus for "not found" (errSecItemNotFound = -25300).
            // Rather than match on the opaque error type, treat any "not found"-shaped
            // error as None. Other errors (permission denied etc.) propagate.
            let msg = e.to_string();
            if msg.contains("not found") || msg.contains("could not be found") || msg.contains("-25300") {
                Ok(None)
            } else {
                Err(format!("keychain get failed: {e}"))
            }
        }
    }
}

/// Remove the API key for the given provider. Returns Ok even if nothing was there.
#[cfg(target_os = "macos")]
pub fn delete_api_key(provider: &str) -> Result<(), String> {
    use security_framework::passwords::delete_generic_password;
    match delete_generic_password(SERVICE, provider) {
        Ok(()) => Ok(()),
        Err(e) => {
            let msg = e.to_string();
            if msg.contains("not found") || msg.contains("could not be found") || msg.contains("-25300") {
                Ok(())
            } else {
                Err(format!("keychain delete failed: {e}"))
            }
        }
    }
}

// --- non-macOS stubs ---

#[cfg(not(target_os = "macos"))]
pub fn store_api_key(_provider: &str, _key: &str) -> Result<(), String> {
    Err("keychain not supported on this platform".into())
}

#[cfg(not(target_os = "macos"))]
pub fn load_api_key(_provider: &str) -> Result<Option<String>, String> {
    Ok(None)
}

#[cfg(not(target_os = "macos"))]
pub fn delete_api_key(_provider: &str) -> Result<(), String> {
    Ok(())
}

// --- tests ---
//
// These tests touch the real Keychain. They use a dedicated test service
// suffix and clean up after themselves. On first run the user may see a
// "allow access to Keychain" prompt — that's expected and we ask the
// human to click "Always Allow" once.

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;

    // Override the service for tests so we don't pollute the production
    // entry the running app may have stored.
    const TEST_PROVIDER: &str = "__test-only__";

    fn cleanup() {
        let _ = delete_api_key(TEST_PROVIDER);
    }

    #[test]
    fn roundtrip_store_load_delete() {
        cleanup();
        assert_eq!(load_api_key(TEST_PROVIDER).unwrap(), None);
        store_api_key(TEST_PROVIDER, "secret-value-123").unwrap();
        assert_eq!(load_api_key(TEST_PROVIDER).unwrap(), Some("secret-value-123".into()));
        store_api_key(TEST_PROVIDER, "overwritten").unwrap();
        assert_eq!(load_api_key(TEST_PROVIDER).unwrap(), Some("overwritten".into()));
        delete_api_key(TEST_PROVIDER).unwrap();
        assert_eq!(load_api_key(TEST_PROVIDER).unwrap(), None);
    }

    #[test]
    fn delete_missing_is_ok() {
        cleanup();
        delete_api_key(TEST_PROVIDER).unwrap(); // should not error even though nothing is there
    }
}
