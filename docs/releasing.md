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
audits dependency advisories, runs Bandit static analysis, and tests its own
device/Link credential rules.

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

## Repository protections

Verify these controls in GitHub before making the repository public. Source files
describe the policy; read back the live settings to establish that it is enforced.

| Area | Required policy |
| --- | --- |
| `main` | Pull requests, one independent CODEOWNERS approval, latest-push approval, stale-review dismissal, resolved conversations, and up-to-date `Required checks` from GitHub Actions. Enforce for admins; block direct/force pushes and deletion; squash merges only. |
| Actions | Read-only default token; Actions cannot approve PRs. Require full commit SHAs and allow only `actions/checkout` and `actions/setup-python`. Private fork runs are disabled and receive no write token or secrets. Use GitHub-hosted runners and review any new action before expanding the allowlist. |
| Dependencies | Alerts and security-update PRs enabled, weekly version-update PRs for Python and Actions, and no automatic merging. Keep both Python locks and notices synchronized. |
| Release tags | `v*` creation limited to repository admins; a separate no-bypass rule blocks updating or deleting those tags, including by admins. |
| Releases | Immutability enabled for future releases. Assemble assets in a draft and publish only when complete. Correct a published release with a new version. |
| Community | Valid CODEOWNERS with two eligible maintainers, license/notice, contribution and conduct guides, issue/PR templates, and a monitored private security contact. |

Requiring signatures on every contributed commit is deferred until contributor
and bot signing workflows are supported. Required reviews and CI remain enforced.
Bandit is the static-analysis tool for this source-available release. CodeQL
requires a separate licensing check before adoption. Research and the primary
sources for these choices are in the
[public-readiness note](research/2026-10-31-public-readiness.md).

## Immediately after the owner changes visibility

GitHub private vulnerability reporting and the public-fork approval policy are
public-repository features. Free repository secret scanning and push protection
must also be enabled and verified after publication. The email reporting route
and local/CI secret scans remain available throughout preparation.

As a repository administrator, with the GitHub CLI authenticated, run:

```sh
python3 scripts/finish-public-security.py --apply
```

The command refuses a private repository, never changes visibility, and enables
only public secret scanning, push protection, private vulnerability reporting,
and workflow approval for **all external contributors**. It reads the settings
back and fails if any remain disabled. Run without `--apply` for verification
only. It does not enable paid private-repository security products or CodeQL.

Check that Security → Advisories offers **Report a vulnerability**, and that the
maintainers receive security notifications. Recheck main/tag protection and
Actions policy after the visibility transition. Resolve any failed setting
before announcing the repository.

Before a firmware release, also complete [the board checklist](../firmware/README.md).
Store private signing keys outside Git/CI logs, publish verification material,
pin the toolchain, provide checksums, and rehearse recovery on the target board.
Tag only an actually verified release and describe what its tests cover.

Routine development uses branches and PRs. Dependency alerts and the CI audit
help identify updates; update locks and notices together. This repository has
no access to company production deployments and no package publishing workflow.

Repository controls follow [GitHub's security guidance](https://docs.github.com/en/repositories/creating-and-managing-repositories/best-practices-for-repositories).
