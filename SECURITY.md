# Security

This development snapshot has no supported production release yet. The current
branch receives fixes; hardware and Internet deployment still need qualification.

## Report a vulnerability

While the repository is private, contact a repository administrator through the
team's private channel. Share a minimal reproduction and affected commit without
live keys, recordings, transcripts, or customer information. Keep sensitive
findings out of issues and pull requests.

Before public release, maintainers must enable and test GitHub private
vulnerability reporting and name the responsible team. Do not assume a public
reporting channel exists until it has been verified.

## Trust boundaries

- Every handheld has a separate random credential. Link has its own credential;
  neither role can authenticate as the other. Device names are routing labels,
  not secrets. The server validates the authenticated sender before routing.
- Use WSS and trusted certificates. Plaintext is restricted to explicit loopback
  development. A tunnel may terminate TLS and carry your content; choose it as
  carefully as a speech provider. Expose only the server endpoint.
- Keep generated configuration outside the repository: directories mode 0700,
  files mode 0600. Share only the scoped profile each client needs. Provider keys
  stay on the server. Remove/rotate exposed credentials and restart affected
  processes; changing a file does not disconnect existing sockets immediately.
- Link runs trusted local assistant software with your operating-system account's
  permissions. A custom command is not sandboxed. Retain assistant approvals and
  grant access only to intended files/tools. The standalone launcher binds its
  local control listener to loopback with a fresh, unprinted authentication token.
  Missing local authentication denies access. Its per-server Unix socket is
  separate from other standalone Links and the consumer Mac app.
- Multiple devices share one trusted assistant context. This is not a tenant
  isolation boundary. The server prevents cross-device reply acknowledgment and
  steering, but participants can ask the same assistant about shared work.
- Connection, input, recording, response, and time limits reduce resource abuse.
  They are not a guarantee against denial of service. Keep network protections,
  dependency updates, and backups appropriate to your deployment.
- Transcripts are optional and private. Server delivery state is in memory;
  disconnects or restarts can lose replies. Never replay a command automatically
  when the assistant may already have acted.

## Repository checks

Run `./scripts/security.sh` after `./scripts/setup.sh`, with Gitleaks 8.30.1
installed. It scans both committed history and the current non-ignored source,
checks dependency advisories, and verifies that generated test credentials are
actually detected. The custom Gitleaks rule covers device and Link tokens;
default rules still cover common service credentials and private keys.

The source check rejects generated profiles even after they are renamed, and
rejects forced additions of private configuration and transcript files. CI uses
read-only GitHub permissions and does not retain checkout credentials in Git.
Runtime and test dependencies have exact versions and verified download hashes.

CI scans source/history for secrets, checks dependency advisories, and exercises
authentication, malformed input, device routing, TLS, steering, approvals, and
reconnect behavior with generated fixtures. These checks reduce risk; they are
not a claim that all vulnerabilities or hardware faults have been ruled out.
