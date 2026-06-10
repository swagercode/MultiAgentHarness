# Agent Notes

This repository is the reusable Codex multi-agent harness. Keep it decoupled from any specific application project.

Do not add project-specific business logic or target-repo implementation code here. The harness may read target-repo state, generate task prompts, invoke Codex CLI subprocess agents, and record logs.

Use the official Codex CLI only. Do not implement custom ChatGPT auth. Do not inspect, read, copy, or parse Codex auth files.

For visible subagent windows on Windows, prefer a local source-built official Codex binary when available, such as `C:\Users\Luke\Documents\Code\codex-cli-default-tier\codex-rs\target\debug\codex.exe`. The installed `0.129.0-alpha.2` binary rejects `service_tier = "default"` in config, while the current source build accepts default/standard routing. Do not force `service_tier = "fast"` just to make windows launch.

Preserve true multi-agent behavior:

- The supervisor is a separate Codex subprocess for prompt management.
- In the supervised flow, the supervisor is prompted exactly once, writes `task_prompt.md` and `delegation_plan.json`, then exits.
- With a supplied delegation plan, the worker harness skips the manager and runs the configured worker iteration loop without re-prompting the supervisor. In each iteration, every agent whose dependencies are satisfied launches immediately as a separate Codex subprocess with its JSON assignment and assigned model when provided.
- When orchestrating, structure delegation plans for as much practical parallelism as the task allows. Prefer independent agents running concurrently to reduce wall-clock time, while keeping real dependency, shared-file, and verification ordering constraints explicit.
- Keep delegation boundaries clean so agents do not step on each other. Assign each parallel agent a clear scope, expected artifact, and file ownership or read-only role; serialize work when multiple agents must edit the same files or shared state.
- Agents may write child delegation plans to their optional child-plan path. The worker runs those child plans recursively with the same dependency-graph scheduler and bounded recursion.
- If the worker harness is run directly without a supplied delegation plan, it may launch a one-time manager Codex subprocess to write `delegation_plan.json` before the worker agents.
- Do not wrap `tools/codex_supervisor.py` in a loop after `CONTINUE`. A supervisor invocation is one prompt-management pass and one worker run.
- Native Windows mode opens real PowerShell-hosted Codex CLI windows. Do not add tmux, WSL, Windows Terminal, or log-tail monitor window backends.

The supervisor must not declare success without verification command results and required artifacts when configured. It must not ask the user questions during a run. It must keep tasks bounded, continue over code-level failures, and honor iteration caps. Missing local runtimes, packages, services, launch commands, generated artifacts, or repo setup are agent work when the machine can install, build, configure, or start them. Stop for the user only when progress requires authentication, credentials, license acceptance, account access, or permissions that the agent cannot grant.

Before handing off meaningful harness changes:

```powershell
python -m py_compile tools/codex_multiagent_loop.py tools/codex_supervisor.py scripts/check_monitor_env.py
```
