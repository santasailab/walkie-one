# Your Walkie One, one Python server

Run a small server on a Mac Mini, another computer, or a hosted machine. It
connects your cellular handhelds to your assistant and your speech providers.
One Python process, a private configuration file, and no database, Docker,
company login, control plane or company SIM entitlement check.

```text
Handhelds ── cellular / WSS ──► personal server ◄── WSS ── assistant connector
                                    │                       (standalone Link,
                                    ├─ speech-to-text        your app, or
                                    └─ text-to-speech        your own program)
```

**Software preview:** the local rehearsal exercises the real server and Link
with simulated speech and a simulated handheld. The new cellular board's
server/APN/trust installer still needs implementation and hardware qualification.
The walkthrough does not claim that a physical kit can be connected yet.

## Try it locally first

From the repository root, with Python 3.12 on macOS or Linux (macOS may ship
an older `python3`):

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r personal-server/requirements.txt
.venv/bin/python personal-server/rehearse.py
```

The rehearsal starts a loopback server, runs the **real Link runtime** with a
harmless fixture assistant, sends simulated microphone audio, receives reply
audio, and acknowledges playback. It stops its processes and removes its
temporary files afterward. It uses no accounts, paid providers or external
hosting. The generated sound is a fixed test asset; it is not a real TTS result.
This is a plumbing test, not a cellular, assistant-provider or microphone test.

For the complete software test, run `./scripts/setup.sh` and `./scripts/check.sh`.
This includes two simultaneous standalone Links, two handheld identities,
an HTTPS speech fixture, credential rejection, and failure/reconnect scenarios.
It uses temporary private state and cleans up its processes. Tests do not need
your existing Mac app, assistant accounts, or speech keys.

## Set up your real server

```sh
.venv/bin/python personal-server/server.py init --directory "$HOME/walkie-private"
```

This creates three private files. Keep the directory outside the repository:

| File | Who needs it |
| --- | --- |
| `server.json` | Only the server owner; contains speech settings and client credentials |
| `link.json` | Standalone Link or your own connector; contains only its address and credential |
| `walkie-1.json` | The device provisioning step; contains only its address and credential |

Open `server.json` in a local editor. The supplied speech choice is Deepgram;
enter your own key under `speech.DEEPGRAM_API_KEY`. Both speech selectors can independently be `deepgram` or
`http`. For your own HTTP speech implementation, use `WALKIEONE_SPEECH_URL` and
`WALKIEONE_SPEECH_TOKEN` as described in the [speech contract](../docs/extending.md#your-http-speech-service).
The key stays on this server. There is no company-service fallback.

```sh
.venv/bin/python personal-server/server.py check --directory "$HOME/walkie-private"
.venv/bin/python personal-server/server.py run --directory "$HOME/walkie-private"
```

`check` validates configuration without calling a provider. `run` stays in the
foreground on `127.0.0.1:8787`. `curl http://127.0.0.1:8787/healthz` checks that
the process is running; it does not prove speech or assistant availability.
Missing speech credentials fail startup instead of pretending to work.
Keep the computer awake and both this process and its internet connection running.

## Make it reachable from cellular

The handheld cannot reach your Mac's private `192.168.x.x` address from the
cellular network. It opens an outbound connection to your **public WSS address**.
It needs no public/static cellular IP or incoming port on the SIM.

For a home Mac, a tunnel forwards a public HTTPS/WSS address to
`http://127.0.0.1:8787`. The tunnel agent makes an outbound connection from your
Mac, so home router port forwarding is unnecessary. The personal server still
authenticates every device and Link. A tunnel operator carries the traffic and
may terminate TLS; choose it with the same care as a hosting provider.

