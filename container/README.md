# Optional worker container

The supported first path is host-local Pi, described in
[installation](../docs/installation.md). It reuses Pi's own API-key or subscription
configuration. It does not need this worker image or Docker.

Container execution is optional and uses the image built from this checkout:

```sh
docker build -t wayline-worker:local container
```

Set `container.image: wayline-worker:local`, as shown in `dispatch.example.yaml`.
There is no upstream image fallback. The dispatcher and server application image
is also built from this checkout by `docker compose build`. `docker compose up`
starts only the server; the optional dispatcher profile is
`docker compose --profile container-dispatcher up` after configuring `dispatch.yaml`.
The dispatcher needs access to the Docker socket. Linux host networking in the
container example is platform-dependent and must be selected for the actual host.

The generic worker includes pinned Pi, Codex and Claude CLIs plus Python, Node,
git, curl, jq, ripgrep and fd. It runs as the image's unprivileged `node` user.
It replaces the old Kali image's mutable security-tool repositories, broad
capabilities, cloud CLIs and regional defaults. Add task-specific tools explicitly
when required. Instructions come from this checkout's [AGENTS.md](AGENTS.md).

Container workers require provider configuration in their dispatcher worker env.
The example contains placeholders, not credentials. Host Pi subscription credentials
are not automatically copied or mounted into containers. Authenticated container
provider execution and a containerized Pi viewer need separate configuration; the
host-local installation smoke does not establish either. The server image does not
bundle Go, Node or the optional viewer binary.
