from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import traceback
from typing import Any

import codex_multiagent_loop as worker


HARNESS_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path.cwd()
SUPERVISOR_REMEDIATION = worker.CODEX_REMEDIATION
VERDICTS = ("COMPLETE", "BLOCKED", "CONTINUE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Codex supervisor over the worker multi-agent harness.")
    parser.add_argument("--goal", required=True, help="Top-level milestone for the supervisor to pursue.")
    parser.add_argument("--repo-root", default=".", help="Repository root to supervise.")
    parser.add_argument(
        "--log-dir",
        default=str(HARNESS_ROOT / "runs" / "codex_supervisor"),
        help="Supervisor log directory.",
    )
    parser.add_argument("--harness", default=str(HARNESS_ROOT / "tools" / "codex_multiagent_loop.py"), help="Worker harness script path.")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable.")
    parser.add_argument("--dry-run", action="store_true", help="Write supervisor prompts without running Codex or the worker harness.")
    parser.add_argument("--print-codex-discovery", action="store_true", help="Print Codex discovery candidates.")
    parser.add_argument("--live-monitor", action="store_true", help="Stream command output in this process while preserving logs.")
    parser.add_argument(
        "--monitor-backend",
        choices=["auto", "powershell-windows"],
        default=None,
        help="Native Codex CLI window backend. On Windows, auto opens real PowerShell-hosted Codex CLI windows for supervisor and worker agents.",
    )
    parser.add_argument("--allow-npx-codex", action="store_true", help="Allow npx --yes @openai/codex as a fallback.")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Timeout per supervisor or verification command.")
    parser.add_argument(
        "--verification-command",
        action="append",
        default=[],
        help="Verification command to pass to the worker harness and final supervisor verification. Repeatable.",
    )
    parser.add_argument(
        "--success-artifact",
        action="append",
        default=[],
        help="Artifact path that must exist and be nonzero for supervisor COMPLETE. Repeatable.",
    )
    parser.add_argument("--debug", action="store_true", help="Print tracebacks for unexpected supervisor errors.")
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    return args


