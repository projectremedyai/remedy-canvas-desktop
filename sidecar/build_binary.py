"""Phase 7a — PyInstaller build script for the crd-sidecar.

Produces a one-directory bundle at ``sidecar/dist/crd-sidecar/`` and
copies/renames the root executable (plus its ``_internal`` folder) into
``sidecar/dist/bin/`` with the triple-suffixed basename that Tauri v2's
``externalBin`` convention expects on this host.

Usage (from the repo root):

    cd sidecar
    uv run python build_binary.py

Or with pyinstaller auto-installed in a throwaway env:

    uv run --with pyinstaller python sidecar/build_binary.py

The resulting binary speaks JSON-RPC 2.0 over stdio. Smoke test:

    echo '{"jsonrpc":"2.0","id":1,"method":"ping"}' \
        | sidecar/dist/bin/crd-sidecar-aarch64-apple-darwin
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "crd-sidecar"
# Tauri v2 externalBin matches files by basename + host triple suffix.
# On this host the triple is `aarch64-apple-darwin`.
HOST_TRIPLE_OVERRIDE = None  # set to e.g. "aarch64-apple-darwin" to force


def detect_host_triple() -> str:
    if HOST_TRIPLE_OVERRIDE:
        return HOST_TRIPLE_OVERRIDE
    mach = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        arch = "aarch64" if mach in {"arm64", "aarch64"} else "x86_64"
        return f"{arch}-apple-darwin"
    if system == "windows":
        return "x86_64-pc-windows-msvc"
    if system == "linux":
        arch = "aarch64" if mach in {"arm64", "aarch64"} else "x86_64"
        return f"{arch}-unknown-linux-gnu"
    raise RuntimeError(f"unsupported host: {system} {mach}")


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    sidecar_dir = Path(__file__).resolve().parent
    dist_dir = sidecar_dir / "dist"
    build_dir = sidecar_dir / "build"
    work_dir = sidecar_dir / "build" / "work"
    bin_dir = dist_dir / "bin"

    # Clean previous outputs to avoid stale shadows.
    for d in (dist_dir, build_dir):
        if d.exists():
            shutil.rmtree(d)
    bin_dir.mkdir(parents=True, exist_ok=True)

    entry = sidecar_dir / "crd_sidecar" / "main.py"
    if not entry.exists():
        raise SystemExit(f"entrypoint missing: {entry}")

    # PyInstaller CLI. We use --onefile so Tauri's externalBin convention
    # (a single file copied into Contents/MacOS/) gets everything it needs.
    # The one-dir layout can't survive Tauri's split between Contents/MacOS/
    # (the exe) and Contents/Resources/ (everything else), because
    # PyInstaller's bootstrap looks for _internal next to the binary.
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(build_dir),
        "--collect-all",
        "pydantic",
        "--collect-all",
        "pydantic_core",
        "--collect-all",
        "structlog",
        "--collect-all",
        "openai",
        "--collect-all",
        "bs4",
        "--collect-all",
        "lxml",
        "--collect-all",
        "ulid",
        # Playwright: collect the Python package + its driver files. The
        # async_playwright API resolves driver paths relative to the package
        # directory; inside a PyInstaller onefile extraction dir that means
        # we must ship the whole package tree (including the Node-based
        # driver under playwright/driver/). Missing this → "executable not
        # found" on launch even when PLAYWRIGHT_BROWSERS_PATH is set.
        "--collect-all",
        "playwright",
        # LiteParse Python wrapper — optional extra at the pyproject level
        # but MANDATORY in the packaged sidecar since document conversion
        # is a first-class feature of the shipped app.
        "--collect-all",
        "liteparse",
        # Our own package — forces PyInstaller to include the submodules
        # that get imported lazily (orchestrator, crd_core.*, imscc.*, render.*).
        "--collect-submodules",
        "crd_sidecar",
        # Ship the ACR export template + vpat_criteria data alongside the
        # Python modules. Jinja2's PackageLoader resolves them relative to
        # the package directory, which in a PyInstaller onefile build is
        # the extracted _MEIPASS tree — these --add-data flags plant them
        # where the loader expects to find them. Source paths MUST be
        # absolute — PyInstaller resolves relative sources against --specpath
        # (build/), which won't have these files.
        "--add-data",
        f"{sidecar_dir / 'crd_sidecar' / 'templates'}"
        f":crd_sidecar/templates",
        "--add-data",
        f"{sidecar_dir / 'crd_sidecar' / 'crd_core' / 'accessibility' / 'vpat_criteria.json'}"
        f":crd_sidecar/crd_core/accessibility",
        # lxml has a handful of submodules PyInstaller occasionally misses.
        "--hidden-import",
        "lxml.etree",
        "--hidden-import",
        "lxml._elementpath",
        "--hidden-import",
        "lxml.builder",
        # Playwright's greenlet + pyee dependencies ride along but the
        # imports happen through async_playwright factories; call them
        # out so PyInstaller doesn't prune them on dead-code analysis.
        "--hidden-import",
        "greenlet",
        "--hidden-import",
        "pyee",
        str(entry),
    ]
    run(cmd)

    # --onefile layout: a single executable lives directly in dist/.
    exe_name = APP_NAME + (".exe" if sys.platform.startswith("win") else "")
    root_exe = dist_dir / exe_name
    if not root_exe.exists():
        raise SystemExit(f"expected bundled executable at {root_exe}")

    # Tauri v2 externalBin convention: the file referenced by
    # `bundle.externalBin` must exist on disk with the host-triple suffix.
    triple = detect_host_triple()
    triple_basename = f"{APP_NAME}-{triple}" + (
        ".exe" if sys.platform.startswith("win") else ""
    )
    triple_exe = bin_dir / triple_basename
    shutil.copy2(root_exe, triple_exe)
    triple_exe.chmod(0o755)

    print("\n--- build complete ---")
    print(f"  sidecar: {triple_exe}")
    print(
        "\nSmoke test:\n"
        f"  echo '{{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}}' | "
        f"{triple_exe}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
