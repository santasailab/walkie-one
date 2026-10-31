# Working in Walkie One

Read README.md, then the guide for the component you are changing.

- Server setup or configuration: personal-server/README.md.
- Helping a first-time user: follow personal-server/README.md's first-time setup,
  including download approval, human-only secret entry and separate check results.
  The single copy/paste setup prompt lives in docs/ai-setup.md; link to it rather than
  maintaining another version.
- Transport, routing, or audio formats: personal-server/PROTOCOL.md and schema/events.json.
- Assistant or speech adapters: docs/extending.md and link-runtime/README.md.
- Board, provisioning, reset, or recovery: firmware/README.md.
- Publication, licensing or dependency changes: docs/releasing.md, LICENSE,
  NOTICE.md and THIRD_PARTY_NOTICES.md.

Update the existing owning guide with behavior changes. Add a new document only
when it has a distinct reader and purpose; keep the root README as the index.
Preserve the minimal setup: one server, private configuration, and a connector.
Keep credentials and transcripts outside Git and out of tool/chat output.
Use generated fixture credentials for tests. A simulated device is not hardware
qualification; report those results separately.

Run ./scripts/check.sh before committing. Runtime changes need regression tests
at the affected boundary, especially authentication, device routing, steering,
approvals, cancellation, and duplicate delivery. Retain provider approval controls.
Public checks must run without company credentials or infrastructure.

The original material identified in NOTICE.md uses the Walkie One Open Community
License v0.2. Describe it as source available; preserve third-party terms.
Keep repository visibility unchanged during release preparation. Publishing source
or packages, deploying services, or changing repository access needs explicit
maintainer authorization. No deployment workflow belongs in the initial source
distribution.
