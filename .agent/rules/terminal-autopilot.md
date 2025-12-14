---
trigger: always_on
---

Terminal Autopilot Rules (PowerShell) - Auto Commit & Sync Enabled

==================================================
0) Scope
==================================================
- PowerShell on Windows.
- Goal:
  - allow agent to automatically commit AND sync (push) after it finishes writing changes
  - keep deployment validation and DB migration safety boundaries

==================================================
1) Execution Discipline (MANDATORY)
==================================================
1. Execute EXACTLY ONE terminal command per execution request.
2. DO NOT chain commands using ; && or || .
3. DO NOT use pipelines | unless explicitly requested.
4. DO NOT use redirections (2>&1, *>, Out-File, Tee-Object) unless explicitly requested.
5. DO NOT wrap commands using powershell -Command, { }, Invoke-Expression, cmd /c.
6. If waiting is needed, run Start-Sleep as a standalone command only.

==================================================
2) Allowed Auto-Execution (Command Families)
==================================================

Docker (safe subset):
- docker compose ps
- docker compose pull
- docker compose build
- docker compose up
- docker compose up -d
- docker compose logs --tail <N> <service>
- docker logs --tail <N> <container>
- docker ps
- docker inspect <container>

Git (read/sync):
- git status
- git log
- git diff
- git fetch
- git pull
- git branch
- git show
- git rev-parse --abbrev-ref HEAD
- git remote -v

Git (write + sync, controlled):
- git add <path>
- git add .
- git commit -m "<message>"
- git push origin <branch>
- git push --set-upstream origin <branch>   (ONLY if upstream is missing and branch is current branch)

Wait:
- Start-Sleep -Seconds <N>

==================================================
3) Commit Message Standard (Template)
==================================================
Commit message MUST be exactly one line in this format:
<type>(<scope>): <summary>

type must be one of:
feat | fix | chore | refactor | docs | ops | db

scope must be short module/service name:
api | db | docker | migration | scheduler | master-api | etc.

summary rules:
- single line
- start with a verb (add/fix/handle/update/adjust)
- no placeholders (NO: WIP, tmp, test, update)
- <= 80 chars preferred

==================================================
4) Auto Commit Policy (CRITICAL)
==================================================
Agent may auto-commit ONLY when ALL conditions are met:

A) Pre-checks (MUST run and confirm):
1) git status   (must show modified/added files)
2) git diff     (agent must review and summarize changes in plain language)

B) Staging rules:
- Prefer: git add <specific paths> when feasible
- Allowed fallback: git add . ONLY when the change is confined to the intended workspace
- MUST NOT stage secrets or credentials
  (no .env, no private keys, no tokens; if detected, STOP and ask)

C) Commit rules:
- MUST generate a compliant commit message (Section 3)
- MUST show the final commit message before executing commit
- Then run:
  1) git add ...
  2) git commit -m "<message>"

D) Prohibited commit behaviors (never auto):
- git commit --amend
- interactive rebase
- rewriting history

==================================================
5) Auto SYNC Policy (Push) (CRITICAL)
==================================================
Agent may auto-sync (push) ONLY when ALL conditions are met:

A) Must determine current branch:
- git rev-parse --abbrev-ref HEAD
Rule: push ONLY the current branch.

B) Must confirm remote is origin and unchanged:
- git remote -v
Rule: MUST NOT change or add remotes automatically.

C) Must fetch and check for remote updates first:
- git fetch

D) Must avoid conflicts:
- If remote has new commits that require integration, do NOT auto-merge.
- Allowed safe integration (optional, only if you want it):
  - git pull --rebase   (ONLY if you explicitly allow it in Allow List and want rebase behavior)
Otherwise:
- Stop and ask.

E) Push rules:
- Push ONLY to origin/<current-branch>
- Use:
  - git push origin <current-branch>
- If upstream is missing:
  - git push --set-upstream origin <current-branch>
- Never push tags automatically.
- Never force push.

==================================================
6) Deploy: Mandatory Post-Checks (Docker)
==================================================
After docker compose up/pull/build/restart:

Step A: docker compose ps
Step B: if suspicious -> docker logs --tail 80 <service/container>
Step C: classify failure on:
ERROR/FATAL/panic/Traceback/permission denied/address already in use/bind/cannot/failed/exited/restarting/CrashLoop
If failed:
- stop automation
- quote minimal relevant error lines
- suggest next read-only diagnostic step

==================================================
7) Database Migration Safety
==================================================
- Read-only schema inspection is allowed.
- DDL (ALTER/CREATE/DROP) requires manual approval.
- After approved migration:
  - restart service (single command)
  - run docker ps/ps + logs validation

==================================================
8) Forbidden Without Explicit Instruction (High Risk)
==================================================
Docker:
- docker exec (except read-only schema inspection)
- docker compose down / rm
- docker rm
- docker system prune
- volume/image deletion

Git:
- git push --force
- git push --mirror
- git reset (any)
- git clean (any)
- git commit --amend
- git rebase -i
- git checkout <other-branch>
- git remote add/set-url
- tag pushes

Shell:
- powershell -Command
- Invoke-Expression
- cmd /c

==================================================
9) End-to-End Auto Flow (Agent Behavior)
==================================================
When agent finishes writing changes in repo:

1) git status
2) git diff
3) git rev-parse --abbrev-ref HEAD
4) generate commit message (standard format)
5) git add ...
6) git commit -m "<message>"
7) git fetch
8) git push origin <current-branch>
9) (optional) deploy + validate if asked

End of ruleset.
