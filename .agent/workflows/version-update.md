---
description: how to update agent version when making breaking changes
---

# Agent Version Update Workflow

When making changes that require BOTH master-api and agent to be updated for the feature to work, you MUST update the version numbers:

## Steps

// turbo-all

1. Update agent version in `agent/app.py` line ~22:
   ```python
   AGENT_VERSION = "X.Y.Z"  # Increment this
   ```

2. Update expected version in `master-api/app/main.py` line ~2727 (in JavaScript template):
   ```javascript
   const expectedVersion = 'X.Y.Z';  // Match the agent version
   ```

3. Use semantic versioning:
   - MAJOR.MINOR.PATCH (e.g., 1.0.2 → 1.0.3)
   - Increment PATCH for bug fixes
   - Increment MINOR for new features
   - Increment MAJOR for breaking changes

## When to Update

Update versions when:
- Adding new API endpoints that agent needs to call
- Changing request/response payload formats
- Adding new required configuration options
- Any feature requiring both master and agent to be updated together

## Current Locations

| Component | File | Variable |
|-----------|------|----------|
| Agent | `agent/app.py` | `AGENT_VERSION` (line ~22) |
| Master (expected) | `master-api/app/main.py` | `expectedVersion` (line ~2727 in JS) |
