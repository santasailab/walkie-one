# Set up Walkie One with your AI

Copy the prompt below into an AI assistant that can work on your computer.
It will guide you through the same steps as the [manual setup guide](../personal-server/README.md),
starting with a simulated device. No coding experience or hardware is needed
for this first stage.

The AI will explain downloads and ask for approval before installing software
or making your server public. You handle account sign-ins, billing and private
key entry directly. While this repository is private, your GitHub account needs
access to clone it.

## Copy this prompt

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

[Back to Walkie One](../README.md)
