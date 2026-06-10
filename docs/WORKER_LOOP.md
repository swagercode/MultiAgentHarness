# Worker Loop

`tools/codex_multiagent_loop.py` runs one bounded task against a target repository.

The loop uses separate Codex CLI subprocess agents:

- planner: creates the current iteration plan
- implementer: edits code, docs, and tests
- tester: runs or interprets verification
- reviewer: reviews changes and risks
- controller: recommends `COMPLETE`, `PARTIAL`, `BLOCKED`, or `CONTINUE`

With a supplied `delegation_plan.json`, the worker runs agents as a dependency graph. Any agents whose `depends_on` entries are satisfied launch concurrently up to `--max-concurrent-agents`. Agents may write child delegation plans to their optional child-plan path; child plans run recursively up to `--max-agent-recursion-depth`.

The harness also inspects command results. Model claims do not override verification failures.

Example:

```powershell
python C:\Users\Luke\Documents\Code\MultiAgentHarness\tools\codex_multiagent_loop.py `
  --repo-root C:\path\to\repo `
  --task-prompt C:\path\to\task.md `
  --max-iters 5 `
  --verification-command "pytest"
```

Logs are written to `runs/codex_loop` inside the harness repository unless `--log-dir` is overridden.

Use `--print-codex-discovery` to print every Codex CLI candidate checked. Use `--dry-run` to write prompts and reports without launching Codex agents.
