---
name: tsec-actions
description: Use when submitting a challenge flag in an authorized TSEC CTF or intelligent penetration-testing competition.
---

## Usage

- Call the API directly with `curl`. `TSEC_SERVER_HOST` and `TSEC_AGENT_TOKEN` are already
  provided as environment variables.
- Flags use the `flag{...}` format.

Submit a flag:

```bash
curl -X POST "http://${TSEC_SERVER_HOST}/api/submit" \
  -H "Agent-Token: ${TSEC_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code": "<challenge_code>", "flag": "<flag>"}'
```

## Rules

- Submit only when confidence is high. Do not use the submission endpoint for brute force.
- Use the API response to determine whether submission succeeded. Do not assume success.
