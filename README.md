# MultiAgentHarness

Reusable Codex CLI multi-agent orchestration for local repositories.

This repo contains agent orchestration only. It does not contain project-specific application logic. Point it at a target repository with `--repo-root` and provide a bounded task prompt plus verification commands.

## Worker Loop

Run one bounded task with separate Codex subprocess agents:

```powershell
python tools/codex_multiagent_loop.py --repo-root C:\path\to\repo --task-prompt C:\path\to\task.md --max-iters 5 --verification-command "pytest"
```

The normal supervised flow passes a supervisor-written `delegation_plan.json` to the worker. The worker treats that JSON as a dependency graph: every agent whose `depends_on` entries are satisfied launches immediately, so independent agents run concurrently as separate Codex CLI subprocesses. If the worker is run directly without a delegation plan, it falls back to a one-time manager agent that writes the plan first. Logs are written under this harness repo's `runs/codex_loop` by default.

Agents can request nested delegation by writing a child delegation JSON file to the optional child-plan path in their prompt. The worker runs child plans recursively with the same dependency-graph scheduler, bounded by `--max-agent-recursion-depth`.

## Supervisor

Run a single supervisor pass that generates one bounded task prompt, writes one delegation plan JSON file, invokes the worker loop, and verifies the result:

```powershell
python tools/codex_supervisor.py --repo-root C:\path\to\repo --goal "Ship the next verified milestone" --verification-command "pytest"
```

The supervisor is also a real Codex CLI subprocess. It is prompted exactly once per run. It handles prompt and delegation-plan management; the worker harness handles exactly one supplied-plan worker pass.

`--max-cycles` and `--max-harness-iters` are not part of the supervisor interface. If the result is `CONTINUE`, the current invocation is complete; do not wrap the supervisor in an automatic repeat loop to create hidden cycles.

## Visible Codex Windows

Use the native Windows backend to open real Codex CLI windows for the supervisor and each worker agent:

```powershell
python tools/codex_supervisor.py --repo-root C:\path\to\repo --goal "Ship the next verified milestone" --monitor-backend auto
```

On Windows, `auto` uses `powershell-windows`. The supervisor window receives the automated prompt as its initial user message and writes `task_prompt.md` plus `delegation_plan.json`. The worker then opens Codex CLI windows according to that JSON dependency graph; multiple ready agents can be live at the same time. The harness continues when required output files are written and dependencies are satisfied.

When the installed Codex CLI supports it, visible windows are launched with `--no-alt-screen` and `--disable shell_snapshot` so terminal output remains readable and Codex does not start an extra shell snapshot process inside the managed console.

Supported native window backends are `auto` and `powershell-windows`.

## Codex CLI

The harness uses only the official Codex CLI as a subprocess. It does not implement authentication and does not read Codex auth files.

If Codex is unavailable, install and log in:

```powershell
npm install -g @openai/codex
codex login
```
