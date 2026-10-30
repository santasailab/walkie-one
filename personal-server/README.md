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

## First-time setup on a Mac

An AI can walk through this guide with you using the
[single setup prompt](../docs/ai-setup.md). You can also follow
the commands yourself. Start here even if Git and Python are not installed.
Nothing in our setup scripts installs system packages, creates provider accounts,
opens a public tunnel, or touches a handheld automatically.

### Understand the downloads first

| Software | Why it is needed | Where it goes / when to approve |
| --- | --- | --- |
| Git | Downloads this repository | Reuse existing Git; otherwise approve Apple's Command Line Tools or your package manager's Git installation |
| Python 3.12 | Runs the server and Link | Reuse 3.12 or approve a separate installation; leave macOS's Python alone |
| Homebrew, if needed | Installs missing tools on a supported Mac | Optional package manager installation; explain its own dependencies and any administrator prompt first |
| Pinned Python packages | FastAPI serves requests, Uvicorn runs it, websockets connects Link; the lockfile includes their dependencies | Approve downloads into this checkout's `.venv`; no global pip install |
| cloudflared, later | Forwards a public HTTPS/WSS address to this Mac | Separate optional download; installing it does not start a public tunnel |
| An assistant CLI, later | Runs your chosen assistant through Link | Reuse an installed CLI or approve its documented prerequisites separately |

The AI should group related downloads into one clear approval request, reuse
what is installed and ask again only if the scope changes. Also ask before
provider charges, public exposure, DNS changes or background services. Account
login, billing, OS passwords and secret entry are yours to do directly; do not
paste them into chat. If an AI cannot run commands, it should explain one step
at a time and wait for your result rather than claiming it ran them.

### Check and install missing tools

Check the Mac version and architecture, then find existing tools:

```sh
sw_vers
uname -m
command -v git
command -v python3.12
command -v brew
command -v cloudflared
```