For a temporary bench test, install `cloudflared` using
[Cloudflare's instructions](https://developers.cloudflare.com/tunnel/downloads/)
and run, in another terminal:

```sh
cloudflared tunnel --url http://127.0.0.1:8787
```

This follows [Cloudflare's Quick Tunnel setup](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).
Use the HTTPS address it prints:

```sh
.venv/bin/python personal-server/server.py set-origin \
  --directory "$HOME/walkie-private" --origin https://YOUR-HOST.trycloudflare.com
```

Restart the personal server and reimport client profiles after changing the
address. Quick Tunnel addresses change when restarted and have testing limits;
use a [named tunnel and your domain](https://developers.cloudflare.com/tunnel/get-started/)
for ongoing use. A browser login challenge in front of these endpoints prevents
the firmware and Link from connecting. The server's scoped credentials provide
their authentication. Test the provider's WSS forwarding and the board's TLS
trust before use. No tunnel account or public endpoint is created by our scripts.

A public hosted machine can instead expose TLS directly:

```sh
.venv/bin/python personal-server/server.py run --directory "$HOME/walkie-private" \
  --host 0.0.0.0 --port 8443 --cert /private/fullchain.pem --key /private/server.key
```

Use a trusted certificate, put the matching port in `--origin`, allow that port
through the host firewall, and renew the certificate. Plaintext binding outside
loopback is refused. A reverse proxy on the same host is another option. A VPS
or tunnel can be changed without depending on Santa AI Lab. These are alternatives;
the basic setup does not require deploying both.

## Connect your assistant

Run the standalone Link from the terminal. The consumer Mac app is not required.
Sign in to your chosen assistant using its own installed CLI first.

```sh
.venv/bin/python link-runtime/independent.py \
  --profile "$HOME/walkie-private/link.json" --agent codex
```

Use `claude` for Claude. Add `--development` only for a loopback `ws://127.0.0.1`
profile during software testing. For another assistant use the
[custom command adapter](../docs/extending.md#custom-command-adapter),
or write a connector in any language using the [protocol](PROTOCOL.md).
The assistant may run on this computer or another machine. No public command
execution endpoint is required. Keep the assistant's own permission checks on.

## Prepare the handheld

Use `walkie-1.json` for its WSS address and device credential. The complete board
profile also needs your carrier's APN and a CA certificate that trusts the TLS
front door. Your SIM must have working data service. Do not paste server.json or
the Link credential into firmware, and never disable TLS certificate validation.

**Hardware stop point:** this file does not install anything on the board.
The legacy USB credential writer does not install the complete endpoint/APN/trust
profile. Follow the [firmware checklist](../firmware/README.md) for that installer.
Before replacing a device image, retain its current image/configuration and use
the qualified board-specific procedure. Keep the company configuration available
for returning to it; changing this server does not edit company records.

The first hardware check: establish cellular data, install the complete profile,
connect through the public WSS address, then speak a harmless question. Confirm
the real STT text reaches the selected assistant, the correct handheld receives
real TTS audio, Listen plays it, and playback is acknowledged. Repeat after a
cellular reconnect, then with two devices. Check that neither device can receive
or acknowledge the other's answer. Retain redacted evidence, not credentials.

## Add a device, remove access, or keep transcripts

```sh
.venv/bin/python personal-server/server.py add-device \
  --directory "$HOME/walkie-private" --name walkie-2
.venv/bin/python personal-server/server.py remove-device \
  --directory "$HOME/walkie-private" --name walkie-2
.venv/bin/python personal-server/server.py rotate-link --directory "$HOME/walkie-private"
```

Restart after editing configuration or running these commands. Removal revokes
that device after restart. To rotate a device credential, remove and re-add it;
the configuration must retain at least one device, so add a replacement first if
necessary. Import the changed profile on the client. Files already copied to
another machine are not erased, but their old credentials no longer authenticate.

Up to sixteen configured handhelds share one trusted Link and assistant. This is
a household/personal trust group, not isolation between unrelated customers:
everyone uses that assistant's account and permissions. Replies are routed to
the originating device; devices share the assistant conversation. One assistant
turn runs at a time. Claude and Codex can receive follow-ups from the device
that started the active turn; another device receives a busy response. The first configured
device is primary. Two simultaneous sockets using one credential are rejected.

Set `transcripts` to `true` in `server.json` to save optional JSONL request and
generated-reply records. They rotate at 1 MiB, with three backups. They are not
an offline reply queue, and “reply_generated” does not mean “heard.” The existing
Link also maintains its local speech context. Recording is off by default on
this server. Stop the process and delete its `transcripts.jsonl*` files to clear
these records; provider-owned history is separate.

Connections, recordings, and pending audio live in memory. A device disconnect
discards its unacknowledged audio; a server restart loses unfinished turns and
unheard replies. It never automatically repeats a possibly executed command.
Check the assistant before retrying. Background notifications are explicitly
rejected because this server has no durable mailbox. Managed OTA offers,
company onboarding/claims and hosted-provider account flows are not included.
Qualified firmware and USB recovery are still pending. A future standalone
signed-update option can be added independently. This repository does not ship
the company account system or database-backed delivery service.

## Security without extra infrastructure

- Use WSS with valid certificates. Keep the default loopback listener behind
  your tunnel, or use the explicit TLS listener. Do not expose a plaintext port.
- Keep state mode 0700 and files mode 0600. Never commit profiles, show them to
  an AI chat, or include credentials in URLs/screenshots/logs. Transfer only the
  scoped profile a particular client needs. Keep speech keys on the server.
- Register each handheld separately. Remove compromised credentials and restart.
  A serial number is not authentication. A Link credential grants access to this
  personal trust group's traffic; share it only with your trusted connector.
- Keep connection, frame, capture and request limits enabled. The implementation
  bounds frames at 64 KiB, capture at 60 seconds of PCM/65 seconds wall time,
  individual writes at ten seconds, and assistant waits at 25 minutes. Rate
  limits cover authenticated traffic as well as reconnection attempts. They are
  not a guarantee against internet-scale denial of service; the front door can
  apply additional connection limits without replacing device authentication.
- Keep command execution in the assistant connector. It must not execute arbitrary
  shell text supplied by an unauthenticated request. Assistant tools can change
  files/accounts; retain their normal approvals and grant only intended access.
- Review speech and tunnel providers' handling of content, update pinned
  dependencies, and protect optional transcript files and backups. Architecture,
  message formats and security mechanisms are public; live keys, customer data
  and production secrets are private.

These controls follow [OWASP's WebSocket guidance](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html).
The checked-in test credentials are generated during tests. Nothing here
provides a credential for company services.

## Have your AI help—or do it yourself

Every command above is usable without an AI. To delegate the same work:

> Read AGENTS.md and personal-server/README.md. Set up the smallest personal
> Python server using its pinned requirements. Run the local rehearsal first.
> Use private files for credentials; never print or ask me to paste secrets.
> Help me choose a public WSS address, speech providers, and an assistant.
> Do not create a database, company account, or extra services. Use the existing
> docs instead of creating more README files. Stop for provider login and the
> physical board setup. Report simulated tests separately from real speech,
> tunnel/TLS, and cellular tests. Do not deploy or publish company infrastructure.

To build a different implementation:

> Read personal-server/PROTOCOL.md and schema/events.json. Implement the same
> device and Link interfaces in my chosen language. Preserve authentication,
> device isolation, bounded audio, playback receipts and reconnect behavior.
> Test with two simulated devices and the maintained Link before using hardware.

Python/FastAPI is the maintained reference implementation. Node, Go, Rust or
another language can implement the same protocol; they are not separately
maintained servers supplied by this repository.
