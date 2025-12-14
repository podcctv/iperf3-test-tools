---
trigger: always_on
---

Workflow: deploy-with-commit

Step 1: Check git working tree
- git status
- git diff

Decision:
- If no changes detected:
  - Skip commit
- If changes detected:
  - Generate commit message using commit rules
  - Show commit message
  - git add .
  - git commit -m "<message>"

Step 2: Deploy
- docker compose up -d

Step 3: Validate deployment
- docker compose ps

If any service is not running:
- docker logs --tail 80 <service>

Success condition:
- Service running
- No startup errors in logs

Failure condition:
- exited / restarting / ERROR / panic detected
- Stop immediately and report
