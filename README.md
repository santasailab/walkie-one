# Walkie One

**Make it yours.**

We're building Walkie One to be something you love using every day. We also
believe you should be able to make it personal: connect the assistant you prefer,
choose your speech services, and adapt it to the things you want to do.

This repository helps you build that setup, starting with a small, dedicated
server for your Walkie One. Run it on your Mac, a Mac Mini, or a hosted computer.
Use the maintained software to get started, then change the parts you want to
make your own.

## What is included

| Component | What you get |
| --- | --- |
| Personal server | A small, self-hosted server for device connections, speech and assistant replies. It runs independently of our hosted service. |
| Standalone Link | The core Link runtime used by our macOS app, adapted to run on its own with your server and assistant. The native Mac app is not included. |
| Protocol, tests and guides | The device and Link interfaces, a simulated-device rehearsal, and instructions for changing your setup. |
| Firmware | An integration checklist for now. Firmware source, board-specific installation and recovery are a separate release milestone. |

Our hosted service uses a separate backend. This repository does not include its
Azure deployment, account system, control plane or database-backed delivery.
The personal server needs none of that infrastructure. It supports live
conversations, but has no durable background notifications or managed firmware
updates. Pending replies are lost when the server restarts.

## Get started

**Set it up with your AI.** Give it our [setup prompt](docs/ai-setup.md).
It will check the tools you need, explain downloads before asking to install
them, and guide you through speech services and getting your server online.
You handle account sign-ins and enter your keys privately.

**Set it up yourself.** Follow the [setup guide](personal-server/README.md)
for the same walkthrough, with commands and expected results.

Both paths start with a simulated device, so you can try the server and Link
on your computer without hardware, speech keys or paid services.

## How it works

Walkie One is a cellular handheld for talking to an AI assistant. With this
setup, it connects to your server, which turns your voice into text. **Link**,
the included connector, passes that text to your assistant. The assistant's
reply comes back through the server as speech for your Walkie One.

```text
Walkie One ◄── cellular ──► Your server ◄──► Link ◄──► Your assistant
                               │
                         Speech-to-text
                         Text-to-speech
```

The server runs as one Python process with a private configuration file. It
needs no database or company login. Device and Link connections use encrypted
WebSockets (WSS) over the internet, and each device has its own credential.

Link includes Claude and Codex adapters and runs independently of the consumer
Mac app. You can use it as supplied, connect another assistant, or build your
own connector. Your speech services are replaceable too.

## Go further

The team maintains the Python implementation. If you want to change how your
setup works, these guides explain the parts and the connections between them.

| What you want to do | Where to start |
| --- | --- |
| Change speech services or connect another assistant | [Extending your setup](docs/extending.md) |
| Understand or adapt the included connector | [Link guide](link-runtime/README.md) |
| Build a compatible server or connector in another language | [Protocol reference](personal-server/PROTOCOL.md) |
| Prepare for device integration | [Firmware checklist](firmware/README.md) |
| Secure your setup or report a vulnerability | [Security](SECURITY.md) |
| Change the code, run tests or propose a contribution | [Contributing](CONTRIBUTING.md) |

## Current status

The server and Link can be used with simulated devices today. Firmware, device
provisioning and recovery still need hardware work; no installable firmware is
included yet.

Software checks use simulated devices and assistants. They do not establish
live-provider compatibility, cellular connectivity or physical-device readiness.
See the [publication checklist](docs/releasing.md) for release requirements.

## License

The original material identified in [NOTICE.md](NOTICE.md) is available under
the [Walkie One Open Community License v0.2](LICENSE). You can study, modify,
share and use it, including for personal builds, subject to that license.

The license permits up to **100 covered hardware units sold in total per maker
or business group**, across releases and modified versions. Written commercial
permission is required before sale 101. The allowance does not reset annually;
binding preorders count. Software-only distribution, software sales and hosting
without supplying hardware are outside that hardware-sales allowance. See the
license for the complete definitions and conditions.

This is **source available under a community license**, not an unrestricted
OSI-approved open-source license. Third-party components keep their own
[licenses and notices](THIRD_PARTY_NOTICES.md). The license provides no company
cloud access, cellular service, provider account or branding rights.
For commercial permission, contact [contact@walkieone.com](mailto:contact@walkieone.com).
