"""Windows-safe stdio configuration.

Forces UTF-8 on the Python side and also flips the console's code page to UTF-8 (65001). Both
matter: Python-level only helps when Python's stdout is a real TTY; code-page flipping lets
subprocesses and child Python ``print()`` calls agree on encoding.
"""

from __future__ import annotations

import codecs
import os
import sys

__all__ = ["configure_windows_stdio", "is_windows", "_install_safe_default_decoder"]

_CONFIGURED = False
_CODEC_WRAP_INSTALLED = False


def is_windows() -> bool:
    """Return True iff running on native Windows (not WSL)."""
    return sys.platform == "win32"


def _flip_console_code_page_to_utf8() -> None:
    """``SetConsoleCP``/``SetConsoleOutputCP`` to CP_UTF8 (65001). Silent on failure: without an
    attached console (redirected stdout, service, PTY-less CI) the calls return 0 and we move on."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def _reconfigure_stream(stream, *, encoding: str = "utf-8", errors: str = "replace") -> None:
    """Reconfigure a text stream to UTF-8 in place; skips streams without ``reconfigure`` (e.g. an
    ``io.StringIO`` substituted during tests)."""
    try:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding=encoding, errors=errors)
    except Exception:
        pass


def _install_safe_default_decoder() -> None:
    """Root-cause fix for ``subprocess._readerthread`` cp1252 UnicodeDecodeError on Windows.

    ``subprocess.Popen(..., text=True)`` opens the child pipe with ``locale.getpreferredencoding``
    (cp1252 here). The pipe's background reader thread calls ``codecs.decode(input, encoding)``
    with no ``errors=`` arg, so any byte that cp1252 can't represent (0x80-0x9F: em-dash, curly
    quotes, ...) raises ``UnicodeDecodeError`` and kills the reader thread. Wrapping
    ``codecs.decode`` to default ``errors="replace"`` neutralises that without changing the
    encoding; the bytes become ``?`` instead of crashing the thread.

    Idempotent; re-running is a no-op.
    """
    global _CODEC_WRAP_INSTALLED
    if _CODEC_WRAP_INSTALLED:
        return
    try:
        original_decode = codecs.decode

        def _safe_decode(data, encoding=None, *args, **kwargs):
            # Only patch the errors= default; leave encoding choice to the caller. ``text=True``
            # Popen passes ``encoding=cp1252, errors=None`` — the None triggers the strict default
            # that throws on 0x90. Replace None with "replace".
            if "errors" not in kwargs and args == ():
                kwargs["errors"] = "replace"
            return original_decode(data, encoding, *args, **kwargs)

        # Defensive: only patch when the original has the signature we expect. ``codecs.decode``'s
        # actual signature is decode(obj, encoding='utf-8', errors='strict').
        if getattr(original_decode, "__module__", "") == codecs.__name__:
            codecs.decode = _safe_decode
            _CODEC_WRAP_INSTALLED = True
    except Exception:
        # Defensive: any failure here must never break the import.
        pass


def configure_windows_stdio(force: bool = False) -> bool:
    """Force UTF-8 stdio on Windows. No-op elsewhere.

    Idempotent; returns ``True`` only when something actually changed. Set
    ``HERMES_DISABLE_WINDOWS_UTF8=1`` to opt out (forces the old cp1252 path for diagnosing
    encoding bugs). Set ``force=True`` to re-apply even after the first successful call (used by
    long-lived daemons that imported us before sitecustomize had a chance to run). Also sets a
    default ``EDITOR`` on Windows if none is set.
    """
    global _CONFIGURED

    if _CONFIGURED and not force:
        return False
    if not is_windows() or os.environ.get("HERMES_DISABLE_WINDOWS_UTF8") in {"1", "true", "True", "yes"}:
        _CONFIGURED = True  # repeated calls on POSIX / opted-out are true no-ops
        return False

    # Make child Python processes use UTF-8 stdio too (PYTHONIOENCODING wins over the locale
    # default; PYTHONUTF8=1 enables UTF-8 Mode, PEP 540). Never override an explicit user setting.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    # prompt_toolkit's ``open_in_editor`` falls back to POSIX-only paths (/usr/bin/nano, /usr/bin/vi)
    # that don't exist on Windows — Ctrl+X Ctrl+E and ``/edit`` silently do nothing there
    # otherwise, even with full Git for Windows installed.
    _default_editor = _default_windows_editor()
    if _default_editor and not os.environ.get("EDITOR") and not os.environ.get("VISUAL"):
        os.environ["EDITOR"] = _default_editor

    _augment_path_with_known_tools()
    # Flip the console code page first so any subprocess inheriting the console also sees CP_UTF8.
    _flip_console_code_page_to_utf8()
    # ``errors="replace"``: a genuinely unencodable sequence prints ``?`` rather than crashing the
    # interpreter. stdin is included for batch/pipe input (prompt_toolkit manages its own encoding).
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        _reconfigure_stream(stream)
    # ROOT-CAUSE FIX: neutralise cp1252's strict-mode decoder so subprocess._readerthread can no
    # longer crash on 0x90/0x9d bytes. Safe no-op if already installed.
    _install_safe_default_decoder()
    _CONFIGURED = True
    return True


def _default_windows_editor() -> str:
    """Windows default for ``$EDITOR``: ``notepad`` (ships with every install, blocks until the
    window closes). The bare name keeps prompt_toolkit's shlex split away from paths with spaces;
    "" when even notepad is missing (WinPE, Nano Server) so prompt_toolkit's no-op applies."""
    import shutil
    return "notepad" if shutil.which("notepad") else ""


def _augment_path_with_known_tools() -> None:
    r"""Prepend Hermes-managed tool directories to ``PATH`` (no-op on POSIX / missing dirs).

    install.ps1 adds entries like ``%LOCALAPPDATA%\hermes\git\bin`` to the User PATH via
    ``SetEnvironmentVariable``, but already-running shells never see that broadcast, so a hermes
    launched from the install session would not find rg / bash / grep. Prepending the known dirs
    at startup closes that first-launch gap.
    """
    if not is_windows():
        return
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return

    # Kept in sync with the PATH entries scripts/install.ps1 adds to User scope. The venv Scripts
    # dir hosts hermes.exe + pip console scripts; WinGet\Links is where ``winget install`` drops
    # CLI shims (ripgrep lands there as rg.exe).
    candidate_dirs = [
        os.path.join(local_appdata, "hermes", "git", "cmd"),
        os.path.join(local_appdata, "hermes", "git", "bin"),
        os.path.join(local_appdata, "hermes", "git", "usr", "bin"),
        os.path.join(local_appdata, "hermes", "hermes-agent", "venv", "Scripts"),
        os.path.join(local_appdata, "Microsoft", "WinGet", "Links")]
    existing = os.environ.get("PATH", "")
    existing_lower = {p.lower() for p in existing.split(os.pathsep) if p}
    prepend = [d for d in candidate_dirs if os.path.isdir(d) and d.lower() not in existing_lower]
    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, existing])
