use pyo3::prelude::*;
use pyo3::exceptions::PyValueError;
use argon2::{Argon2, Algorithm, Version, Params};
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use aes_gcm::aead::Aead;
use aes_gcm::aead::{AeadCore, OsRng};
use zeroize::Zeroize;
use base64::Engine;

// ---------- Platform-specific memory locking ----------

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "freebsd", target_os = "openbsd"))]
mod platform {
    use libc::{c_void, mlock, munlock};
    pub unsafe fn lock(ptr: *const c_void, len: usize) -> i32 { mlock(ptr, len) }
    pub unsafe fn unlock(ptr: *const c_void, len: usize) -> i32 { munlock(ptr, len) }
}

#[cfg(target_os = "windows")]
mod platform {
    use winapi::um::memoryapi::{VirtualLock, VirtualUnlock};
    use winapi::ctypes::c_void;
    pub unsafe fn lock(ptr: *const c_void, len: usize) -> i32 {
        // VirtualLock returns BOOL (0=fail, !=0=success). We invert for errno-style (0=ok).
        if VirtualLock(ptr as *mut c_void, len) == 0 { -1 } else { 0 }
    }
    pub unsafe fn unlock(ptr: *const c_void, len: usize) -> i32 {
        if VirtualUnlock(ptr as *mut c_void, len) == 0 { -1 } else { 0 }
    }
}

/// A secure memory buffer that locks pages in RAM (prevents swap)
/// and zeroizes on drop.
/// NOTE: On Windows, VirtualLock has a working set limit (default ~4MB).
/// Our 32-byte buffer is tiny, so this is safe.
struct SecureBuffer {
    // NonNull prevents compiler from optimizing away zeroize?
    // Vec<u8> with ZeroizeOnDrop is sufficient.
    data: Vec<u8>,
    // Track if lock succeeded so we only unlock if we locked.
    locked: bool,
}

impl SecureBuffer {
    fn new(size: usize) -> Result<Self, String> {
        // Allocate zeroed memory
        let data = vec![0u8; size];
        let ptr = data.as_ptr() as *const std::ffi::c_void;

        // Attempt Lock
        let ret = unsafe { platform::lock(ptr, size) };
        let locked = ret == 0;

        if !locked {
            // Log warning but DO NOT FAIL.
            // Failing closed breaks Docker/CI/Restricted environments.
            eprintln!("[cogni_vault] WARNING: Memory lock failed (errno={}). Keys may be swapped to disk.", ret);
        }

        Ok(Self { data, locked })
    }

    fn as_mut_slice(&mut self) -> &mut [u8] {
        &mut self.data
    }
}

impl Drop for SecureBuffer {
    fn drop(&mut self) {
        // 1. Zeroize (ZeroizeOnDrop handles this automatically via derive,
        // but explicit call here ensures order: Zero -> Unlock)
        self.data.zeroize();

        // 2. Unlock
        if self.locked {
            let ptr = self.data.as_ptr() as *const std::ffi::c_void;
            let _ = unsafe { platform::unlock(ptr, self.data.len()) };
        }
    }
}

/// Internal helper: Derives a KEK using Argon2id into a SecureBuffer.
/// REQUIRES `py: Python` to release GIL.
fn derive_kek_internal(py: Python, passphrase_bytes: &[u8], salt_bytes: &[u8]) -> Result<SecureBuffer, String> {
    let params = Params::new(64 * 1024, 3, 1, Some(32)) // 64MB, 3 iter, 1 lane, 32B out
    .map_err(|e| format!("Argon2 params error: {}", e))?;

    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let mut kek_buffer = SecureBuffer::new(32)?;

    // RELEASE GIL HERE: Argon2 is heavy (CPU + Memory)
    py.allow_threads(|| {
        argon2.hash_password_into(passphrase_bytes, salt_bytes, kek_buffer.as_mut_slice())
    }).map_err(|e| format!("KDF failed: {}", e))?;

    Ok(kek_buffer)
}

/// Unwraps (decrypts) the DEK. Returns the DEK as a hex string.
#[pyfunction]
fn unwrap_dek_secure(
    py: Python,
    encrypted_dek_b64: &str,
    nonce_b64: &str,
    passphrase_bytes: &[u8],
    salt_bytes: &[u8]
) -> PyResult<Vec<u8>> {

    let mut kek_buffer = derive_kek_internal(py, passphrase_bytes, salt_bytes)
    .map_err(|e| PyValueError::new_err(e))?;

    let cipher = Aes256Gcm::new_from_slice(&kek_buffer.data)
    .map_err(|_| PyValueError::new_err("Invalid KEK length"))?;

    // Zeroize KEK immediately after cipher initialization
    kek_buffer.data.zeroize();

    let nonce_bytes = base64::engine::general_purpose::STANDARD.decode(nonce_b64)
    .map_err(|_| PyValueError::new_err("Invalid nonce"))?;
    let nonce = Nonce::from_slice(&nonce_bytes);

    let ciphertext = base64::engine::general_purpose::STANDARD.decode(encrypted_dek_b64)
    .map_err(|_| PyValueError::new_err("Invalid ciphertext"))?;

    let dek_bytes = cipher.decrypt(nonce, ciphertext.as_ref())
    .map_err(|_| PyValueError::new_err("Decryption failed (Wrong Passphrase or Tampered Envelope)"))?;

    // dek_bytes is moved into the returned PyResult<Vec<u8>> — PyO3 converts it
    // to a Python `bytes` object directly, no intermediate hex string, no extra copy.
    Ok(dek_bytes)
}

/// Wraps (encrypts) the DEK. Returns a tuple of (nonce_b64, ciphertext_b64).
#[pyfunction]
fn wrap_dek_secure(
    py: Python, // <--- ADD Python GIL Token
    dek_bytes: &[u8],
    passphrase_bytes: &[u8],
    salt_bytes: &[u8]
) -> PyResult<(String, String)> {

    let mut kek_buffer = derive_kek_internal(py, passphrase_bytes, salt_bytes) // <--- PASS PY
    .map_err(|e| PyValueError::new_err(e))?;

    let cipher = Aes256Gcm::new_from_slice(&kek_buffer.data)
    .map_err(|_| PyValueError::new_err("Invalid KEK length"))?;

    kek_buffer.data.zeroize();

    // Explicit CSPRNG
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let nonce_b64 = base64::engine::general_purpose::STANDARD.encode(nonce);
    let ciphertext = cipher.encrypt(&nonce, dek_bytes)
    .map_err(|_| PyValueError::new_err("Encryption failed"))?;

    let ciphertext_b64 = base64::engine::general_purpose::STANDARD.encode(ciphertext);

    Ok((nonce_b64, ciphertext_b64))
}

/// Python Module Definition
#[pymodule]
fn cogni_vault(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(unwrap_dek_secure, m)?)?;
    m.add_function(wrap_pyfunction!(wrap_dek_secure, m)?)?;
    Ok(())
}
