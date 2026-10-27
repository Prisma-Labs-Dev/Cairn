# Worker container

The worker image provides the CLI tools and Kali environment used by container-mode Cairn
workers. The dispatcher creates one environment per standalone project. Workflow stages
share a workflow environment so later stages can use files produced earlier.

Build a local image from this directory:

```bash
cd container
docker build . -t cairn-worker-container
```

Then set `container.image: cairn-worker-container` in the dispatcher configuration. The
repository's default example uses the published upstream image instead:

```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

Building here uses this checkout's [AGENTS.md](AGENTS.md). Pulling the published image does
not rebuild it with this fork's translated instructions. The file describes the upstream CTF
environment; generic research and production tasks should supply their own goal and context.
Its external callback IP placeholder must be configured for the actual environment before use.

Container execution requires Docker and provider configuration for the selected workers.
The dispatcher controls process launch, claims, heartbeats, and result writeback. Agent
processes do not call the Cairn API directly. The original container setup includes broad
security tooling; choose network mode and capabilities for the authorized task.

See the [main setup guide](../README.md#getting-started),
[dispatcher specification](../docs/specs/dispatcher-design.md),
[core change record](../docs/core-changes.md), [usage capture](../docs/usage.md), and
[workflow workspace rules](../docs/workflows.md). Host-local execution does not use this image.
