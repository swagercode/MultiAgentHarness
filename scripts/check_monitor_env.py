from __future__ import annotations

from pathlib import Path
import json
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import codex_multiagent_loop as worker  # noqa: E402


def run(command: list[str], timeout: int = 30) -> dict:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "timed_out": False,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": isinstance(exc, subprocess.TimeoutExpired),
        }


def bool_text(value: bool) -> str:
    return "yes" if value else "no"


def main() -> int:
    codex = worker.detect_codex("codex", 30, False)
    powershell_path = shutil.which("powershell.exe") or shutil.which("powershell")
    powershell_version = (
        run([powershell_path, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"])
        if powershell_path
        else None
    )
    native_windows_ready = (
        platform.system().lower() == "windows"
        and bool(powershell_path)
        and powershell_version is not None
        and powershell_version["returncode"] == 0
        and codex["available"]
    )

    lines = [
        f"platform={platform.platform()}",
        f"python_executable={sys.executable}",
        f"cwd={Path.cwd()}",
        f"repo_root_detected={bool_text((ROOT / 'tools' / 'codex_supervisor.py').exists())}",
        f"codex_detected={bool_text(codex['available'])}",
        f"codex_version={codex.get('version_text') or ''}",
        f"codex_path={codex.get('path') or ''}",
        f"powershell_windows_available={bool_text(bool(powershell_path))}",
        f"powershell_windows_path={powershell_path or ''}",
        f"powershell_windows_version={(powershell_version or {}).get('stdout', '')}",
        f"native_codex_windows_ready={bool_text(native_windows_ready)}",
        "recommended_monitor_backend=powershell-windows" if native_windows_ready else "recommended_monitor_backend=BLOCKED",
    ]
    print("\n".join(lines))
    print("\njson_summary=" + json.dumps({"codex": codex, "powershell": powershell_version}, indent=2))
    return 0 if native_windows_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
