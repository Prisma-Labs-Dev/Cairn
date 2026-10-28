# Runtime concurrency controls

Open **Execution** in the UI to inspect connected dispatchers and adjust concurrency.
The controls govern new task dispatches. Saving settings does not start a dispatcher,
install a worker CLI, or create a model session.

## Limits and configuration

Three limits can override the dispatcher's loaded YAML configuration:

| Limit | What it controls |
| --- | --- |
| `max_workers` | In-flight bootstrap, reason, and explore tasks in one dispatcher |
| `max_running_projects` | Projects admitted for work by one dispatcher |
| `max_project_workers` | In-flight tasks from one project, including stage projects |

Each saved override is either an integer from 1 to 256 or `null`. A number overrides the
corresponding YAML value. `null` restores the value loaded from YAML when that dispatcher
started. Editing the YAML file on disk requires a dispatcher restart; the UI does not
rewrite it.

The saved policy applies independently to every connected dispatcher. It is **not a
cross-process global cap**. Two dispatchers with `max_workers: 4` may together run eight
tasks, subject to their own worker pools and project limits. The runtime API reports
`scope: "per_dispatcher"` to make this explicit.

## Worker pool constraints

Increasing `max_workers` does not create additional worker capacity. Every configured
worker retains its own `max_running`, allowed `task_types`, and health/rejection checks.
For example, a dispatcher limit of three with one worker whose `max_running` is one can
run only one task at a time. Workers limited to reasoning cannot execute exploration jobs.

The UI distinguishes configured YAML limits, desired limits after saved overrides, and
effective limits last reported by a dispatcher. It also shows nominal worker capacity,
capacity excluding known health cooldowns, and free worker slots. A worker without a
known cooldown is not a guarantee that its credentials or every task type will succeed.

## Live application and reductions

The dispatcher polls saved settings on each scheduling tick and publishes the limits it
actually applied. A saved revision remains pending until that dispatcher reports it.
With no connected dispatcher, settings remain saved for the next one that connects.

Increases can admit additional work on the next tick, including enlarging the execution
pool while earlier tasks continue. Reductions do not cancel or interrupt running agents.
If existing work exceeds a new limit, Wayline waits for it to drain before submitting more.
When the project limit decreases, only the allowed subset of projects keeps admission;
the others wait rather than continuing to receive new work under the old limit.

`running_tasks` and `running_projects` describe in-flight work. They do not count all
active projects or queued stages in the database. `admitted_projects` separately reports
the projects currently holding scheduling slots.

## Freshness

Every dispatcher has a process-specific ID and server-timestamped `last_seen`. Its
snapshot becomes offline/stale after the greater of 30 seconds or three configured poll
intervals. A stale snapshot remains visible temporarily, with its last reported values;
it is not proof that a dispatcher is still running or has applied a saved change.

Snapshots include worker names, types, task types, limits, and in-flight counts. They do
not include worker environment variables or API keys. Old process snapshots are removed
after a day when a new heartbeat is received.

## API

`GET /runtime` returns saved settings and dispatcher snapshots. With no dispatcher:

```json
{
  "scope": "per_dispatcher",
  "settings": {
    "max_workers": null,
    "max_running_projects": null,
    "max_project_workers": null
  },
  "settings_revision": 0,
  "dispatchers": []
}
```

`PUT /runtime/settings` accepts a partial settings object and returns the updated runtime
overview. For example:

```json
{"max_workers": 4, "max_running_projects": 2, "max_project_workers": 2}
```

Reset a single control without changing the others:

```json
{"max_workers": null}
```

Empty objects, unknown fields, booleans, numeric strings, zero, and out-of-range overrides
are rejected. Re-saving identical values does not increment the settings revision.

`GET /runtime/settings` is the small settings/revision response used by dispatcher polling.
`POST /runtime/dispatchers/{id}` is the internal heartbeat endpoint. Reports contain
`configured`, `effective`, `applied_settings_revision`, `stale_after_seconds`, `counts`,
and the safe `workers` list. The server adds freshness, desired limits, pending status,
and worker capacity calculations to `GET /runtime`.

The existing `GET /settings` and `PUT /settings` endpoints still control intent and reason
lease timeouts. Runtime concurrency controls do not change those lease settings.