A missing command is expected on a fresh Mac. If Git resolves to `/usr/bin/git`,
check `xcode-select -p` first: invoking Apple's Git without Command Line Tools
may open an installer. After approval, `xcode-select --install` opens Apple's
installation dialog. Finish it before continuing. See
[Apple's Command Line Tools guide](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/).
Then verify `git --version` and, when present, `python3.12 --version`.

If Python 3.12 is missing and Homebrew is already working, after download approval:

```sh
brew install python@3.12
python3.12 --version
```

If Homebrew is also missing, first check its
[current Mac requirements and installation instructions](https://docs.brew.sh/Installation).
Explain the official installer's changes and obtain approval before running it;
the user handles any administrator prompt. Follow its printed PATH instructions,
then install [Python 3.12](https://formulae.brew.sh/formula/python@3.12).
Do not run blanket upgrades or replace system Python. If this Mac is unsupported,
stop and choose a supported machine or an official compatible Python installation
instead of forcing an installer. Linux users can use their distribution's Git
and Python 3.12 packages, then follow the same commands below. This walkthrough
has not qualified Windows; don't translate Unix permission commands blindly.

### Download the source

In Terminal, choose a folder for the source that does not already contain a
checkout named `walkie-one`, then run:

```sh
git clone https://github.com/santasailab/walkie-one.git
cd walkie-one
```

While the repository is private, GitHub access is required. If cloning fails,
sign in through your normal GitHub credential manager or request access from the
maintainers. Never put a token in the clone URL or paste one into an AI chat.
Do not overwrite an existing checkout to make the clone work.

## Try it locally first

From the repository root, with Python 3.12 on macOS or Linux (macOS may ship
an older `python3`), approve the pinned dependency download, then run:

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
Wait for the rehearsal's success message before continuing. If it fails, fix
that local failure before adding accounts or networking. You can stop here and
resume later; no speech key or internet-facing server is needed for this stage.

For the complete software test, run `./scripts/setup.sh` and `./scripts/check.sh`.
This includes two simultaneous standalone Links, two handheld identities,
an HTTPS speech fixture, credential rejection, and failure/reconnect scenarios.
It uses temporary private state and cleans up its processes. Tests do not need
your existing Mac app, assistant accounts, or speech keys.

## Set up your real server

For the simplest supported speech setup, use your own Deepgram account for both
speech-to-text and text-to-speech. You may instead connect your own HTTPS speech
service using the [speech contract](../docs/extending.md#your-http-speech-service).
Neither option uses company speech credentials or a company-service fallback.

For Deepgram, sign in directly to the [Deepgram Console](https://console.deepgram.com/).
In your project, open Settings → API Keys and create a key for this server,
following [Deepgram's key guide](https://developers.deepgram.com/docs/create-additional-api-keys).
Use permissions for speech requests, not account administration; choose an expiry
and keep the key in your password manager. Review your provider's balance and
billing yourself. The AI should wait while you do this and must not inspect a
page or screenshot containing the key. No Deepgram SDK or CLI download is needed.

Open **your own Terminal**, go to this checkout, and run:

```sh
.venv/bin/python personal-server/onboard.py configure --directory "$HOME/walkie-private"
```

Paste the key into the hidden prompt and press Enter. Characters will not appear;
that is expected. Do not put the key in the command itself. Tell your AI only
whether the command reported success. The helper refuses piped input and will
stop if the terminal cannot hide your key. It makes no network request.

For your own speech service, use the same command with `--provider http`; enter
the HTTPS service URL, then its token in the hidden prompt. Advanced users can
set STT and TTS independently in the private configuration; see the speech contract.

On first run this creates three private files. Keep the directory outside every
Git checkout; the helper enforces that. The directory has owner-only permissions
(0700) and its files are owner-only (0600):

| File | Who needs it |
| --- | --- |
| `server.json` | Only the server owner; contains speech settings and client credentials |
| `link.json` | Standalone Link or your own connector; contains only its address and credential |
| `walkie-1.json` | The device provisioning step; contains only its address and credential |

Rerunning `configure` preserves device/Link credentials and the server address;
Enter at the hidden prompt keeps the saved key. Choosing a different provider
replaces the speech settings for both directions. Restart a running server
after changing them. The underlying `server.py init` command remains available
for developers who prefer to configure JSON manually.

```sh
.venv/bin/python personal-server/server.py check --directory "$HOME/walkie-private"
```

This validates configuration without calling a provider. To check the actual
speech APIs, first approve **two potentially billed requests**: 0.1 seconds of
silence for STT and the fixed sentence “Your Walkie One server is ready.” for TTS.
Then run:

```sh
.venv/bin/python personal-server/onboard.py check-speech \
  --directory "$HOME/walkie-private" --approve-provider-requests
```

Two PASS messages mean the APIs accepted the requests and their responses met
the adapter's format checks. This does not measure recognition accuracy or
speaker playback. An empty transcript from the silent test is normal. Without
the approval flag, the command refuses to contact providers. A failure reports
which speech direction needs attention without showing keys or response bodies.

Start the server in a terminal you can leave open:

```sh
.venv/bin/python personal-server/server.py run --directory "$HOME/walkie-private"
```

`run` stays in the foreground on `127.0.0.1:8787`. In another terminal at this
checkout, verify the real WebSocket connection:

```sh
.venv/bin/python personal-server/onboard.py check-network \
  --directory "$HOME/walkie-private" --local-port 8787
```

This temporarily connects as the first device, rejects anonymous device/Link
connections, and checks the server's presence message. Run it before connecting
that physical device or another simulator using its profile. It sends no audio
or assistant command. “Link: not connected yet” is normal at this point.

`curl http://127.0.0.1:8787/healthz` only checks that
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

For a temporary bench test, obtain approval for the download, then install `cloudflared` using
[Cloudflare's instructions](https://developers.cloudflare.com/tunnel/downloads/)
(on an existing supported Homebrew installation: `brew install cloudflared`).
Explain that the next step exposes this server to the internet through Cloudflare
and obtain separate approval to start it. Then run, in another terminal:

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
address. Verify the public WSS route from this computer:

```sh
.venv/bin/python personal-server/onboard.py check-network --directory "$HOME/walkie-private"
```

PASS means TLS was verified, anonymous connections were rejected, and this
computer connected through the saved public address using a device credential.
It does not prove cellular reachability or speech/assistant execution. The probe
refuses redirects; a login page is not a device endpoint. Do not disable TLS or
authentication to fix a failure. Keep the server, tunnel and internet running.

Quick Tunnel addresses change when restarted and have testing limits;
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
profile during software testing. An AI should explain and obtain approval for
any missing CLI/prerequisite installation. The user signs in directly, without
sharing account tokens in chat. After Link starts, repeat `check-network`:
“Link: connected” confirms the connector's socket, not a successful agent turn.
For another assistant use the
[custom command adapter](../docs/extending.md#custom-command-adapter),
or write a connector in any language using the [protocol](PROTOCOL.md).
The assistant may run on this computer or another machine. No public command
execution endpoint is required. Keep the assistant's own permission checks on.

## Stop here, resume later

Hardware is not part of this setup. You can stop after the local rehearsal, after
configuring speech, or after checking your internet connection. Keep a short
record of which checks passed; skipped steps remain untested. The local rehearsal
uses fixture speech and a fixture assistant even if real credentials exist.

For an online setup, keep the server, tunnel and (when used) Link terminals open
and the Mac awake. Ctrl-C in each terminal stops that process; stopping the
tunnel removes public reachability. Nothing here installs a background service,
changes sleep settings, or runs at login. You can close the rehearsal terminal
as soon as it finishes; it already cleans up its processes.

To resume, open the checkout, start the server, start a new tunnel, save its new
HTTPS address with `set-origin`, restart the server, then start Link with the
updated profile. Run `check-network` again. Do not rerun `init`, erase the private
directory, or rotate credentials just to restart. For a stable address, choose a
named tunnel/domain using the provider's guide above; review account, DNS and
any billing changes before making them. Do not call the temporary tunnel a
permanent deployment.

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

Every command above is usable without an AI. To delegate the same work, use
the [single setup prompt](../docs/ai-setup.md).

To build a different implementation:

> Read personal-server/PROTOCOL.md and schema/events.json. Implement the same
> device and Link interfaces in my chosen language. Preserve authentication,
> device isolation, bounded audio, playback receipts and reconnect behavior.
> Test with two simulated devices and the maintained Link before using hardware.

Python/FastAPI is the maintained reference implementation. Node, Go, Rust or
another language can implement the same protocol; they are not separately
maintained servers supplied by this repository.
