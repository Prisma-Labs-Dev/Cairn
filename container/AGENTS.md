# Worker environment

This is the optional source-built Wayline worker image. It provides Python, Node,
git, curl, jq, ripgrep, fd, and the Pi, Codex and Claude CLIs. The current directory
is the assigned task workspace. Workflow stages share it.

Follow the assigned goal and its explicit filesystem/network scope. Save useful
findings and required artifacts in the workspace before reporting completion.
Do not assume a host, callback address, provider credential or external integration
is configured. Use only the access and targets explicitly supplied for the task.

This image is a general-purpose CLI environment. It does not include the original
upstream Kali security suite, vulnerability repositories or cloud-provider CLIs.
