# Supervisor Workflow

`tools/codex_supervisor.py` adds a project-management layer above the worker harness.

Architecture:

```text
supervisor Codex subprocess
  -> writes task_prompt.md
  -> writes delegation_plan.json
  -> tools/codex_multiagent_loop.py
      -> launch every ready agent concurrently
      -> recursively run child delegation plans
      -> verify after controller/final graph outputs
```

This is true multi-agent execution because every ready role is launched as a separate Codex CLI subprocess, and independent roles can be live at the same time. The supervisor is prompted once per invocation, writes the run script for the worker agents, and exits. The supplied delegation plan is executed once. Do not wrap the supervisor in an external loop to simulate cycles after `CONTINUE`; launch a new explicit run only when a human or higher-level orchestrator intentionally starts a new milestone.

Run:

```powershell
python C:\Users\Luke\Documents\Code\MultiAgentHarness\tools\codex_supervisor.py `
  --repo-root C:\path\to\repo `
  --goal "Complete the next verified milestone" `
  --verification-command "pytest"
```

The supervisor reads the target repo's `README.md`, `AGENTS.md`, `WORKLOG.md`, `REAL_VS_MOCKED.md`, `ROADMAP.md`, previous loop reports, and recent stderr logs when present. It writes `task_prompt.md` and `delegation_plan.json` under `runs/codex_supervisor/cycle_001` by default, invokes one worker pass with that supplied delegation plan, then classifies the run.

Verdicts:

- `COMPLETE`: configured verification commands pass and required success artifacts exist.
- `BLOCKED`: progress requires the user, such as authentication, credentials, license acceptance, account access, or permissions the agent cannot grant.
- `CONTINUE`: more code-level work remains; the current supervisor invocation is still finished.

Verification gates override model claims. A controller or supervisor may recommend success, but the harness only reports completion when commands and artifacts prove it.
