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

## Set it up with your AI

No coding experience is needed to follow the guided path. Give an AI that can
work on your computer the prompt below. It will check for missing tools, explain
the downloads, and wait for your approval before installing anything. You handle
account sign-ins and enter your speech key in a hidden Terminal prompt.

This is a guided setup, not an unattended installer. Git downloads the source;
Python and the pinned packages run it; an optional tunnel makes it reachable
online. Speech and assistant providers may charge for usage. Start with the
free local simulation, then decide whether to connect real services.
While this repository is private, your GitHub account needs access to clone it.

Copy this entire prompt:

```text
Help me set up my own Walkie One server on this computer. I have no coding
experience. Source: https://github.com/santasailab/walkie-one
Leave all hardware, firmware and flashing for later.

1. Check my operating system, Git, Python 3.12 and existing package manager.
   Follow the repository's personal-server/README.md first-time setup guide.
   Explain any missing downloads, their purpose, installation location and
   administrator access needed. Ask for approval before installing tools or
   Python dependencies. Reuse working installations. Do not replace system
   Python, run blanket upgrades, or bypass permissions. If you cannot run
   commands, guide me through one command at a time and wait for the result.
2. Clone the source into a new folder, then read AGENTS.md and the setup guide.
   If private-repository access fails, help me sign in directly or get access;
   never ask for a password/token in chat. Create the isolated .venv, install
   the hash-pinned runtime requirements and run rehearse.py. Continue only
   when it passes. Keep the maintained server, Link and edge-case handling.
3. Guide me through choosing speech services. Default to the supported
   Deepgram setup unless I choose my own HTTPS speech service. I handle
   signup, billing and key creation. Have me run onboard.py configure in my
   own Terminal to enter the key privately. Never read, print or request my
   secrets, private profiles or screenshots containing them. Run the local
   configuration check. Ask before the two potentially billed speech checks.
4. Start the server on loopback and verify it with onboard.py check-network.
   Explain the temporary Cloudflare tunnel, its traffic handling and changing
   address. Ask before installing cloudflared and before exposing the server.
   Follow the guide to save the public origin and verify authenticated WSS.
   Keep TLS verification on. A health page alone is not a successful test.
   Offer a stable tunnel/domain for ongoing use; ask before account, DNS,
   service or billing changes. Do not turn on background startup automatically.
5. Help me connect the standalone Link to my chosen installed assistant, or
   pause here if I only want the server prepared. Explain any additional CLI
   downloads and get approval. I handle assistant sign-in; keep its normal
   permission checks. Use the documented adapters, not a new command runner.
6. Tell me what passed: local simulation, speech APIs, public WebSocket access
   and assistant connection, separately. Mark skipped checks as untested.
   Explain what must keep running and how to stop/restart it. Stop before
   hardware. Use the existing docs; don't add a database or extra services.
```

Prefer doing it yourself? The [same setup guide](personal-server/README.md)
includes each command and its expected result.

## Try it without hardware

Use Python 3.12 on macOS or Linux:

```sh
git clone https://github.com/santasailab/walkie-one.git
cd walkie-one
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r personal-server/requirements.txt
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

## Development and maintenance

```sh
./scripts/setup.sh
./scripts/check.sh
```

The suite also exercises two independent Links on one computer and a local
HTTPS speech service with a temporary certificate. It checks routing, steering,
permissions, reconnects, malformed input, and rejection of untrusted TLS.
All assistants and speech responses in these tests are fixtures.

Run `./scripts/security.sh` with Gitleaks 8.30.1 installed to audit dependencies
and scan the Git history and current source. CI runs these checks automatically.
Generated profiles and credentials must stay outside the repository.

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
