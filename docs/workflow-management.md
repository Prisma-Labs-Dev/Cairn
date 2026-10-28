# Workflow management

The home workflow cards and workflow detail view provide Pause, Resume, and Delete controls.
Pausing stops further stage dispatch and preserves the current work. Completed workflows can
be deleted but cannot be resumed.

Delete moves a workflow to recoverable Trash. The confirmation explains this before the
request is sent. Trash hides the workflow from the home list and pauses every unfinished
stage project. The dispatcher observes those paused projects and cancels their running worker
processes. Open exploration, reasoning, and review leases are invalidated.

Graphs, findings, downloaded artifacts, shared workspace files, token usage, completion claims,
and review history remain intact. A pending independent review remains pending, with its old
reviewer lease invalidated. Reading a trashed workflow and its saved results remains possible;
mutations require restoration first.

Restore returns unfinished workflows to the home list in the paused state. It does not run
workers or unlock new stages. Resume is an explicit separate action. Restoring a completed
workflow leaves it completed.

## API

| Request | Behavior |
| --- | --- |
| `GET /workflows` | List workflows outside Trash. |
| `GET /workflow-trash` | List recoverable workflows in Trash. |
| `GET /workflows/{id}` | Read a workflow, including `workflow.deleted_at` when trashed. |
| `PUT /workflows/{id}/status` | Pause with `{"status":"stopped"}` or resume with `{"status":"active"}`. |
| `DELETE /workflows/{id}` | Move to Trash and return its preserved detail. |
| `POST /workflows/{id}/restore` | Restore without resuming and return detail. |

Trash and restore are idempotent. Repeating Trash preserves the first deletion timestamp.
Repeating Restore on an already restored workflow does not change its current status.
Unknown IDs return 404. Workflow metadata, project producer writes, completion, and review
mutations return 409 while the owning workflow is in Trash.

The deletion timestamp is an additive SQLite migration. Trash, restore, completion, and
review decisions use immediate write transactions, so a concurrent completion either lands
before Trash pauses all remaining stages or fails after Trash is committed. Open claim checks
also acquire the write transaction before reading mutable lifecycle state.

The API does not permanently erase a workflow or remove its workspace.

## Verification

`uv run --project wayline --group dev pytest wayline/tests/test_workflow_management.py`

The focused suite checks list visibility, retained read access, mutation guards, idempotence,
paused and completed restoration, artifacts and usage retention, pending review leases,
concurrent completion and approval, migration, and real harmless subprocess cancellation.
The subprocess test retains an unfinished workspace even with standalone cleanup configured
to remove completed projects. It makes no model calls.
