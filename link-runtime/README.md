# Standalone Link

Link keeps an outbound WebSocket connection to your personal server, passes text
requests to an assistant, and returns its replies. It reuses the maintained
Claude and Codex runtime behavior without requiring the consumer Mac app or a
company login.

Start with [the server setup](../personal-server/README.md). Install and sign in
to the chosen assistant's CLI using its own instructions, then run:

```sh
.venv/bin/python link-runtime/independent.py \
  --profile "$HOME/walkie-private/link.json" --agent codex --check
.venv/bin/python link-runtime/independent.py \
  --profile "$HOME/walkie-private/link.json" --agent codex
```

Choose `claude` for Claude. The private profile contains only the WSS URL and
Link credential. `--check` validates it without starting an assistant or making
a network connection. `--development` permits `ws://127.0.0.1` only for local
testing. Live provider login and compatibility still require a separate check;
fixture tests do not verify your installed provider version.

The launcher creates a separate workspace, recent speech state, outbox, and task
directory for each server URL under `~/.relay/servers/`. A server switch therefore
does not replay another server's background work. Provider-owned history and
files created by the assistant remain separate.

Each server also gets its own private control socket under `~/.relay/ipc/`.
Two standalone Links can run beside the consumer Mac app without replacing its
socket. Run only one Link for a given server profile.

Use the standalone launcher, rather than running `link.py` directly: it validates
the profile, sets local state, and protects the loopback control listener. The
listener uses an ephemeral port and a fresh unprinted token. No inbound Internet
port is needed on the assistant computer.
If the runtime is started directly without a local token, HTTP status and
assistant-control requests are rejected.

## Behavior retained from the maintained runtime

- Claude/Codex follow-ups steer the active operation when supported. Ambiguous
  delivery is not retried as a new command. Another handheld cannot steer it.
- Provider questions and tool approvals remain bound to the exact request and
  device. Spoken approval requires the matching question playback receipt.
- Duplicate results and playback acknowledgments do not rerun assistant work.
- Disconnect, cancellation, and shutdown clean up provider processes. Normalized
  errors keep provider output and credentials out of device messages.

The personal server supports one shared assistant conversation. It rejects
unsolicited notifications because it has no durable delivery service. Some
shared runtime helpers support richer servers; their presence does not make
those capabilities available on the minimal server.

## Extend it

Use the [custom command adapter](../docs/extending.md#custom-command-adapter) for
another assistant, or implement the [Link protocol](../personal-server/PROTOCOL.md)
in your application. A simple command adapter supports final replies and ordinary
questions; full steering and provider approvals need an `AgentEndpoint` adapter.

Run `.venv/bin/python -m pytest link-runtime/tests` for runtime checks, and
`.venv/bin/python personal-server/rehearse.py` for the real server/Link rehearsal.
The optional `.venv/bin/python link-runtime/agent.py --demo` self-check uses
private temporary state and preserves your existing workspace and speech history.
