# Preparing the public release

Prepare and review changes while the repository remains private. Its owner
performs the visibility change separately. Completing software tests does not
authorize changing visibility, publishing packages or releasing firmware.

## Software-only scope

The first source release can contain the personal server, standalone Link,
protocol, tests, and documentation while firmware remains a separate milestone.
Keep `firmware/README.md` as the integration checklist; do not add board images,
provisioning claims, or flashing commands until those paths are verified.
Keep the company backend, accounts, production configuration, and native app
out of this distribution.

Original material is covered by [LICENSE](../LICENSE), with explicit scope in
[NOTICE.md](../NOTICE.md). The Walkie One Open Community License v0.2 includes
a 100-unit hardware-sales allowance; use **source available** in release copy.
Preserve the full license and notices in every source archive. This adoption
does not change licenses on files distributed elsewhere, including website
downloads. Confirm rights and update the notice before importing new material.

## Repeatable readiness checks

From a fresh clone, run:

```sh
./scripts/setup.sh
./scripts/check.sh
./scripts/security.sh  # requires Gitleaks 8.30.1
```

The software checks cover the real server/Link with simulated handhelds,
replaceable speech over local HTTPS, multiple devices, two independent Links,
busy/steering behavior, provider approvals, playback receipts, and reconnects.
They include rejection of wrong-role credentials, untrusted certificates,
oversized/malformed messages, and missing local control authentication.
The security command scans all local Git history plus uncommitted source,
audits dependency advisories, and tests its own device/Link credential rules.

Record the commit, CI URL, commands, and outcomes in the release PR. Keep raw
logs and any sensitive reproductions outside Git. A passing simulated run
does not establish live-provider compatibility, public-network behavior, or
physical-device behavior; record those separately when tested.

## Publication gate

Before the team makes it public:

- Verify that LICENSE and NOTICE.md cover the exact source being published and
  that new contributions have the written permissions described in CONTRIBUTING.md.
- Verify the third-party inventory against the final lockfiles. Source archives
  exclude installed packages; any future bundled binary needs the full relevant
  third-party license texts and notices. Preserve the audio fixture's provenance.
- Review the complete Git history with Gitleaks and review file contents for
  credentials, customer data, production configuration, and private references.
  Removing a secret from the latest commit does not remove it from history.
- Run `./scripts/check.sh`, `./scripts/security.sh`, the clean-clone rehearsal, and CI
  without company credentials. Review any remaining advisory explicitly.
- Verify README commands and internal links from a fresh clone. Keep supported
  software behavior and pending hardware work clearly distinguished.
- Confirm the contact in SECURITY.md is monitored. GitHub private vulnerability
  reporting is a public-repository feature: enable and verify it when changing
  visibility, retaining email as a fallback. Check available secret scanning,
  push protection and dependency alerts at that point. Keep branch protection
  and CI's read-only token permissions. Document settings that remain unavailable
  while private; a passing local scan does not enable GitHub's protections.
- Obtain explicit maintainer approval for public visibility. Review the public
  website wording and repository links as a separate website change.

Before a firmware release, also complete [the board checklist](../firmware/README.md).
Store private signing keys outside Git/CI logs, publish verification material,
pin the toolchain, provide checksums, and rehearse recovery on the target board.
Tag only an actually verified release and describe what its tests cover.

Routine development uses branches and PRs. Dependency alerts and the CI audit
help identify updates; update locks and notices together. This repository has
no access to company production deployments and no package publishing workflow.

Repository controls follow [GitHub's security guidance](https://docs.github.com/en/repositories/creating-and-managing-repositories/best-practices-for-repositories).
