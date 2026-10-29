# Walkie One

Run your own server. Choose your speech services. Connect your assistant.

Walkie One is a cellular handheld for talking to an AI assistant. This repository
contains the small Python server and standalone Link for an independent setup.
Your server authenticates your devices with credentials you generate locally;
it needs no company login, database, or control plane.

```text
Walkie One ── cellular / WSS ──► Your server ◄── WSS ── Standalone Link
                                    │                       │
                              Speech-to-text          Your assistant
                              Text-to-speech          Claude, Codex,
                                                      or your connector
```

**Development status:** the software can be exercised with simulated devices
today. Firmware for the new board, USB provisioning, cellular/audio checks, and
recovery need physical-device work. No installable firmware is released here.
The repository is private and the source-code license is pending.

## Try it without hardware

Use Python 3.12 on macOS or Linux:

```sh
git clone https://github.com/santasailab/walkie-one.git
cd walkie-one
python3.12 -m venv .venv
.venv/bin/python -m pip install -r personal-server/requirements.txt
.venv/bin/python personal-server/rehearse.py
```

The rehearsal starts the real server and Link on loopback, uses a harmless
fixture assistant and simulated audio, and verifies the reply and playback
receipt. It needs no account, API key, public server, or hardware. It cleans up
its temporary processes and files afterward.

Next, follow [the setup guide](personal-server/README.md) to configure your own
speech services, server address, and assistant. The consumer Mac app is not
required: Link is a standalone program included here.

## Find what you need

| Location | Purpose |
| --- | --- |
| [personal-server/](personal-server/README.md) | Server, private configuration, and local rehearsal |
| [link-runtime/](link-runtime/README.md) | Maintained Claude/Codex adapters and custom-command connector |
| `server-runtime/` | Shared speech adapters and conversation/delivery logic; no separate service |
| [Protocol](personal-server/PROTOCOL.md) and `schema/` | Device and Link messages, audio formats, and contract tests |
| [Extending](docs/extending.md) | Replace speech or connect another assistant |
| [firmware/](firmware/README.md) | Hardware integration and recovery checklist |
| [Security](SECURITY.md) | Trust boundaries and vulnerability reporting |

Multiple handhelds can share a server and one assistant conversation. Replies go
to their requesting device. Claude/Codex follow-ups can steer that device's
active turn; another device cannot steer it. This is a trusted personal group,
not isolated accounts for unrelated customers. Delivery is in memory: a server
restart can lose an unheard reply, and uncertain commands are never replayed.

Python is the maintained reference implementation. Other languages can implement
the same protocol; separate JavaScript or Java servers are not maintained here.

## AI-assisted setup

Give your coding assistant this repository and the following prompt:

> Read AGENTS.md and personal-server/README.md. Help me run the local rehearsal,
> then configure my own server, speech services, and standalone Link. Reuse the
> maintained runtime and its handling of steering, busy agents, approvals,
> reconnects, and playback receipts. Keep secrets in private files and never
> ask me to paste them into chat. Explain each command and expected result.
> Use the existing guides rather than creating more README files. Report
> simulated, live-provider, public-network, and physical-device results
> separately. Stop at the documented hardware boundary; do not invent flashing
> commands. Get my agreement before creating paid services or exposing a server.

All setup steps also have ordinary developer commands; an AI is optional.

## Development and maintenance

```sh
./scripts/setup.sh
./scripts/check.sh
```

The team maintains the official edition. Forks will be the normal customization
path once a license is selected and the source is published. Proposed upstream
changes are accepted at the team's discretion; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

**No open-source license has been granted yet.** The team will select a license
before making this repository public. Until then, this is a private development
repository, not a public open-source release. Third-party components retain
their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Maintainers should complete [the publication checklist](docs/releasing.md)
before changing visibility.
