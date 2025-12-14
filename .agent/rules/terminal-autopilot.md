---
trigger: always_on
---

Terminal Autopilot Rules (PowerShell)

==================================================
0) Scope
==================================================
- This ruleset is for PowerShell terminals on Windows.
- Goal:
  - maximize safe auto-execution
  - minimize manual approvals
  - enforce post-deployment validation (docker + database)

==================================================
1) Execution Discipline (MANDATORY)
==================================================
1. Execute EXACTLY ONE terminal command per execution request.
2. DO NOT chain commands using ; && or || .
3. DO NOT use pipelines | unless explicitly requested by the user.
4. DO NOT use redirections:
   - 2>&1
   - *>
   - Out-File
   - Tee-Object
   unless explicitly requested.
5. DO NOT wrap commands using:
   - powershell -Command
   - scriptblocks { }
   - Invoke-Expression
   - cmd /c
6. If waiting is needed, run Start-Sleep as a standalone command only.

Reason:
Chaining, redirection, and scripting patterns often trigger manual approval
even if individual commands are allow-listed.

==================================================
2) Allowed Auto-Execution (Safe Command Families)
==================================================

The agent SHOULD auto-execute these commands when they match the allow list
and remain standalone.

Docker / Docker Compose (safe subset):
- docker compose ps
- docker compose pull
- docker compose build
- docker compose up
- docker compose up -d
- docker compose logs --tail <N> <service>
- docker logs --tail <N> <container>
- docker ps
- docker inspect <container>

Git (safe subset):
- git status
- git log
- git diff
- git fetch
- git pull
- git branch
- git show

Wait:
- Start-Sleep -Seconds <N>

==================================================
3) Docker Deploy: Mandatory Post-Checks
==================================================

After ANY of the following actions:
- docker compose up
- docker compose up -d
- docker compose pull
- docker compose build
- docker compose restart <service> (only when explicitly requested or after approved DB migration)

The agent MUST run the validation sequence below.

--------------------------------------------------
Step A: Check container / service state
--------------------------------------------------
Run:
- docker compose ps

Interpretation:
- If all services are running (or healthy), proceed to Step C.
- If any service is exited, restarting, or unhealthy, go to Step B.

--------------------------------------------------
Step B: Inspect logs
--------------------------------------------------
For each failing or suspicious service/container, run ONE of:
- docker compose logs --tail 80 <service_name>
- docker logs --tail 80 <container_name_or_id>

--------------------------------------------------
Step C: Classify startup result
--------------------------------------------------
If logs contain ANY of the following, treat startup as FAILED:

- ERROR / Error / error
- FATAL
- panic
- exception
- Traceback
- permission denied
- address already in use
- bind:
- cannot
- failed
- exited
- restarting
- CrashLoop

On FAILED startup:
1. Stop further automation immediately.
2. Quote only the minimal relevant error lines.
3. Suggest the smallest next diagnostic step (read-only first).
4. DO NOT run destructive or recovery commands automatically.

On OK startup:
- Report that deployment looks healthy.

==================================================
4) Database Migration Handling (Controlled + Verifiable)
==================================================

--------------------------------------------------
4.1 Migration Trigger Conditions
--------------------------------------------------
If any of the following appear in logs or errors:
- column "<X>" does not exist
- relation does not exist
- schema mismatch
- migration required

The agent MUST switch to the Migration Flow below.

--------------------------------------------------
4.2 Migration Flow (Read-only First)
--------------------------------------------------

Step 1: Identify DB container and table
- Prefer known container/service names if visible in logs
  (example: iperf3-test-tools-db-1)
- Otherwise, run docker compose ps to identify DB container

Step 2: Read-only schema inspection (ALLOWED)
The agent MAY auto-execute read-only commands such as:
- docker exec <db_container> psql -U <user> -d <db> -c "\d <table>"
- SELECT column_name FROM information_schema.columns WHERE table_name = '<table>';

NO DDL is allowed in this step.

--------------------------------------------------
4.3 Schema Modification (MANUAL APPROVAL REQUIRED)
--------------------------------------------------
The agent MUST NOT auto-execute:
- ALTER TABLE
- CREATE TABLE
- DROP TABLE
- DROP COLUMN
- any irreversible DDL

Instead, the agent MUST:
1. Clearly explain what is missing and why it is required.
2. Provide the exact SQL statement to be executed.

Example:
ALTER TABLE test_schedules
ADD COLUMN IF NOT EXISTS udp_bandwidth VARCHAR(50);

3. Wait for explicit user approval before executing.

--------------------------------------------------
4.4 Post-Migration Mandatory Verification
--------------------------------------------------

After user approval and successful DDL execution:

Step A: Verify schema updated
- docker exec <db_container> psql -U <user> -d <db> -c "\d test_schedules"

Step B: Restart dependent service (single command only)
- docker compose restart master-api

Step C: Validate service startup
- docker compose ps
- docker logs --tail 80 master-api

If startup errors appear:
- Stop automation
- Report errors
- Do NOT retry destructively

==================================================
5) Forbidden Without Explicit Instruction (High Risk)
==================================================

Docker:
- docker exec (except read-only schema inspection)
- docker compose down
- docker compose rm
- docker rm
- docker system prune
- any volume or image deletion

Git:
- git reset
- git clean
- git push --force
- any history-rewriting operation

==================================================
6) Error Handling Policy
==================================================
- On any command failure: stop and explain clearly.
- Prefer read-only diagnostics before modifications.
- Never retry destructive commands automatically.
- Keep commands atomic to maximize allow-list auto-execution.

==================================================
7) Practical PowerShell Execution Templates
==================================================

Wait then check logs (DO NOT chain):
1) Start-Sleep -Seconds 3
2) docker logs --tail 80 <container>

Deploy then validate:
1) docker compose up -d
2) docker compose ps
3) docker logs --tail 80 <service_or_container>

End of ruleset.
