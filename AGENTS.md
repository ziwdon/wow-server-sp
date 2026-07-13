# Local Codex Continuity Guard

This file is local operational guidance. It is not wow-server-sp product code or shared project documentation.

This guard is already installed. Reading it is not sufficient: a cold session
that receives an explicit whole-plan terminal instruction must actually inspect
and establish persistent goal state before beginning implementation.

When the user asks to execute a multi-task plan:

- If the user explicitly requires a terminal outcome such as "finish the whole
  plan," "continue until all pending tasks are complete," or "do not stop,"
  use the persistent goal controller before implementation: call `get_goal`;
  if no goal is active, call `create_goal` with the complete plan outcome as
  its objective. `update_plan` is only a task ledger and is not a substitute
  for a persistent goal.
- Keep that goal active across individual task completions and turn boundaries.
  Never call `update_goal(status="complete")` for an individual task; complete
  it only after the entire requested plan and all required validation are done.
- Treat completion of the entire requested plan, not completion of an individual task, as the normal terminal condition.
- After validating a task, identify and begin the next dependency-ready task immediately. Progress updates belong in commentary, not a final response.
- Before any final response, check whether a task remains ready or active, an agent remains active, or required validation remains pending. If any answer is yes, continue execution.
- A final response is allowed only when the complete requested plan is done, the user asks to stop, or a genuine external blocker or unresolved user policy decision requires a handoff.
- For incident history, recovery instructions, and the dry-run test, read `.codex/continuity.md`.

## Host-safety incident guard

Treat `/opt/stacks/azerothcore`, `/opt/stacks/azerothcore-admin`, the local
Docker daemon/socket, `azerothcore.service`, and all `ac-*` or
`azerothcore-*` containers, networks, and volumes as **live production
state**. Do not make lifecycle changes to any of them unless the user has
explicitly asked for that live operation.

### Mandatory rules for tests and scripts with destructive behavior

- Never run, delegate, or approve a host-side test that can reach live Docker
  and issue `docker compose down`, `docker rm`, `docker network rm`, `docker
  volume rm`, `systemctl`, `sudo`, destructive `rm`, an installer, or an
  uninstaller. This includes invoking a lifecycle script with `--yes`.
- A test fixture is not safe merely because it passes temporary environment
  variables. Verify that the script under test consumes them; copy the source
  to a temporary directory and rewrite or otherwise isolate every hard-coded
  live path before executing it.
- Destructive-script tests must be hermetic: use a stub-only `PATH` for at
  least `docker`, `sudo`, `systemctl`, `rm`, and `crontab`, or run in an
  isolated VM/container with no Docker socket, no privileged access, and no
  host `/opt` mount. Never expose `/var/run/docker.sock` to such a test.
- Before executing a destructive-script test, explicitly fail closed if Docker
  resolves to the host daemon, if a target path canonicalizes beneath
  `/opt/stacks`, or if any required command is not a test stub. Prefer a
  `--dry-run` path first.
- After any lifecycle-related test or command attempt, immediately perform a
  non-mutating health check of the live stack. If an expected AC or admin
  container is not up, stop further testing and report the incident; do not
  start, stop, restore, or redeploy it without the user's direction.

Incident record: on 2026-07-13 a direct Compose-style teardown removed the
live AC containers and default network. Persistent database data survived, but
the service was unavailable. Docker logs did not retain the initiating process,
so exact attribution is unknown; an unsafe test path that reached host Docker
was identified. These rules exist to prevent recurrence.
