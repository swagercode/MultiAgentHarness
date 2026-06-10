from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from typing import Any


HARNESS_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path.cwd()
WORKER_AGENT_ORDER = ("planner", "implementer", "tester", "reviewer", "controller")
AGENT_ORDER = ("manager", *WORKER_AGENT_ORDER)
BUILTIN_AGENT_ROLES = set(AGENT_ORDER)
VERDICTS = ("COMPLETE", "PARTIAL", "BLOCKED", "CONTINUE")
CODEX_REMEDIATION = """Install and log in with the official Codex CLI:

npm install -g @openai/codex
codex login
"""


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_array(values: list[str]) -> str:
    return "@(" + ", ".join(powershell_quote(value) for value in values) + ")"


def safe_window_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", name).strip("-")
    return cleaned[:40] or "agent"


def safe_agent_stem(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", name).strip("-")
    return cleaned[:80] or "agent"


class CodexWindowRunner:
    def __init__(self, backend: str = "auto") -> None:
        self.requested_backend = backend
        self.backend: str | None = None
        self.powershell_path = shutil.which("powershell.exe") or shutil.which("powershell")
        self.started = False
        self.diagnostics: list[dict[str, Any]] = []

    def _record(self, command: list[str], result: subprocess.CompletedProcess[str] | None, reason: str) -> None:
        self.diagnostics.append(
            {
                "backend": "powershell-windows",
                "command": command,
                "returncode": None if result is None else result.returncode,
                "stdout": "" if result is None else result.stdout,
                "stderr": "" if result is None else result.stderr,
                "platform": platform.platform(),
                "cwd": str(Path.cwd()),
                "reason": reason,
            }
        )

    def _probe(self, command: list[str], reason: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self._record(command, result, reason)
        return result

    def _print_last_diagnostic(self) -> None:
        if not self.diagnostics:
            return
        item = self.diagnostics[-1]
        print("codex window backend diagnostic:")
        print(f"  command: {' '.join(item['command'])}")
        print(f"  exit_code: {item['returncode']}")
        print(f"  stdout: {item['stdout'].strip()}")
        print(f"  stderr: {item['stderr'].strip()}")
        print(f"  platform: {item['platform']}")

    def ensure(self) -> None:
        if self.started:
            return
        if self.requested_backend not in ("auto", "powershell-windows"):
            raise RuntimeError(f"Unsupported Codex window backend: {self.requested_backend}")
        if platform.system().lower() != "windows":
            raise RuntimeError("Native Codex CLI windows currently require Windows PowerShell consoles.")
        if not self.powershell_path:
            self._record(["powershell.exe"], None, "powershell.exe not found on PATH")
            self._print_last_diagnostic()
            raise RuntimeError("powershell.exe not found; cannot open native Codex CLI windows.")
        version = self._probe(
            [self.powershell_path, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            "check PowerShell availability",
        )
        if version.returncode != 0:
            self._print_last_diagnostic()
            raise RuntimeError("PowerShell is unavailable; cannot open native Codex CLI windows.")
        self.backend = "powershell-windows"
        self.started = True
        print("codex window backend selected: powershell-windows")

    def attach_instructions(self) -> str:
        return "Native Codex CLI PowerShell windows are opened for supervisor and worker agents."

    def run_codex_window(
        self,
        *,
        name: str,
        codex_base_command: list[str],
        prompt_path: Path,
        completion_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
    ) -> "CommandResult":
        self.ensure()
        assert self.powershell_path is not None
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        if completion_path.exists():
            completion_path.unlink()
        launcher_path = prompt_path.with_suffix(".launch.ps1")
        title = f"codex-{safe_window_name(name)}"
        codex_executable = codex_base_command[0]
        codex_args = codex_base_command[1:]
        launcher = f"""$ErrorActionPreference = 'Stop'
$host.UI.RawUI.WindowTitle = {powershell_quote(title)}
$stdoutPath = {powershell_quote(str(stdout_path))}
$stderrPath = {powershell_quote(str(stderr_path))}
$promptPath = {powershell_quote(str(prompt_path))}
$codexCommand = {powershell_quote(codex_executable)}
$codexArgs = {powershell_array(codex_args)}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($stdoutPath, "native Codex CLI window started for {name}`n", $utf8NoBom)
[System.IO.File]::AppendAllText($stdoutPath, ('prompt_path=' + $promptPath + "`n"), $utf8NoBom)
try {{
    $promptText = Get-Content -LiteralPath $promptPath -Raw
    & $codexCommand @codexArgs $promptText
    $exitCode = if ($null -eq $LASTEXITCODE) {{ 0 }} else {{ $LASTEXITCODE }}
    [System.IO.File]::AppendAllText($stdoutPath, ('codex_exit_code=' + $exitCode + "`n"), $utf8NoBom)
    exit $exitCode
}} catch {{
    [System.IO.File]::WriteAllText($stderrPath, ($_ | Out-String), $utf8NoBom)
    exit 1
}}
"""
        launcher_path.write_text(launcher, encoding="utf-8", newline="\n")
        command = [
            self.powershell_path,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher_path),
        ]
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as exc:
            stderr_path.write_text(str(exc), encoding="utf-8")
            return CommandResult.from_run(
                name=name,
                command=[*codex_base_command, f"@{prompt_path}"],
                cwd=ROOT,
                returncode=None,
                stdout="",
                stderr=str(exc),
                duration_seconds=time.monotonic() - started,
            )

        with stdout_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"launcher_pid={process.pid}\n")
            handle.write(f"completion_path={completion_path}\n")

        stable_since: float | None = None
        last_size = -1
        timed_out = False
        returncode: int | None = 0
        while True:
            elapsed = time.monotonic() - started
            if completion_path.exists() and completion_path.stat().st_size > 0:
                size = completion_path.stat().st_size
                if size == last_size:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 2:
                        break
                else:
                    last_size = size
                    stable_since = None

            polled = process.poll()
            if polled is not None:
                returncode = polled
                if not completion_path.exists() or completion_path.stat().st_size == 0:
                    break

            if elapsed >= timeout_seconds:
                timed_out = True
                returncode = None
                with stderr_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"\nTimed out after {timeout_seconds} seconds waiting for {completion_path}.\n")
                break
            time.sleep(1)

        stdout = read_text(stdout_path)
        stderr = read_text(stderr_path)
        if completion_path.exists() and completion_path.stat().st_size > 0 and not timed_out:
            returncode = 0
        return CommandResult.from_run(
            name=name,
            command=[*codex_base_command, f"@{prompt_path}"],
            cwd=ROOT,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )
class CommandResult(dict):
    @classmethod
    def from_run(
        cls,
        *,
        name: str,
        command: list[str],
        cwd: Path,
        returncode: int | None,
        stdout: str,
        stderr: str,
        duration_seconds: float,
        timed_out: bool = False,
    ) -> "CommandResult":
        return cls(
            name=name,
            command=command,
            cwd=str(cwd),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a true multi-agent Codex CLI worker loop for any repository."
    )
    parser.add_argument(
        "--task-prompt",
        required=True,
        help="Path to the task prompt for this worker loop.",
    )
    parser.add_argument(
        "--delegation-plan",
        default=None,
        help="Path to a supervisor-written delegation_plan.json. When supplied, the worker skips the manager agent.",
    )
    parser.add_argument("--repo-root", default=".", help="Repository root to operate on.")
    parser.add_argument("--dry-run", action="store_true", help="Write planned prompts without running agents or verification.")
    parser.add_argument("--skip-codex", action="store_true", help="Skip Codex agent invocations and run verification only.")
    parser.add_argument("--verification-only", action="store_true", help="Run verification once without agent invocations.")
    parser.add_argument(
        "--log-dir",
        default=str(HARNESS_ROOT / "runs" / "codex_loop"),
        help="Directory for loop logs.",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable.")
    parser.add_argument(
        "--allow-npx-codex",
        action="store_true",
        help="Allow npx --yes @openai/codex as a fallback Codex invocation.",
    )
    parser.add_argument(
        "--print-codex-discovery",
        action="store_true",
        help="Print every Codex CLI discovery candidate and its result.",
    )
    parser.add_argument("--live-monitor", action="store_true", help="Stream command output in this process while preserving logs.")
    parser.add_argument(
        "--monitor-backend",
        choices=["auto", "powershell-windows"],
        default=None,
        help="Native Codex CLI window backend. On Windows, auto opens real PowerShell-hosted Codex CLI windows for agents.",
    )
    parser.add_argument("--debug", action="store_true", help="Print full tracebacks for unexpected harness errors.")
    parser.add_argument("--max-iters", type=int, default=5, help="Maximum loop iterations.")
    parser.add_argument("--max-concurrent-agents", type=int, default=5, help="Maximum Codex agent subprocesses to run at once.")
    parser.add_argument("--max-agent-recursion-depth", type=int, default=2, help="Maximum nested child delegation plan depth.")
    parser.add_argument(
        "--continue-on-agent-failure",
        action="store_true",
        help="Continue the iteration even if an agent subprocess fails.",
    )
    parser.add_argument(
        "--stop-on-blocked",
        action="store_true",
        help="Stop immediately on BLOCKED instead of only stopping for external blockers.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per command or Codex call.")
    parser.add_argument(
        "--verification-command",
        action="append",
        default=[],
        help="Verification command to run after each iteration. Repeatable. Defaults to pytest when pytest config/tests are present.",
    )
    args = parser.parse_args()
    if args.max_iters < 1:
        parser.error("--max-iters must be at least 1")
    if args.max_concurrent_agents < 1:
        parser.error("--max-concurrent-agents must be at least 1")
    if args.max_agent_recursion_depth < 0:
        parser.error("--max-agent-recursion-depth must be at least 0")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    return args


def safe_rel_or_abs(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        cwd_path = Path.cwd() / path
        path = cwd_path if cwd_path.exists() else ROOT / path
    return path.resolve()


def set_repo_root(path_text: str) -> Path:
    global ROOT
    ROOT = safe_rel_or_abs(path_text)
    return ROOT


def ensure_prompt_available(task_prompt: Path) -> None:
    if not task_prompt.exists():
        raise FileNotFoundError(
            f"Task prompt not found: {task_prompt}. "
            "Create a prompt file or pass --task-prompt."
        )


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_stream(
    pipe: Any,
    *,
    name: str,
    stream_name: str,
    output: list[str],
    live: bool,
    output_path: Path | None = None,
) -> None:
    try:
        handle = output_path.open("a", encoding="utf-8", newline="\n") if output_path else None
        for line in iter(pipe.readline, ""):
            output.append(line)
            if handle:
                handle.write(line)
                handle.flush()
            if live:
                print(f"[{name}:{stream_name}] {line}", end="")
    finally:
        if "handle" in locals() and handle:
            handle.close()
        pipe.close()


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    name: str,
    live: bool = False,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> CommandResult:
    started = time.monotonic()
    if live or stdout_path is not None or stderr_path is not None:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        if stdout_path:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("", encoding="utf-8")
        if stderr_path:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_text("", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_thread = threading.Thread(
                target=_read_stream,
                kwargs={
                    "pipe": process.stdout,
                    "name": name,
                    "stream_name": "stdout",
                    "output": stdout_parts,
                    "live": live,
                    "output_path": stdout_path,
                },
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_read_stream,
                kwargs={
                    "pipe": process.stderr,
                    "name": name,
                    "stream_name": "stderr",
                    "output": stderr_parts,
                    "live": live,
                    "output_path": stderr_path,
                },
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                returncode = process.wait(timeout=timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = None
                timed_out = True
                timeout_message = f"\nTimed out after {timeout_seconds} seconds."
                stderr_parts.append(timeout_message)
                if stderr_path:
                    with stderr_path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(timeout_message)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            return CommandResult.from_run(
                name=name,
                command=command,
                cwd=cwd,
                returncode=returncode,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
            )
        except OSError as exc:
            return CommandResult.from_run(
                name=name,
                command=command,
                cwd=cwd,
                returncode=None,
                stdout="",
                stderr=str(exc),
                duration_seconds=time.monotonic() - started,
            )

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
        return CommandResult.from_run(
            name=name,
            command=command,
            cwd=cwd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult.from_run(
            name=name,
            command=command,
            cwd=cwd,
            returncode=None,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds.",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult.from_run(
            name=name,
            command=command,
            cwd=cwd,
            returncode=None,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )


def candidate_record(
    *,
    source: str,
    command_base: list[str],
    display: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "display": display or " ".join(command_base),
        "command_base": command_base,
        "reason": reason or "candidate generated",
    }


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        key = tuple(candidate["command_base"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def existing_file_candidates(paths: list[Path], source: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in paths:
        candidates.append(
            candidate_record(
                source=source,
                command_base=[str(path)],
                display=str(path),
                reason="common executable path exists" if path.exists() else "common executable path not found",
            )
        )
    return candidates


def path_lookup_candidates(names: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            candidates.append(
                candidate_record(
                    source="PATH lookup",
                    command_base=[resolved],
                    display=resolved,
                    reason=f"{name} resolved on PATH",
                )
            )
        else:
            candidates.append(
                candidate_record(
                    source="PATH lookup",
                    command_base=[name],
                    display=name,
                    reason=f"{name} did not resolve on PATH",
                )
            )
    return candidates


def common_npm_codex_paths() -> list[Path]:
    home = Path.home()
    system = platform.system().lower()
    paths: list[Path] = []
    if system == "windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm_dir = Path(appdata) / "npm"
            paths.extend([npm_dir / "codex.cmd", npm_dir / "codex.exe", npm_dir / "codex"])
        paths.extend(
            [
                home / "AppData" / "Roaming" / "npm" / "codex.cmd",
                home / "AppData" / "Roaming" / "npm" / "codex.exe",
                Path("C:/Program Files/nodejs/codex.cmd"),
                Path("C:/Program Files/nodejs/codex.exe"),
            ]
        )
    else:
        paths.extend(
            [
                Path("/usr/local/bin/codex"),
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/bin/codex"),
                home / ".npm-global" / "bin" / "codex",
                home / ".local" / "bin" / "codex",
            ]
        )
    return paths


def common_codex_native_paths() -> list[Path]:
    home = Path.home()
    system = platform.system().lower()
    if system != "windows":
        return []
    source_roots = []
    for root in (HARNESS_ROOT.parent, ROOT.parent, Path.cwd().parent):
        if root not in source_roots:
            source_roots.append(root)
    roots = [
        home / ".codex" / "codex-alpha" / "node_modules" / "@openai",
        home / ".codex" / "codex" / "node_modules" / "@openai",
    ]
    paths: list[Path] = []
    for root in source_roots:
        paths.extend(root.glob("codex*/codex-rs/target/debug/codex.exe"))
        paths.extend(root.glob("codex*/codex-rs/target/release/codex.exe"))
    for root in roots:
        paths.extend(root.glob("codex-win32-*/vendor/*/codex/codex.exe"))
    return paths


def npm_global_bin_candidates(timeout_seconds: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    npm = shutil.which("npm")
    if not npm:
        candidates.append(
            candidate_record(
                source="npm global bin",
                command_base=["npm", "bin", "-g"],
                reason="npm did not resolve on PATH",
            )
        )
        return candidates

    bin_result = run_command([npm, "bin", "-g"], cwd=ROOT, timeout_seconds=min(timeout_seconds, 30), name="npm_bin_global")
    bin_dir = (bin_result.get("stdout") or "").strip().splitlines()
    if bin_result["returncode"] == 0 and bin_dir:
        base = Path(bin_dir[-1])
        names = ["codex.cmd", "codex.exe", "codex"] if platform.system().lower() == "windows" else ["codex"]
        for name in names:
            candidates.append(
                candidate_record(
                    source="npm bin -g",
                    command_base=[str(base / name)],
                    display=str(base / name),
                    reason="resolved from npm bin -g",
                )
            )
    else:
        candidates.append(
            candidate_record(
                source="npm bin -g",
                command_base=[npm, "bin", "-g"],
                reason=f"npm bin -g failed: {(bin_result.get('stderr') or bin_result.get('stdout') or '').strip()}",
            )
        )

    prefix_result = run_command([npm, "prefix", "-g"], cwd=ROOT, timeout_seconds=min(timeout_seconds, 30), name="npm_prefix_global")
    prefixes = (prefix_result.get("stdout") or "").strip().splitlines()
    if prefix_result["returncode"] == 0 and prefixes:
        prefix = Path(prefixes[-1])
        if platform.system().lower() == "windows":
            names = ["codex.cmd", "codex.exe", "codex"]
            for name in names:
                candidates.append(
                    candidate_record(
                        source="npm prefix -g",
                        command_base=[str(prefix / name)],
                        display=str(prefix / name),
                        reason="resolved from npm prefix -g",
                    )
                )
        else:
            candidates.append(
                candidate_record(
                    source="npm prefix -g",
                    command_base=[str(prefix / "bin" / "codex")],
                    display=str(prefix / "bin" / "codex"),
                    reason="resolved from npm prefix -g",
                )
            )
    else:
        candidates.append(
            candidate_record(
                source="npm prefix -g",
                command_base=[npm, "prefix", "-g"],
                reason=f"npm prefix -g failed: {(prefix_result.get('stderr') or prefix_result.get('stdout') or '').strip()}",
            )
        )
    return candidates


def build_codex_candidates(codex_bin: str, timeout_seconds: int, allow_npx: bool) -> list[dict[str, Any]]:
    candidates = [
        candidate_record(
            source="configured --codex-bin",
            command_base=[codex_bin],
            display=codex_bin,
            reason="first configured CLI value",
        )
    ]
    candidates.extend(existing_file_candidates(common_codex_native_paths(), "common Codex native path"))
    candidates.extend(path_lookup_candidates(["codex", "codex.cmd", "codex.exe"]))
    candidates.extend(existing_file_candidates(common_npm_codex_paths(), "common npm global path"))
    candidates.extend(npm_global_bin_candidates(timeout_seconds))
    if allow_npx:
        npx = shutil.which("npx") or "npx"
        candidates.append(
            candidate_record(
                source="npx fallback",
                command_base=[npx, "--yes", "@openai/codex"],
                display=f"{npx} --yes @openai/codex",
                reason="allowed by --allow-npx-codex",
            )
        )
    else:
        candidates.append(
            candidate_record(
                source="npx fallback",
                command_base=["npx", "--yes", "@openai/codex"],
                display="npx --yes @openai/codex",
                reason="skipped unless --allow-npx-codex is set",
            )
        )
    return dedupe_candidates(candidates)


def detect_codex(codex_bin: str, timeout_seconds: int, allow_npx: bool) -> dict[str, Any]:
    candidates = build_codex_candidates(codex_bin, timeout_seconds, allow_npx)
    checked: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    version = CommandResult.from_run(
        name="codex_version",
        command=[codex_bin, "--version"],
        cwd=ROOT,
        returncode=None,
        stdout="",
        stderr="not run",
        duration_seconds=0.0,
    )

    for candidate in candidates:
        check = dict(candidate)
        if candidate["source"] == "npx fallback" and not allow_npx:
            check.update(
                {
                    "passed": False,
                    "failure": "npx fallback disabled; pass --allow-npx-codex to enable it",
                }
            )
            checked.append(check)
            continue
        command_base = candidate["command_base"]
        version = run_command(
            [*command_base, "--version"],
            cwd=ROOT,
            timeout_seconds=min(timeout_seconds, 30),
            name="codex_version",
        )
        version_text = ((version.get("stdout") or "") + "\n" + (version.get("stderr") or "")).strip()
        check.update(
            {
                "version_command": [*command_base, "--version"],
                "returncode": version["returncode"],
                "timed_out": version["timed_out"],
                "stdout": version["stdout"],
                "stderr": version["stderr"],
                "passed": version["returncode"] == 0,
                "failure": None if version["returncode"] == 0 else "version command failed",
            }
        )
        checked.append(check)
        if version["returncode"] == 0:
            selected = candidate
            break

    available = selected is not None
    exec_help = CommandResult.from_run(
        name="codex_exec_help",
        command=[*(selected["command_base"] if selected else [codex_bin]), "exec", "--help"],
        cwd=ROOT,
        returncode=None,
        stdout="",
        stderr="not run",
        duration_seconds=0.0,
    )
    if available:
        exec_help = run_command(
            [*selected["command_base"], "exec", "--help"],
            cwd=ROOT,
            timeout_seconds=min(timeout_seconds, 30),
            name="codex_exec_help",
        )
    interactive_help = CommandResult.from_run(
        name="codex_help",
        command=[*(selected["command_base"] if selected else [codex_bin]), "--help"],
        cwd=ROOT,
        returncode=None,
        stdout="",
        stderr="not run",
        duration_seconds=0.0,
    )
    if available:
        interactive_help = run_command(
            [*selected["command_base"], "--help"],
            cwd=ROOT,
            timeout_seconds=min(timeout_seconds, 30),
            name="codex_help",
        )
    help_text = (exec_help.get("stdout") or "") + "\n" + (exec_help.get("stderr") or "")
    interactive_help_text = (interactive_help.get("stdout") or "") + "\n" + (interactive_help.get("stderr") or "")
    version_text = ((version.get("stdout") or "") + "\n" + (version.get("stderr") or "")).strip()
    exec_available = available and exec_help["returncode"] == 0
    command_base = selected["command_base"] if selected else None
    invocation_form = "exec subcommand" if exec_available else ("direct prompt argument" if available else None)
    selected_invocation = [*command_base, "exec"] if command_base and exec_available else command_base
    return {
        "available": available,
        "path": command_base[0] if command_base else None,
        "selected": selected,
        "command_base": command_base,
        "selected_invocation": selected_invocation,
        "version_text": version_text,
        "version": version,
        "exec_available": exec_available,
        "invocation_form": invocation_form,
        "exec_help": exec_help,
        "exec_help_text": help_text,
        "interactive_help": interactive_help,
        "interactive_help_text": interactive_help_text,
        "candidates": checked,
    }


def interactive_codex_base_command(codex_info: dict[str, Any], model: str | None = None) -> list[str]:
    command_base = codex_info.get("command_base")
    if not command_base:
        raise RuntimeError("Codex command requested before successful Codex discovery.")
    help_text = codex_info.get("interactive_help_text", "")
    command = [*command_base]
    if model and "--model" in help_text:
        command.extend(["--model", model])
    if "--cd" in help_text:
        command.extend(["--cd", str(ROOT)])
    if "--ask-for-approval" in help_text:
        command.extend(["--ask-for-approval", "never"])
    if "--sandbox" in help_text:
        command.extend(["--sandbox", "danger-full-access"])
    if "--no-alt-screen" in help_text:
        command.append("--no-alt-screen")
    if "--disable" in help_text:
        command.extend(["--disable", "shell_snapshot"])
    return command


def interactive_codex_command(prompt: str, codex_info: dict[str, Any], model: str | None = None) -> list[str]:
    return [*interactive_codex_base_command(codex_info, model=model), prompt]


def codex_command(
    prompt: str,
    codex_info: dict[str, Any],
    output_path: Path | None = None,
    model: str | None = None,
) -> list[str]:
    command_base = codex_info.get("command_base")
    if not command_base:
        raise RuntimeError("Codex command requested before successful Codex discovery.")
    if not codex_info.get("exec_available"):
        return [*command_base, prompt]

    help_text = codex_info.get("exec_help_text", "")
    command = [*command_base, "exec"]
    if model and "--model" in help_text:
        command.extend(["--model", model])
    if "--cd" in help_text:
        command.extend(["--cd", str(ROOT)])
    if "--skip-git-repo-check" in help_text:
        command.append("--skip-git-repo-check")
    if "--approval-policy" in help_text:
        command.extend(["--approval-policy", "never"])
    elif "--ask-for-approval" in help_text:
        command.extend(["--ask-for-approval", "never"])
    if "--sandbox" in help_text:
        command.extend(["--sandbox", "danger-full-access"])
    elif "--sandbox-mode" in help_text:
        command.extend(["--sandbox-mode", "danger-full-access"])
    if output_path is not None and "--output-last-message" in help_text:
        command.extend(["--output-last-message", str(output_path)])
    command.append(prompt)
    return command


def split_command(command_text: str) -> list[str]:
    return shlex.split(command_text, posix=platform.system().lower() != "windows")


def verification_commands(configured: list[str] | None = None) -> list[tuple[str, list[str]]]:
    configured = configured or []
    if configured:
        return [(f"verify_{index:02d}", split_command(command)) for index, command in enumerate(configured, start=1)]
    if (ROOT / "pyproject.toml").exists() or (ROOT / "pytest.ini").exists() or (ROOT / "tests").exists():
        return [("pytest", ["pytest"])]
    return []


def run_verification(
    iter_dir: Path,
    timeout_seconds: int,
    dry_run: bool,
    live: bool = False,
    configured_commands: list[str] | None = None,
) -> list[CommandResult]:
    results: list[CommandResult] = []
    (ROOT / "runs").mkdir(parents=True, exist_ok=True)
    for name, command in verification_commands(configured_commands):
        print(f"verification: {' '.join(command)}")
        stdout_path = iter_dir / "verification" / f"{name}.stdout.txt"
        stderr_path = iter_dir / "verification" / f"{name}.stderr.txt"
        if dry_run:
            result = CommandResult.from_run(
                name=name,
                command=command,
                cwd=ROOT,
                returncode=0,
                stdout="DRY RUN: command not executed.\n",
                stderr="",
                duration_seconds=0.0,
            )
        else:
            result = run_command(
                command,
                cwd=ROOT,
                timeout_seconds=timeout_seconds,
                name=name,
                live=live,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        results.append(result)
        write_text(stdout_path, result["stdout"])
        write_text(stderr_path, result["stderr"])
    return results


def agent_template(agent: str) -> Path:
    role = agent if agent in BUILTIN_AGENT_ROLES else "generic"
    return HARNESS_ROOT / "tools" / "prompts" / "agents" / f"{role}.md"


def default_delegation_plan(iter_dir: Path) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for agent in WORKER_AGENT_ORDER:
        agents[agent] = {
            "model": None,
            "task": f"Perform the {agent} role for this iteration.",
            "context": ["task prompt", "previous agent outputs"],
            "deliverable": f"{agent}.md",
            "depends_on": [] if agent == "planner" else [WORKER_AGENT_ORDER[WORKER_AGENT_ORDER.index(agent) - 1]],
            "verification_focus": "configured verification commands",
        }
    return {
        "manager": {
            "summary": "Default delegation plan generated by harness fallback.",
            "assumptions": [],
            "stop_conditions": [],
        },
        "agents": agents,
    }


def load_delegation_plan(plan_path: Path) -> dict[str, Any]:
    if not plan_path.exists() or plan_path.stat().st_size == 0:
        return default_delegation_plan(plan_path.parent)
    try:
        parsed = json.loads(read_text(plan_path).lstrip("\ufeff"))
    except json.JSONDecodeError:
        return default_delegation_plan(plan_path.parent)
    if not isinstance(parsed, dict):
        return default_delegation_plan(plan_path.parent)
    agents = parsed.get("agents")
    if not isinstance(agents, dict):
        parsed["agents"] = default_delegation_plan(plan_path.parent)["agents"]
    return parsed


def model_for_agent(agent: str, plan_path: Path) -> str | None:
    if agent == "manager":
        return None
    plan = load_delegation_plan(plan_path)
    item = (plan.get("agents") or {}).get(agent)
    if not isinstance(item, dict):
        return None
    model = item.get("model")
    if not isinstance(model, str):
        return None
    model = model.strip()
    return model or None


def agent_assignments(plan_path: Path) -> dict[str, Any]:
    plan = load_delegation_plan(plan_path)
    agents = plan.get("agents")
    return agents if isinstance(agents, dict) else {}


def agent_dependencies(agent: str, assignments: dict[str, Any]) -> list[str]:
    item = assignments.get(agent)
    if not isinstance(item, dict):
        return []
    deps = item.get("depends_on")
    if not isinstance(deps, list):
        return []
    names: list[str] = []
    for dep in deps:
        if isinstance(dep, str) and dep.strip():
            names.append(dep.strip())
    return names


def child_plan_path_for(agent: str, iter_dir: Path) -> Path:
    return iter_dir / f"{safe_agent_stem(agent)}.children.json"


def agent_output_path_for(agent: str, iter_dir: Path) -> Path:
    return iter_dir / f"{safe_agent_stem(agent)}.md"


def compose_agent_prompt(
    *,
    agent: str,
    task_prompt: Path,
    iter_dir: Path,
    previous_summary: Path | None,
    agent_output: Path,
    child_delegation_plan: Path,
    delegation_plan: Path,
    dry_run: bool,
) -> str:
    template_path = agent_template(agent)
    template = read_text(template_path)
    task_text = read_text(task_prompt)
    delegation_text = read_text(delegation_plan, "Delegation plan: not written yet.")
    assignments = agent_assignments(delegation_plan)
    assignment = assignments.get(agent, {})
    assignment_text = json.dumps(assignment, indent=2) if isinstance(assignment, dict) else "{}"
    previous_text = f"Previous iteration summary: {previous_summary}" if previous_summary else "Previous iteration summary: none"
    dry_run_note = "This is a dry run; describe what would be done without changing files." if dry_run else ""
    manager_note = (
        f"Required delegation plan path: {delegation_plan}\n"
        if agent == "manager"
        else (
            f"Delegation plan path: {delegation_plan}\n"
            "Follow your assignment in the delegation plan. If it conflicts with the task prompt, prefer the task prompt and note the conflict.\n"
        )
    )
    return f"""You are the {agent} agent in a true multi-agent Codex CLI loop.

Repository root: {ROOT}
Task prompt: {task_prompt}
Iteration log directory: {iter_dir}
Required output path: {agent_output}
Optional child delegation plan path: {child_delegation_plan}
{manager_note}
{previous_text}
{dry_run_note}

Read the task prompt and the template instructions below. Write your final role output to the required output path.

If your assignment is too large or naturally parallel, write a child delegation JSON file to the optional child delegation plan path. Use the same JSON shape as the main delegation plan, with an "agents" object whose agent names are local to this child plan and whose dependencies are expressed in "depends_on". The harness will run that child graph recursively after your output is written. Do not use child agents for work you can finish directly.

--- Task prompt: {task_prompt} ---
{task_text}

--- Your assignment from the delegation plan ---
```json
{assignment_text}
```

--- Current delegation plan: {delegation_plan} ---
{delegation_text}

--- Agent template: {template_path} ---
{template}
"""


def run_agent(
    *,
    agent: str,
    args: argparse.Namespace,
    task_prompt: Path,
    iter_dir: Path,
    previous_summary: Path | None,
    delegation_plan: Path,
    codex_info: dict[str, Any],
    window_runner: CodexWindowRunner | None = None,
) -> CommandResult:
    agent_output = agent_output_path_for(agent, iter_dir)
    child_delegation_plan = child_plan_path_for(agent, iter_dir)
    if child_delegation_plan.exists():
        child_delegation_plan.unlink()
    prompt = compose_agent_prompt(
        agent=agent,
        task_prompt=task_prompt,
        iter_dir=iter_dir,
        previous_summary=previous_summary,
        agent_output=agent_output,
        child_delegation_plan=child_delegation_plan,
        delegation_plan=delegation_plan,
        dry_run=args.dry_run,
    )
    prompt_path = iter_dir / f"{safe_agent_stem(agent)}.prompt.txt"
    write_text(prompt_path, prompt)

    if args.dry_run or args.skip_codex or args.verification_only:
        dry_command = [*(codex_info.get("selected_invocation") or codex_info.get("command_base") or ["codex", "exec"]), f"@{prompt_path}"]
        if agent == "manager" and not delegation_plan.exists():
            write_text(delegation_plan, json.dumps(default_delegation_plan(iter_dir), indent=2))
        write_text(agent_output, f"# {agent.title()} Agent\n\nSkipped by harness mode.\n")
        return CommandResult.from_run(
            name=agent,
            command=dry_command,
            cwd=ROOT,
            returncode=0,
            stdout=f"Skipped {agent} agent by harness mode.\n",
            stderr="",
            duration_seconds=0.0,
        )

    stdout_path = iter_dir / f"{safe_agent_stem(agent)}.stdout.txt"
    stderr_path = iter_dir / f"{safe_agent_stem(agent)}.stderr.txt"
    selected_model = model_for_agent(agent, delegation_plan)
    if window_runner is not None:
        command = interactive_codex_base_command(codex_info, model=selected_model)
        model_note = f" model={selected_model}" if selected_model else ""
        print(f"agent {agent}: opening visible Codex CLI window with prompt={prompt_path}{model_note}")
        result = window_runner.run_codex_window(
            name=safe_agent_stem(agent),
            codex_base_command=command,
            prompt_path=prompt_path,
            completion_path=agent_output,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        command = codex_command(f"Read and follow this prompt file: {prompt_path}", codex_info, agent_output, model=selected_model)
        print(f"agent {agent}: {' '.join(command[:-1])} <prompt-file>")
        result = run_command(
            command,
            cwd=ROOT,
            timeout_seconds=args.timeout_seconds,
            name=agent,
            live=args.live_monitor,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    write_text(stdout_path, result["stdout"])
    write_text(stderr_path, result["stderr"])
    if not agent_output.exists():
        fallback = (
            f"# {agent.title()} Agent Output\n\n"
            "The agent did not create the required output file. Captured stdout follows.\n\n"
            "```text\n"
            f"{result['stdout']}\n"
            "```\n\n"
            "Captured stderr:\n\n"
            "```text\n"
            f"{result['stderr']}\n"
            "```\n"
        )
        write_text(agent_output, fallback)
    return result


def command_result_failed(result: CommandResult) -> bool:
    return result["returncode"] not in (0, None) or bool(result["timed_out"])


def dependency_blocked_result(agent: str, deps: list[str], iter_dir: Path) -> CommandResult:
    output = agent_output_path_for(agent, iter_dir)
    missing = ", ".join(deps) if deps else "unknown dependency cycle"
    text = (
        f"# {agent.title()} Agent\n\n"
        f"Skipped because dependencies were not satisfied: {missing}.\n"
    )
    write_text(output, text)
    return CommandResult.from_run(
        name=agent,
        command=["dependency-scheduler"],
        cwd=ROOT,
        returncode=1,
        stdout="",
        stderr=f"Dependencies not satisfied for {agent}: {missing}\n",
        duration_seconds=0.0,
    )


def run_agent_tree(
    *,
    agent: str,
    args: argparse.Namespace,
    task_prompt: Path,
    iter_dir: Path,
    previous_summary: Path | None,
    delegation_plan: Path,
    codex_info: dict[str, Any],
    window_runner: CodexWindowRunner | None,
    depth: int,
) -> list[CommandResult]:
    result = run_agent(
        agent=agent,
        args=args,
        task_prompt=task_prompt,
        iter_dir=iter_dir,
        previous_summary=previous_summary,
        delegation_plan=delegation_plan,
        codex_info=codex_info,
        window_runner=window_runner,
    )
    results = [result]
    child_plan = child_plan_path_for(agent, iter_dir)
    if (
        depth < args.max_agent_recursion_depth
        and child_plan.exists()
        and child_plan.stat().st_size > 0
        and not command_result_failed(result)
    ):
        child_dir = iter_dir / f"{safe_agent_stem(agent)}_children"
        child_dir.mkdir(parents=True, exist_ok=True)
        print(f"agent {agent}: running child delegation graph {child_plan}")
        results.extend(
            run_delegation_graph(
                args=args,
                task_prompt=task_prompt,
                iter_dir=child_dir,
                previous_summary=previous_summary,
                delegation_plan=child_plan,
                codex_info=codex_info,
                window_runner=window_runner,
                depth=depth + 1,
            )
        )
    return results


def run_delegation_graph(
    *,
    args: argparse.Namespace,
    task_prompt: Path,
    iter_dir: Path,
    previous_summary: Path | None,
    delegation_plan: Path,
    codex_info: dict[str, Any],
    window_runner: CodexWindowRunner | None,
    depth: int = 0,
) -> list[CommandResult]:
    assignments = agent_assignments(delegation_plan)
    if not assignments:
        return []

    ordered_agents = list(assignments.keys())
    pending = set(ordered_agents)
    completed: set[str] = set()
    failed: set[str] = set()
    running: dict[concurrent.futures.Future[list[CommandResult]], str] = {}
    results: list[CommandResult] = []
    max_workers = max(1, args.max_concurrent_agents)
    trace_path = iter_dir / "scheduler_trace.log"
    write_text(
        trace_path,
        "scheduler_start "
        + json.dumps(
            {
                "depth": depth,
                "max_workers": max_workers,
                "agents": ordered_agents,
                "dependencies": {agent: agent_dependencies(agent, assignments) for agent in ordered_agents},
            },
            indent=2,
        )
        + "\n",
    )

    def ready_agents() -> list[str]:
        ready: list[str] = []
        for candidate in ordered_agents:
            if candidate not in pending:
                continue
            deps = [dep for dep in agent_dependencies(candidate, assignments) if dep in assignments]
            if all(dep in completed or (args.continue_on_agent_failure and dep in failed) for dep in deps):
                ready.append(candidate)
        return ready

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while pending or running:
            capacity = max_workers - len(running)
            ready_now = ready_agents()
            with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event": "scheduler_tick",
                            "pending": sorted(pending),
                            "running": list(running.values()),
                            "completed": sorted(completed),
                            "failed": sorted(failed),
                            "ready": ready_now,
                            "capacity": capacity,
                        }
                    )
                    + "\n"
                )
            for agent in ready_now[:capacity]:
                pending.remove(agent)
                print(f"agent {agent}: launching (depth={depth}, delegation_plan={delegation_plan})", flush=True)
                future = executor.submit(
                    run_agent_tree,
                    agent=agent,
                    args=args,
                    task_prompt=task_prompt,
                    iter_dir=iter_dir,
                    previous_summary=previous_summary,
                    delegation_plan=delegation_plan,
                    codex_info=codex_info,
                    window_runner=window_runner,
                    depth=depth,
                )
                running[future] = agent

            if not running:
                for agent in list(pending):
                    deps = [dep for dep in agent_dependencies(agent, assignments) if dep in assignments and dep not in completed]
                    results.append(dependency_blocked_result(agent, deps, iter_dir))
                    failed.add(agent)
                    pending.remove(agent)
                break

            done, _ = concurrent.futures.wait(running.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                agent = running.pop(future)
                try:
                    agent_results = future.result()
                except Exception as exc:
                    agent_results = [
                        CommandResult.from_run(
                            name=agent,
                            command=["agent-thread"],
                            cwd=ROOT,
                            returncode=1,
                            stdout="",
                            stderr=str(exc),
                            duration_seconds=0.0,
                        )
                    ]
                results.extend(agent_results)
                if any(command_result_failed(item) for item in agent_results):
                    failed.add(agent)
                    if args.continue_on_agent_failure:
                        completed.add(agent)
                else:
                    completed.add(agent)

    order = {name: index for index, name in enumerate(ordered_agents)}
    results.sort(key=lambda item: order.get(str(item.get("name")), len(order)))
    return results


def extract_controller_verdict(controller_text: str) -> str | None:
    upper = controller_text.upper()
    explicit_match = re.search(
        r"\bFINAL\s+VERDICT\s*:\s*`?(COMPLETE|PARTIAL|BLOCKED|CONTINUE)`?\b",
        upper,
    )
    if explicit_match:
        return explicit_match.group(1)

    line_matches = re.findall(
        r"^\s*(COMPLETE|PARTIAL|BLOCKED|CONTINUE)\s*$",
        upper,
        flags=re.MULTILINE,
    )
    if line_matches:
        return line_matches[-1]
    return None


def external_blocker_seen(results: list[CommandResult], controller_text: str) -> bool:
    text = controller_text + "\n" + "\n".join((r.get("stdout") or "") + "\n" + (r.get("stderr") or "") for r in results)
    text = text.lower()
    blockers = [
        "codex cli is unavailable",
        "not logged in",
        "authentication required",
        "auth required",
        "credentials",
        "license required",
        "license acceptance",
        "permission denied",
        "access denied",
        "account access",
    ]
    return any(blocker in text for blocker in blockers)


def command_ok(results_by_name: dict[str, CommandResult], name: str) -> bool:
    result = results_by_name.get(name)
    return bool(result and result.get("returncode") == 0 and not result.get("timed_out"))


def classify_iteration(
    *,
    verification_results: list[CommandResult],
    controller_text: str,
    codex_available: bool,
    agents_ran: bool,
) -> str:
    if not codex_available and agents_ran:
        return "BLOCKED"

    results_by_name = {r["name"]: r for r in verification_results}
    verification_ok = all(command_ok(results_by_name, name) for name in results_by_name)
    controller_verdict = extract_controller_verdict(controller_text)
    if verification_ok and controller_verdict == "COMPLETE":
        return "COMPLETE"
    if controller_verdict == "BLOCKED" and external_blocker_seen(verification_results, controller_text):
        return "BLOCKED"
    if external_blocker_seen(verification_results, controller_text):
        return "BLOCKED"
    if controller_verdict in ("PARTIAL", "CONTINUE"):
        return controller_verdict
    if verification_ok and agents_ran:
        return "PARTIAL"
    return "CONTINUE"


def markdown_report(
    *,
    iteration: int,
    verdict: str,
    agent_results: list[CommandResult],
    verification_results: list[CommandResult],
    codex_info: dict[str, Any],
    mode: str,
) -> str:
    lines = [
        f"# Iteration {iteration}",
        "",
        f"- mode: {mode}",
        f"- verdict: {verdict}",
        f"- codex_available: {codex_info.get('available')}",
        f"- codex_exec_available: {codex_info.get('exec_available')}",
        "",
        "## Agents",
    ]
    for result in agent_results:
        lines.append(f"- {result['name']}: returncode={result['returncode']} timed_out={result['timed_out']}")
    lines.extend(["", "## Verification"])
    for result in verification_results:
        command = " ".join(result["command"])
        lines.append(f"- {result['name']}: returncode={result['returncode']} timed_out={result['timed_out']} command=`{command}`")
    return "\n".join(lines) + "\n"


def mode_name(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "dry-run"
    if args.verification_only:
        return "verification-only"
    if args.skip_codex:
        return "skip-codex"
    return "loop"


def print_codex_preflight(codex_info: dict[str, Any]) -> None:
    if codex_info["available"]:
        path = codex_info.get("path") or "<resolved by subprocess PATH>"
        version = codex_info.get("version_text") or "<version output unavailable>"
        invocation = codex_info.get("invocation_form") or "unknown"
        command_form = " ".join(codex_info.get("selected_invocation") or [])
        print(f"Codex CLI path: {path}")
        print(f"Codex CLI version: {version}")
        print(f"Codex exec available: {codex_info.get('exec_available')}")
        print(f"Codex CLI invocation: {invocation}")
        print(f"Codex CLI command form: {command_form} <prompt>")
        return

    print("Codex CLI unavailable.")
    print(CODEX_REMEDIATION.strip())


def print_codex_discovery(codex_info: dict[str, Any]) -> None:
    print("Codex discovery candidates:")
    for index, candidate in enumerate(codex_info.get("candidates", []), start=1):
        status = "PASS" if candidate.get("passed") else "FAIL"
        display = candidate.get("display") or " ".join(candidate.get("command_base") or [])
        reason = candidate.get("reason") or ""
        failure = candidate.get("failure")
        detail = f"{reason}; {failure}" if failure else reason
        print(f"{index}. [{status}] {candidate.get('source')}: {display} ({detail})")


def write_blocked_preflight(
    *,
    log_dir: Path,
    codex_info: dict[str, Any],
    mode: str,
) -> None:
    blocked_dir = log_dir / "iteration_001"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    controller_text = (
        "FINAL VERDICT: BLOCKED\n\n"
        "Codex CLI is unavailable. The harness does not inspect auth files or implement auth.\n\n"
        f"{CODEX_REMEDIATION}"
    )
    write_text(blocked_dir / "controller.md", controller_text)
    summary = {
        "iteration": 1,
        "mode": mode,
        "verdict": "BLOCKED",
        "reason": "Codex CLI is unavailable or not executable.",
        "remediation": [
            "npm install -g @openai/codex",
            "codex login",
        ],
        "codex": codex_info,
    }
    write_text(blocked_dir / "summary.json", json.dumps(summary, indent=2))
    write_text(
        blocked_dir / "report.md",
        markdown_report(
            iteration=1,
            verdict="BLOCKED",
            agent_results=[],
            verification_results=[],
            codex_info=codex_info,
            mode=mode,
        )
        + "\n## Candidates Checked\n\n"
        + "\n".join(
            f"- [{'PASS' if c.get('passed') else 'FAIL'}] {c.get('source')}: {c.get('display')} - {c.get('failure') or c.get('reason')}"
            for c in codex_info.get("candidates", [])
        )
        + "\n\n## Remediation\n\n```powershell\nnpm install -g @openai/codex\ncodex login\n```\n",
    )


def main() -> int:
    args = parse_args()
    set_repo_root(args.repo_root)
    task_prompt = safe_rel_or_abs(args.task_prompt)
    supplied_delegation_plan = safe_rel_or_abs(args.delegation_plan) if args.delegation_plan else None
    log_dir = safe_rel_or_abs(args.log_dir)
    ensure_prompt_available(task_prompt)
    if supplied_delegation_plan is not None and not supplied_delegation_plan.exists():
        raise FileNotFoundError(f"Delegation plan not found: {supplied_delegation_plan}")
    log_dir.mkdir(parents=True, exist_ok=True)

    codex_info = detect_codex(args.codex_bin, args.timeout_seconds, args.allow_npx_codex)
    write_text(log_dir / "codex_detection.json", json.dumps(codex_info, indent=2))
    if args.print_codex_discovery:
        print_codex_discovery(codex_info)
    print_codex_preflight(codex_info)

    if not codex_info["available"]:
        write_blocked_preflight(log_dir=log_dir, codex_info=codex_info, mode=mode_name(args))
        print("Final verdict: BLOCKED")
        return 2

    window_runner: CodexWindowRunner | None = None
    monitor_backend = args.monitor_backend
    if monitor_backend:
        window_runner = CodexWindowRunner(backend=monitor_backend)
        window_runner.ensure()
        print(f"window runner: {window_runner.attach_instructions()}")

    last_summary: Path | None = None
    final_verdict = "CONTINUE"
    iterations_to_run = 1 if args.verification_only else args.max_iters

    for iteration in range(1, iterations_to_run + 1):
        iter_dir = log_dir / f"iteration_{iteration:03d}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        delegation_plan = supplied_delegation_plan or (iter_dir / "delegation_plan.json")
        print(f"iteration {iteration}: logs={iter_dir}")

        agent_results: list[CommandResult] = []
        agents_ran = not (args.dry_run or args.skip_codex or args.verification_only)
        if args.verification_only:
            agent_results = []
        else:
            if supplied_delegation_plan is None:
                manager_result = run_agent(
                    agent="manager",
                    args=args,
                    task_prompt=task_prompt,
                    iter_dir=iter_dir,
                    previous_summary=last_summary,
                    delegation_plan=delegation_plan,
                    codex_info=codex_info,
                    window_runner=window_runner,
                )
                agent_results.append(manager_result)
                if not delegation_plan.exists() or delegation_plan.stat().st_size == 0:
                    write_text(delegation_plan, json.dumps(default_delegation_plan(iter_dir), indent=2))
                if command_result_failed(manager_result) and not args.continue_on_agent_failure:
                    print("manager failed; worker graph not launched")
                else:
                    agent_results.extend(
                        run_delegation_graph(
                            args=args,
                            task_prompt=task_prompt,
                            iter_dir=iter_dir,
                            previous_summary=last_summary,
                            delegation_plan=delegation_plan,
                            codex_info=codex_info,
                            window_runner=window_runner,
                        )
                    )
            else:
                agent_results = run_delegation_graph(
                    args=args,
                    task_prompt=task_prompt,
                    iter_dir=iter_dir,
                    previous_summary=last_summary,
                    delegation_plan=delegation_plan,
                    codex_info=codex_info,
                    window_runner=window_runner,
                )

        verification_results = run_verification(
            iter_dir,
            args.timeout_seconds,
            dry_run=args.dry_run,
            live=args.live_monitor,
            configured_commands=args.verification_command,
        )
        controller_text = read_text(iter_dir / "controller.md")
        final_verdict = classify_iteration(
            verification_results=verification_results,
            controller_text=controller_text,
            codex_available=codex_info["available"],
            agents_ran=agents_ran,
        )

        summary = {
            "iteration": iteration,
            "mode": mode_name(args),
            "verdict": final_verdict,
            "task_prompt": str(task_prompt),
            "agent_results": agent_results,
            "verification_results": verification_results,
            "codex": {
                "available": codex_info["available"],
                "path": codex_info["path"],
                "command_base": codex_info["command_base"],
                "selected_invocation": codex_info["selected_invocation"],
                "exec_available": codex_info["exec_available"],
                "invocation_form": codex_info["invocation_form"],
                "version_returncode": codex_info["version"]["returncode"],
                "version_stdout": codex_info["version"]["stdout"],
                "version_stderr": codex_info["version"]["stderr"],
            },
        }
        summary_path = iter_dir / "summary.json"
        write_text(summary_path, json.dumps(summary, indent=2))
        write_text(
            iter_dir / "report.md",
            markdown_report(
                iteration=iteration,
                verdict=final_verdict,
                agent_results=agent_results,
                verification_results=verification_results,
                codex_info=codex_info,
                mode=mode_name(args),
            ),
        )
        last_summary = summary_path
        print(f"iteration {iteration}: verdict={final_verdict}")

        if final_verdict in ("COMPLETE", "PARTIAL"):
            break
        if final_verdict == "BLOCKED" and (args.stop_on_blocked or external_blocker_seen(verification_results, controller_text)):
            break

    if final_verdict == "CONTINUE" and iterations_to_run >= args.max_iters and not args.verification_only:
        print("Reached max iterations with CONTINUE-level issues remaining.")
    print(f"Final verdict: {final_verdict}")
    return 0 if final_verdict in ("COMPLETE", "PARTIAL", "CONTINUE") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        if "--debug" in sys.argv:
            traceback.print_exc()
        else:
            print(f"Harness error: {exc}", file=sys.stderr)
        raise SystemExit(1)


