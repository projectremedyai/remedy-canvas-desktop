"""Zip-bomb + malicious-archive guards for IMSCC ingestion.

IMSCC files are user-supplied — typically a course export from
Canvas, but the sidecar has no way to verify provenance. A maliciously
crafted zip could:
- Expand to gigabytes (memory exhaustion)
- Contain millions of tiny members (file-descriptor / metadata DoS)
- Have one member with an extreme compression ratio (decompression DoS)

This module validates an archive against conservative caps BEFORE any
read, and offers a bounded-read helper that enforces per-member size
limits at read time.

Caps are sized for real Canvas course exports (typically 10-500 MB
uncompressed, <5000 files) with generous headroom; legitimate courses
should never trip them.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# --- Defaults ----------------------------------------------------------------

MAX_TOTAL_UNCOMPRESSED = 5 * 1024 * 1024 * 1024  # 5 GB
MAX_MEMBERS = 50_000
MAX_MEMBER_UNCOMPRESSED = 1 * 1024 * 1024 * 1024  # 1 GB per file
MAX_COMPRESSION_RATIO = 300  # flag anything compressing > 300x


class UnsafeArchive(ValueError):
    """Raised when an archive exceeds a configured safety cap."""


def validate_archive(
    archive_path: str | Path,
    *,
    max_total_uncompressed: int = MAX_TOTAL_UNCOMPRESSED,
    max_members: int = MAX_MEMBERS,
    max_member_uncompressed: int = MAX_MEMBER_UNCOMPRESSED,
    max_compression_ratio: float = MAX_COMPRESSION_RATIO,
) -> None:
    """Walk the zip's central directory (no decompression) and raise
    UnsafeArchive if any cap is exceeded.

    This is the first-line defense called once per IMSCC parse. It
    inspects only the zip's metadata; no member bytes are read. That
    keeps the check cheap even for genuinely large archives.
    """
    with zipfile.ZipFile(archive_path) as zf:
        infos = zf.infolist()

    if len(infos) > max_members:
        raise UnsafeArchive(
            f"archive contains {len(infos)} entries (cap: {max_members})"
        )

    total = 0
    for info in infos:
        if info.file_size > max_member_uncompressed:
            raise UnsafeArchive(
                f"entry {info.filename!r} uncompressed size "
                f"{info.file_size} > cap {max_member_uncompressed}"
            )
        # compress_size can be 0 for stored (uncompressed) entries — skip
        # the ratio check there since there's no decompression DoS.
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > max_compression_ratio:
                raise UnsafeArchive(
                    f"entry {info.filename!r} compression ratio "
                    f"{ratio:.1f}x > cap {max_compression_ratio}x"
                )
        total += info.file_size
        if total > max_total_uncompressed:
            raise UnsafeArchive(
                f"archive total uncompressed size > cap "
                f"{max_total_uncompressed}"
            )


def read_member_bounded(
    zf: zipfile.ZipFile,
    member: str,
    *,
    max_size: int = MAX_MEMBER_UNCOMPRESSED,
) -> bytes:
    """Read a single member but refuse to buffer more than ``max_size``
    bytes. Defense-in-depth against a member whose reported ``file_size``
    in the central directory underreports actual decompressed output.

    Read happens in 1 MB chunks so a malicious member stops reading the
    moment the cap is exceeded instead of exhausting memory first.
    """
    with zf.open(member) as stream:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise UnsafeArchive(
                    f"member {member!r} exceeded size cap {max_size} "
                    f"while reading — possible zip bomb"
                )
            chunks.append(chunk)
    return b"".join(chunks)
