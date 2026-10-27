# Environment

- This is a Kali container prepared for authorized CTF competitions. Common command-line
  security tools are installed.
- The current directory is the task workspace. Use it for command logs, large scan results,
  and other working files.
- Cairn also supports general-purpose tasks. The CTF guidance below applies when the assigned
  task uses this competition environment; follow the current task's goal and scope.

# Challenge levels

- Level 1 resembles security response center work, automated crowdsourced testing, and
  common vulnerability discovery. Explore the available evidence. Use the Playwright headless
  browser when needed; `playwright-cli --help` lists its commands.
- Level 2 focuses on known CVEs, cloud security, and AI infrastructure vulnerabilities.
  Relevant proof-of-concept examples and tools may be available in:
  - `/home/kali/.local/nuclei-templates`
  - `/home/kali/pocs`
  - `/home/kali/tools`
  - `/home/kali/knowledges`
- Level 3 simulates several network layers and requires planning across multiple steps and
  maintaining authorized access between them.
- Level 4 simulates an enterprise internal network with Active Directory. Available tools may
  include the `/usr/bin/impacket-*` commands, `chisel-common-binaries`, and `proxychains`.

## Chisel

Chisel binaries are under `/usr/share/chisel-common-binaries`.

# Callback addresses and listeners

The external IP for this container is **not configured in this file**. Obtain the address
provided for the current environment before arranging an authorized callback or external
listener. Use that configured address for reverse-shell listeners, out-of-band callbacks,
XSS receivers, or HTTP services used during authorized SSRF or XXE tests. Do not use the
placeholder text as an address or assume that every listening port is externally reachable.

# Tools and persistent processes

Common Kali tools such as `nuclei` and `ffuf` are available. Check their command help when
needed.

Use a `tmux` session for a process that must keep running or be shared with a later stage,
such as an HTTP service or listener. Record the session name and purpose in the task's final
findings so the next worker can locate it.