def rel_or_abs(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def set_repo_root(path_text: str) -> Path:
    global ROOT
    ROOT = rel_or_abs(path_text)
    worker.set_repo_root(str(ROOT))
    return ROOT


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def latest_files(root: Path, pattern: str, limit: int = 5) -> list[Path]:
    if not root.exists():
        return []
    files = sorted(root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def collect_repo_state(cycle_dir: Path) -> Path:
    sections: list[str] = ["# Supervisor Repo State\n"]
    for name in ("README.md", "AGENTS.md", "WORKLOG.md", "REAL_VS_MOCKED.md", "ROADMAP.md"):
        path = ROOT / name
        if path.exists():
            sections.append(f"\n## {name}\n\n```markdown\n{read_text(path)}\n```\n")
        else:
            sections.append(f"\n## {name}\n\nNot present.\n")

    sections.append("\n## Latest Worker Reports\n")
    for base in (ROOT / "runs" / "codex_loop", HARNESS_ROOT / "runs" / "codex_loop", HARNESS_ROOT / "runs" / "codex_supervisor"):
        for path in latest_files(base, "report.md", limit=5):
            label = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            sections.append(f"\n### {label}\n\n```markdown\n{read_text(path)}\n```\n")

    sections.append("\n## Latest Worker Summaries\n")
    for base in (ROOT / "runs" / "codex_loop", HARNESS_ROOT / "runs" / "codex_loop", HARNESS_ROOT / "runs" / "codex_supervisor"):
        for path in latest_files(base, "summary.json", limit=5):
            label = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            sections.append(f"\n### {label}\n\n```json\n{read_text(path)}\n```\n")

    sections.append("\n## Latest Failure Logs\n")
    for base in (ROOT / "runs", HARNESS_ROOT / "runs"):
        for path in latest_files(base, "*.stderr.txt", limit=5):
            label = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            sections.append(f"\n### {label}\n\n```text\n{read_text(path)}\n```\n")

    output = cycle_dir / "repo_state.md"
    write_text(output, "\n".join(sections))
    return output


def default_task_prompt(goal: str, cycle: int) -> str:
    return f"""# Supervisor Generated Task - Cycle {cycle:03d}

Top-level goal: {goal}

Implement the next bounded, verifiable step toward the top-level goal.

Task:
Read the repository state, previous logs, and project docs. Make a small code/docs/test change that advances the goal. If the next step requires local dependency installation, build setup, service startup, launch-command configuration, or generated artifacts, assign that work to the agents. Stop only for authentication, credentials, license acceptance, account access, or permissions the agent cannot grant.

Verification:
- Run the verification commands configured by the supervisor.
- If no verification commands are configured, run the repository's default test command.

Truthfulness:
- Do not claim success unless verification commands and required artifacts prove it.
- Update WORKLOG.md, REAL_VS_MOCKED.md, and ROADMAP.md if they exist and the task changes their content.
"""


def compose_supervisor_prompt(
    args: argparse.Namespace,
    cycle: int,
    cycle_dir: Path,
    repo_state: Path,
    task_prompt: Path,
    delegation_plan: Path,
) -> str:
    template = read_text(HARNESS_ROOT / "tools" / "prompts" / "supervisor.md")
    return f"""You are the project supervisor agent for this repository.

Repository root: {ROOT}
Top-level goal: {args.goal}
Cycle: {cycle}
Repo state file: {repo_state}
Required generated task prompt path: {task_prompt}
Required delegation plan JSON path: {delegation_plan}
Cycle log directory: {cycle_dir}

Read the repo state and the supervisor template below. Generate exactly one bounded, concrete task prompt for the worker multi-agent harness and write it to the required generated task prompt path.

Then write the complete worker delegation JSON to the required delegation plan JSON path. This JSON is the dependency graph run script for the downstream Codex CLI windows. It must assign planner, implementer, tester, reviewer, auditor, and controller tasks, dependencies, context, deliverables, verification focus, and model recommendations. It may also add extra top-level specialist agents when that creates real parallelism. Do not ask the user questions. Do not run worker tasks yourself.

--- Supervisor template ---
{template}
"""


def run_supervisor_agent(
    *,
    args: argparse.Namespace,
    codex_info: dict[str, Any],
    cycle: int,
    cycle_dir: Path,
    repo_state: Path,
    task_prompt: Path,
    delegation_plan: Path,
    window_runner: worker.CodexWindowRunner | None = None,
) -> worker.CommandResult:
    prompt_text = compose_supervisor_prompt(args, cycle, cycle_dir, repo_state, task_prompt, delegation_plan)
    prompt_path = cycle_dir / "supervisor.prompt.txt"
    output_path = cycle_dir / "supervisor.md"
    write_text(prompt_path, prompt_text)

    if args.dry_run:
        write_text(task_prompt, default_task_prompt(args.goal, cycle))
        write_text(delegation_plan, json.dumps(worker.default_delegation_plan(cycle_dir), indent=2))
        write_text(output_path, "# Supervisor\n\nDry run: generated a default bounded task prompt without invoking Codex.\n")
        return worker.CommandResult.from_run(
            name="supervisor",
            command=[*(codex_info.get("selected_invocation") or codex_info.get("command_base") or ["codex", "exec"]), f"@{prompt_path}"],
            cwd=ROOT,
            returncode=0,
            stdout="Dry run: supervisor Codex subprocess skipped.\n",
            stderr="",
            duration_seconds=0.0,
        )

    stdout_path = cycle_dir / "supervisor.stdout.txt"
    stderr_path = cycle_dir / "supervisor.stderr.txt"
    if window_runner is not None:
        command = worker.interactive_codex_base_command(codex_info)
        print(f"supervisor agent: opening visible Codex CLI window with prompt={prompt_path}")
        result = window_runner.run_codex_window(
            name=f"cycle-{cycle:03d}-supervisor",
            codex_base_command=command,
            prompt_path=prompt_path,
            completion_path=delegation_plan,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        command = worker.codex_command(f"Read and follow this prompt file: {prompt_path}", codex_info, output_path)
        print(f"supervisor agent: {' '.join(command[:-1])} <prompt-file>")
        result = worker.run_command(
            command,
            cwd=ROOT,
            timeout_seconds=args.timeout_seconds,
            name="supervisor",
            live=args.live_monitor,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    missing = [
        str(path)
        for path in (task_prompt, delegation_plan)
        if not path.exists() or path.stat().st_size == 0
    ]
    if missing:
        result["stderr"] = (result.get("stderr") or "") + "\nSupervisor failed to write required file(s): " + ", ".join(missing) + "\n"
        if result.get("returncode") in (0, None):
            result["returncode"] = 1
    write_text(stdout_path, result["stdout"])
    write_text(stderr_path, result["stderr"])
    return result


def validate_delegation_plan(path: Path) -> tuple[bool, str]:
    try:
        data = json.loads(read_text(path).lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return False, "top-level value must be an object"
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return False, "missing agents object"
    missing = [agent for agent in worker.WORKER_AGENT_ORDER if not isinstance(agents.get(agent), dict)]
    if missing:
        return False, "missing agent assignment(s): " + ", ".join(missing)
    return True, "ok"


def run_worker_harness(args: argparse.Namespace, cycle_dir: Path, task_prompt: Path, delegation_plan: Path) -> worker.CommandResult:
    harness = rel_or_abs(args.harness)
    worker_log_dir = cycle_dir / "worker_loop"
    command = [
        sys.executable,
        str(harness),
        "--task-prompt",
        str(task_prompt),
        "--delegation-plan",
        str(delegation_plan),
        "--repo-root",
        str(ROOT),
        "--max-iters",
        "1",
        "--log-dir",
        str(worker_log_dir),
        "--codex-bin",
        args.codex_bin,
    ]
    if args.print_codex_discovery:
        command.append("--print-codex-discovery")
    if args.live_monitor:
        command.append("--live-monitor")
    if args.monitor_backend:
        command.extend(["--monitor-backend", args.monitor_backend])
    for verification in args.verification_command:
        command.extend(["--verification-command", verification])
    print(f"worker harness: {' '.join(command)}")
    if args.dry_run:
        return worker.CommandResult.from_run(
            name="worker_harness",
            command=command,
            cwd=ROOT,
            returncode=0,
            stdout="Dry run: worker harness not executed.\n",
            stderr="",
            duration_seconds=0.0,
        )
    result = worker.run_command(
        command,
        cwd=ROOT,
        timeout_seconds=args.timeout_seconds,
        name="worker_harness",
        live=args.live_monitor,
    )
    write_text(cycle_dir / "worker_harness.stdout.txt", result["stdout"])
    write_text(cycle_dir / "worker_harness.stderr.txt", result["stderr"])
    return result


def run_goal_verification(
    cycle_dir: Path,
    timeout_seconds: int,
    dry_run: bool,
    configured_commands: list[str],
    live: bool = False,
) -> list[worker.CommandResult]:
    if dry_run:
        return [
            worker.CommandResult.from_run(
                name=name,
                command=command,
                cwd=ROOT,
                returncode=0,
                stdout="Dry run: verification not executed.\n",
                stderr="",
                duration_seconds=0.0,
            )
            for name, command in worker.verification_commands(configured_commands)
        ]
    return worker.run_verification(
        cycle_dir / "supervisor_verification",
        timeout_seconds,
        dry_run=False,
        live=live,
        configured_commands=configured_commands,
    )


def latest_worker_summary(cycle_dir: Path) -> dict[str, Any] | None:
    summaries = latest_files(cycle_dir / "worker_loop", "summary.json", limit=1)
    if not summaries:
        return None
    try:
        return json.loads(read_text(summaries[0]))
    except json.JSONDecodeError:
        return None


def command_ok(results: list[worker.CommandResult], name: str) -> bool:
    for result in results:
        if result["name"] == name:
            return result["returncode"] == 0 and not result["timed_out"]
    return False


def external_blocker_text(text: str) -> str | None:
    lowered = text.lower()
    patterns = [
        "authentication required",
        "auth required",
        "license required",
        "license acceptance",
        "login required",
        "codex cli is unavailable",
        "not logged in",
        "credentials",
        "permission denied",
        "access denied",
    ]
    for pattern in patterns:
        if pattern in lowered:
            return pattern
    return None


def classify_supervisor(
    *,
    verification_results: list[worker.CommandResult],
    worker_result: worker.CommandResult,
    worker_summary: dict[str, Any] | None,
    cycle_dir: Path,
    dry_run: bool,
    success_artifacts: list[str],
) -> tuple[str, str]:
    if dry_run:
        return "CONTINUE", "Dry run generated the next task prompt and skipped worker execution."

    worker_verdict = None
    if worker_summary:
        raw_verdict = worker_summary.get("verdict")
        if isinstance(raw_verdict, str):
            worker_verdict = raw_verdict.upper()

    required_ok = all(
        command_ok(verification_results, name)
        for name in {result["name"] for result in verification_results}
    )
    artifacts_ok = all((ROOT / path).exists() and (ROOT / path).stat().st_size > 0 for path in success_artifacts)
    if required_ok and artifacts_ok and worker_verdict == "COMPLETE":
        return "COMPLETE", "Goal verification passed and required success artifacts exist."

    blocker = None
    if worker_verdict == "BLOCKED":
        blocker = external_blocker_text(json.dumps(worker_summary or {})) or "worker reported BLOCKED"
    elif worker_summary is None:
        blocker = external_blocker_text((worker_result.get("stdout") or "") + "\n" + (worker_result.get("stderr") or ""))

    if blocker:
        return (
            "BLOCKED",
            "User-only blocker detected: "
            f"{blocker}. Next setup requirement: provide the required auth, credential, license acceptance, account access, or permission.",
        )

    return "CONTINUE", "Goal verification did not pass, but no external blocker was detected."


def write_cycle_report(
    *,
    cycle_dir: Path,
    cycle: int,
    verdict: str,
    reason: str,
    supervisor_result: worker.CommandResult,
    worker_result: worker.CommandResult,
    verification_results: list[worker.CommandResult],
    task_prompt: Path,
) -> None:
    lines = [
        f"# Supervisor Cycle {cycle:03d}",
        "",
        f"- verdict: {verdict}",
        f"- reason: {reason}",
        f"- task_prompt: {task_prompt}",
        "",
        "## Supervisor Agent",
        f"- returncode: {supervisor_result['returncode']}",
        f"- timed_out: {supervisor_result['timed_out']}",
        "",
        "## Worker Harness",
        f"- returncode: {worker_result['returncode']}",
        f"- timed_out: {worker_result['timed_out']}",
        "",
        "## Verification",
    ]
    for result in verification_results:
        lines.append(f"- {result['name']}: returncode={result['returncode']} timed_out={result['timed_out']}")
    write_text(cycle_dir / "report.md", "\n".join(lines) + "\n")
    summary = {
        "cycle": cycle,
        "verdict": verdict,
        "reason": reason,
        "task_prompt": str(task_prompt),
        "supervisor_result": supervisor_result,
        "worker_result": worker_result,
        "verification_results": verification_results,
    }
    write_text(cycle_dir / "summary.json", json.dumps(summary, indent=2))


def write_blocked_preflight(log_dir: Path, codex_info: dict[str, Any]) -> None:
    cycle_dir = log_dir / "cycle_001"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    report = (
        "# Supervisor Cycle 001\n\n"
        "- verdict: BLOCKED\n"
        "- reason: Codex CLI is unavailable. The supervisor does not inspect auth files or implement auth.\n\n"
        "## Candidates Checked\n"
        + "\n".join(
            f"- [{'PASS' if c.get('passed') else 'FAIL'}] {c.get('source')}: {c.get('display')} - {c.get('failure') or c.get('reason')}"
            for c in codex_info.get("candidates", [])
        )
        + "\n\n## Remediation\n\n```powershell\nnpm install -g @openai/codex\ncodex login\n```\n"
    )
    write_text(cycle_dir / "report.md", report)
    write_text(
        cycle_dir / "summary.json",
        json.dumps(
            {
                "cycle": 1,
                "verdict": "BLOCKED",
                "reason": "Codex CLI is unavailable or not executable.",
                "remediation": ["npm install -g @openai/codex", "codex login"],
                "codex": codex_info,
            },
            indent=2,
        ),
    )


def main() -> int:
    args = parse_args()
    set_repo_root(args.repo_root)
    log_dir = rel_or_abs(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    codex_info = worker.detect_codex(args.codex_bin, args.timeout_seconds, args.allow_npx_codex)
    write_text(log_dir / "codex_detection.json", json.dumps(codex_info, indent=2))
    if args.print_codex_discovery:
        worker.print_codex_discovery(codex_info)
    worker.print_codex_preflight(codex_info)
    if not codex_info["available"]:
        write_blocked_preflight(log_dir, codex_info)
        print("Final verdict: BLOCKED")
        return 2

    window_runner: worker.CodexWindowRunner | None = None
    monitor_backend = args.monitor_backend
    if monitor_backend:
        window_runner = worker.CodexWindowRunner(backend=monitor_backend)
        window_runner.ensure()
        print(f"window runner: {window_runner.attach_instructions()}")

    final_verdict = "CONTINUE"
    final_reason = "Single supervisor cycle completed."
    cycle = 1
    cycle_dir = log_dir / "cycle_001"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    print(f"supervisor cycle {cycle}: logs={cycle_dir}")

    repo_state = collect_repo_state(cycle_dir)
    task_prompt = cycle_dir / "task_prompt.md"
    delegation_plan = cycle_dir / "delegation_plan.json"
    supervisor_result = run_supervisor_agent(
        args=args,
        codex_info=codex_info,
        cycle=cycle,
        cycle_dir=cycle_dir,
        repo_state=repo_state,
        task_prompt=task_prompt,
        delegation_plan=delegation_plan,
        window_runner=window_runner,
    )
    plan_ok, plan_reason = (False, "supervisor failed")
    if supervisor_result["returncode"] == 0:
        plan_ok, plan_reason = validate_delegation_plan(delegation_plan)
    if supervisor_result["returncode"] != 0 or not plan_ok:
        final_verdict = "BLOCKED"
        final_reason = f"Supervisor did not produce a usable delegation plan: {plan_reason}"
        worker_result = worker.CommandResult.from_run(
            name="worker_harness",
            command=[sys.executable, str(rel_or_abs(args.harness)), "--delegation-plan", str(delegation_plan)],
            cwd=ROOT,
            returncode=1,
            stdout="Worker harness skipped because supervisor output was incomplete or invalid.\n",
            stderr=final_reason + "\n",
            duration_seconds=0.0,
        )
        verification_results: list[worker.CommandResult] = []
        write_cycle_report(
            cycle_dir=cycle_dir,
            cycle=cycle,
            verdict=final_verdict,
            reason=final_reason,
            supervisor_result=supervisor_result,
            worker_result=worker_result,
            verification_results=verification_results,
            task_prompt=task_prompt,
        )
        print(f"supervisor cycle {cycle}: verdict={final_verdict}")
        print(f"Final verdict: {final_verdict}")
        print(final_reason)
        return 2
    worker_result = run_worker_harness(args, cycle_dir, task_prompt, delegation_plan)
    verification_results = run_goal_verification(
        cycle_dir,
        args.timeout_seconds,
        args.dry_run,
        args.verification_command,
        live=args.live_monitor,
    )
    worker_summary = latest_worker_summary(cycle_dir)
    final_verdict, final_reason = classify_supervisor(
        verification_results=verification_results,
        worker_result=worker_result,
        worker_summary=worker_summary,
        cycle_dir=cycle_dir,
        dry_run=args.dry_run,
        success_artifacts=args.success_artifact,
    )
    write_cycle_report(
        cycle_dir=cycle_dir,
        cycle=cycle,
        verdict=final_verdict,
        reason=final_reason,
        supervisor_result=supervisor_result,
        worker_result=worker_result,
        verification_results=verification_results,
        task_prompt=task_prompt,
    )
    print(f"supervisor cycle {cycle}: verdict={final_verdict}")
    print(f"Final verdict: {final_verdict}")
    print(final_reason)
    return 0 if final_verdict in ("COMPLETE", "CONTINUE") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        if "--debug" in sys.argv:
            traceback.print_exc()
        else:
            print(f"Supervisor error: {exc}", file=sys.stderr)
        raise SystemExit(1)

