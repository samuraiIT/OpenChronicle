"""Cross-platform-stub AX Tree capture (macOS only in v1).

Wraps the vendored `mac-ax-helper` Swift binary. Ported from Einsia-Partner's
backend/core/capture/ax_capture_service.py with Windows branch removed and
resource resolution adapted for a uv/pip-installable package.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Protocol

from ..logger import get
from .ax_models import AXCaptureResult

logger = get("openchronicle.capture")

_SUBPROCESS_TIMEOUT = 10  # seconds (covers --timeout 3 + overhead)


def _strip_frame_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_frame_fields(v) for k, v in value.items() if k != "frame"}
    if isinstance(value, list):
        return [_strip_frame_fields(item) for item in value]
    return value


def _maybe_compile(swift_path: Path, binary_path: Path) -> None:
    """Dev/first-run: compile the helper if missing or stale."""
    if not swift_path.is_file():
        return
    if binary_path.is_file():
        if binary_path.stat().st_mtime >= swift_path.stat().st_mtime:
            return
        logger.info("mac-ax-helper: source newer than binary, recompiling")
    else:
        logger.info("mac-ax-helper: binary missing, compiling from source")

    cache = Path("/tmp/clang-module-cache")
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(cache)
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
    target = f"{arch}-apple-macos12.0"
    try:
        result = subprocess.run(
            [
                "swiftc",
                str(swift_path),
                "-o",
                str(binary_path),
                "-O",
                "-target",
                target,
                "-swift-version",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("mac-ax-helper compile failed: %s (install Xcode CLT?)", exc)
        return
    if result.returncode != 0:
        logger.warning(
            "mac-ax-helper compile failed (%d): %s",
            result.returncode,
            result.stderr.strip()[:300],
        )


def _resolve_helper_path() -> Path | None:
    """Find the AX tree helper binary (macOS or Linux).

    Search order:
      1. OPENCHRONICLE_AX_HELPER env var (absolute path)
      2. Packaged resource shipped with the wheel (_bundled/)
      3. Dev source tree (../../../resources/ relative to this file)
    """
    override = os.environ.get("OPENCHRONICLE_AX_HELPER")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.warning("OPENCHRONICLE_AX_HELPER set but not executable: %s", p)

    candidates: list[Path] = []

    # 1. Bundled inside the installed package (wheel ships .swift/.py; binary built on demand)
    try:
        from importlib.resources import files as _pkg_files

        bundled_dir = Path(str(_pkg_files("openchronicle").joinpath("_bundled")))
        candidates.append(bundled_dir / "mac-ax-helper")
        candidates.append(bundled_dir / "linux-ax-helper")
    except (ModuleNotFoundError, ValueError):
        pass

    # 2. Dev source tree
    dev_root = Path(__file__).resolve().parents[3]  # .../OpenChronicle/
    candidates.append(dev_root / "resources" / "mac-ax-helper")
    candidates.append(dev_root / "resources" / "linux-ax-helper")

    for binary_path in candidates:
        swift_path = binary_path.with_suffix(".swift")
        py_path = binary_path.with_suffix(".py")
        if swift_path.is_file():
            _maybe_compile(swift_path, binary_path)
        elif py_path.is_file():
            if os.access(py_path, os.X_OK):
                return py_path
            elif os.access(py_path, os.R_OK):
                os.chmod(py_path, py_path.stat().st_mode | 0o111)
                if os.access(py_path, os.X_OK):
                    return py_path
        if binary_path.is_file() and os.access(binary_path, os.X_OK):
            return binary_path

    if platform.system() != "Darwin" and platform.system() != "Linux":
        return None

    return None


class AXProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None: ...

    def capture_all_visible(self) -> AXCaptureResult | None: ...

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None: ...


class UnavailableAXProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None:
        return None

    def capture_all_visible(self) -> AXCaptureResult | None:
        return None

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None:
        return None


class MacAXHelperProvider:
    """Subprocess wrapper around the vendored mac-ax-helper Swift binary."""

    def __init__(self, *, helper_path: Path, depth: int, timeout: int, raw: bool = False) -> None:
        self._helper_path = str(helper_path)
        self._depth = depth
        self._timeout = timeout
        self._raw = raw

    @property
    def available(self) -> bool:
        return True

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None:
        return self._run(all_visible=False, focused_window_only=focused_window_only)

    def capture_all_visible(self) -> AXCaptureResult | None:
        return self._run(all_visible=True)

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None:
        return self._run(
            all_visible=False, app_name=app_name, focused_window_only=focused_window_only
        )

    def _run(
        self,
        *,
        all_visible: bool,
        app_name: str | None = None,
        focused_window_only: bool = False,
    ) -> AXCaptureResult | None:
        args: list[str] = [self._helper_path]
        if app_name:
            args.extend(["--app-name", app_name])
        elif all_visible:
            args.append("--all-visible")
        if focused_window_only:
            args.append("--focused-window-only")
        if self._raw:
            args.append("--raw")
        if self._depth > 0:
            args.extend(["--depth", str(self._depth)])
        args.extend(["--timeout", str(self._timeout)])

        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.warning("mac-ax-helper timed out after %ds", _SUBPROCESS_TIMEOUT)
            return None
        except OSError as exc:
            logger.error("Failed to run mac-ax-helper: %s", exc)
            return None

        if proc.returncode == 2:
            logger.warning(
                "Accessibility permission not granted. "
                "Grant access to your terminal in System Settings → Privacy & Security → Accessibility."
            )
            return None
        if proc.returncode != 0:
            logger.warning(
                "mac-ax-helper exited %d: %s", proc.returncode, proc.stderr.strip()[:200]
            )
            return None

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse mac-ax-helper JSON: %s", exc)
            return None

        data = _strip_frame_fields(data)
        mode = "all-visible" if all_visible else "frontmost"
        return AXCaptureResult(
            raw_json=data,
            timestamp=data.get("timestamp", ""),
            apps=data.get("apps", []),
            metadata={"mode": mode, "depth": self._depth, "platform": "macos", "raw": self._raw},
        )


class LinuxAXHelperProvider:
    """Subprocess wrapper around linux-ax-helper.py (Python AT-SPI)."""

    def __init__(self, *, helper_path: Path, depth: int, timeout: int, raw: bool = False) -> None:
        self._helper_path = str(helper_path)
        self._depth = depth
        self._timeout = timeout
        self._raw = raw

    @property
    def available(self) -> bool:
        return True

    def capture_frontmost(self, *, focused_window_only: bool = True) -> AXCaptureResult | None:
        return self._run()

    def capture_all_visible(self) -> AXCaptureResult | None:
        return self._run()

    def capture_app(
        self, app_name: str, *, focused_window_only: bool = True
    ) -> AXCaptureResult | None:
        return self._run()

    def _run(self) -> AXCaptureResult | None:
        args: list[str] = ["/usr/bin/python3", self._helper_path, str(self._depth)]
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.warning("linux-ax-helper timed out after %ds", _SUBPROCESS_TIMEOUT)
            return None
        except OSError as exc:
            logger.error("Failed to run linux-ax-helper: %s", exc)
            return None

        if proc.returncode != 0:
            logger.warning(
                "linux-ax-helper exited %d: %s", proc.returncode, proc.stderr.strip()[:200]
            )
            return None

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse linux-ax-helper JSON: %s", exc)
            return None

        if "error" in data:
            logger.warning("linux-ax-helper error: %s", data["error"])
            return None

        data = _strip_frame_fields(data)
        return AXCaptureResult(
            raw_json=data,
            timestamp=data.get("timestamp", ""),
            apps=data.get("apps", []),
            metadata={"mode": "tree-dump", "depth": self._depth, "platform": "linux", "raw": self._raw},
        )


def create_provider(*, depth: int = 8, timeout: int = 3, raw: bool = False) -> AXProvider:
    system = platform.system()
    if system == "Darwin":
        helper = _resolve_helper_path()
        if helper is None:
            return UnavailableAXProvider(
                "mac-ax-helper not found. Build it: bash resources/build-mac-ax-helper.sh"
            )
        logger.info("AX capture initialized (macOS): %s", helper)
        return MacAXHelperProvider(helper_path=helper, depth=depth, timeout=timeout, raw=raw)
    elif system == "Linux":
        helper = _resolve_helper_path()
        if helper is None:
            return UnavailableAXProvider(
                "linux-ax-helper not found. Place linux-ax-helper.py in resources/"
            )
        logger.info("AX capture initialized (Linux): %s", helper)
        return LinuxAXHelperProvider(helper_path=helper, depth=depth, timeout=timeout, raw=raw)
    else:
        return UnavailableAXProvider(f"unsupported platform: {system}")
