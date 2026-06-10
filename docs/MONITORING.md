# Native Codex Windows

Visible execution opens real Codex CLI sessions in separate PowerShell windows. These windows are the supervisor and worker agents themselves; they are not log-tail monitors.

Backends:

- `powershell-windows`: separate visible PowerShell-hosted Codex CLI windows
- `auto`: selects `powershell-windows` on Windows

Run diagnostics:

```powershell
python scripts/check_monitor_env.py
```

Run monitored supervisor:

```powershell
python tools/codex_supervisor.py --repo-root C:\path\to\repo --goal "Complete the next verified milestone" --monitor-backend auto
```

The supervisor window receives the generated supervisor prompt as the initial user message. After it writes `task_prompt.md` and `delegation_plan.json`, the worker opens planner, implementer, tester, reviewer, and controller Codex CLI windows according to that JSON.

There is no tmux, WSL, Windows Terminal, or verification log-tail window backend.
