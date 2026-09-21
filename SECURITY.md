# Security

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use GitHub's private reporting instead:
**Security tab → Report a vulnerability**. Include what you found, how to reproduce it, and the version
(`readcue --version`, or the footer of the web UI).

## Deployment notes

readcue reads your course materials and can spend your Claude API key, so treat it like any other private service:
keep it on localhost, a LAN or a VPN; set `READCUE_PASSWORD`; and put it behind HTTPS if it leaves your network.
The app refuses to start when it is reachable beyond localhost without a password. See
[docs/deployment.md](docs/deployment.md).
