# Flush the demo cart cache

This folder contains an isolated entry point for clearing the dedicated
`valkey-cart` database. It uses the shared `actions.registry` and
`actions.runner` safety/execution path. It is not proposed by
`actions.run_incident`; wiring it into that incident flow would require a
separate, reviewed proposal-selection change.

## What this action can do

- Check that the `valkey-cart` container is running and healthy.
- Show the exact operation before approval:
  `docker exec valkey-cart valkey-cli FLUSHDB ASYNC`
- Ask for explicit human approval using the existing `actions.approval` gate.
- Validate the fixed action, target, parameters, and preview using
  `actions.registry`.
- Delegate dry-run/execution, kill-switch rechecks, timeout handling, and audit
  events to `actions.runner`.
- Default to a dry run. Pass `--execute` to run the approved command.
- Check the shared kill switch before approval and immediately before execution.
- Write dry-run, blocked, and execution events to the existing action audit log.
- Apply a command timeout and report command failures.

## Scope and impact

The operation clears every key in the Valkey database used by the demo cart
service. Cart contents stored there will be lost; the cart service may recreate
keys as users interact with it. This handler does not target other databases or
containers, and it does not support arbitrary commands, key patterns, prefixes,
or production endpoints. It does not claim that clearing the cache fixes an
incident; recovery must be checked separately.

## Local use

From the repository root:

```powershell
python actions\flush-cache --incident-id <incident-id>
```

That asks for approval and performs a dry run. To execute after approval:

```powershell
python actions\flush-cache --incident-id <incident-id> --execute
```

Keep `reports/actions/KILL` present to block approval and execution.
