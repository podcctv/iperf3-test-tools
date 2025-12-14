---
trigger: always_on
---

## Terminal execution rules

- Prefer direct execution for allow-listed commands.
- Never wrap commands using `sh -c` or combine commands with `&&`.
- Execute one command at a time.
- For git operations:
  - Auto-execute read-only commands (status, log, diff, fetch, pull).
  - Never auto-execute reset, clean, or force operations.
- For docker compose:
  - Auto-execute ps, logs, pull, build, up.
  - Never auto-execute down, rm, or exec without explanation.
- If a command may destroy data or stop services, explain first and wait.
