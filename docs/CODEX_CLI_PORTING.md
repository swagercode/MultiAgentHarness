# Codex CLI Porting Notes

This harness currently prefers a local source-built official Codex CLI when one is available next to the harness repository.

Current known-good local setup:

- Harness path: `C:\Users\Luke\Documents\Code\MultiAgentHarness`
- Codex source path: `C:\Users\Luke\Documents\Code\codex-cli-default-tier`
- Codex source remote: `https://github.com/openai/codex.git`
- Codex source commit: `7a7cee1be4e82733f393c95180242d11b50064d5`
- Selected executable: `C:\Users\Luke\Documents\Code\codex-cli-default-tier\codex-rs\target\debug\codex.exe`
- Reported version: `codex-cli 0.0.0`

The Codex source checkout had no local source changes when these notes were written, so a private mirror is not required to preserve the special CLI behavior. Rebuild the official source at the pinned commit on the other machine instead.

## Recreate on another Windows machine

Clone the harness and the Codex source as sibling directories:

```powershell
cd C:\Users\Luke\Documents\Code
git clone https://github.com/swagercode/MultiAgentHarness.git
git clone https://github.com/openai/codex.git codex-cli-default-tier
cd codex-cli-default-tier
git checkout 7a7cee1be4e82733f393c95180242d11b50064d5
```

Build the Codex CLI from source:

```powershell
cd C:\Users\Luke\Documents\Code\codex-cli-default-tier\codex-rs
cargo build
```

Verify that the harness selects the source-built executable:

```powershell
cd C:\Users\Luke\Documents\Code\MultiAgentHarness
python scripts/check_monitor_env.py
```

The expected `codex_path` is:

```text
C:\Users\Luke\Documents\Code\codex-cli-default-tier\codex-rs\target\debug\codex.exe
```

## Auth and config

Do not copy Codex auth files between machines. Install and log in with the official Codex CLI on the new computer:

```powershell
codex login
```

If configuration needs to be recreated, prefer setting it through Codex CLI commands or manually recreating non-secret settings. Do not publish or commit local Codex auth material.
