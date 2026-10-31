# Preparing Walkie One for public visibility

Research date: 2026-10-31. This note recommends repository controls; it is not a
live-settings audit, a claim that no vulnerabilities exist, or hardware
qualification. Keep the repository private during preparation. The owner makes
the visibility change separately.

## Scope and terminology

Describe this release as **source available** under the
[Walkie One Open Community License v0.2](../../LICENSE), with the component and
third-party boundaries in [NOTICE.md](../../NOTICE.md). Its commercial hardware
limits differ from the OSI definition, which requires unrestricted redistribution
and use across fields of endeavor. Public visibility does not change license
terms. [OSI Open Source Definition](https://opensource.org/osd)

The practices below remain useful without an open-source badge. The OpenSSF Best
Practices passing badge requires a FLOSS license; do not imply eligibility or
certification for this custom license.
[OpenSSF passing criteria](https://www.bestpractices.dev/en/criteria/0)

## Controls to establish before publication

**Protect accepted code.** Require pull requests, one independent approval,
current required CI from the expected GitHub App, resolved conversations, stale
review dismissal, and approval of the latest push. Apply protections to admins;
block force pushes and deletion. One reviewer is a practical small-team default;
two reviewers are an optional stronger policy when staffing permits.
[GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
[OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md)

**Make review ownership real.** Use actual users with write access, or visible
teams explicitly granted write access, in `.github/CODEOWNERS`. Protect that file
and workflows, and enable required code-owner review separately. Invalid or
ineligible owners do not provide the intended protection. Any one matching owner
can satisfy an ownership requirement.
[GitHub CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)

**Keep Actions privileges narrow.** Default `GITHUB_TOKEN` to read-only, grant
additional permissions only to jobs that need them, pin external actions to full
commit SHAs, and restrict allowed actions to those reviewed. Keep credentials out
of PR checks. Do not execute untrusted PR code in privileged
`pull_request_target` or `workflow_run` contexts; use disposable GitHub-hosted
runners for public contributions.
[GitHub Actions security guidance](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)

Require approval for **all external contributors** to run fork workflows after
publication. First-time-only approval stops applying after an innocuous merged
contribution. Review workflow changes before approving execution. This approval
controls execution and resource consumption; it does not make untrusted code
safe.
[Actions repository settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository),
[Approving fork runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks)

**Maintain dependencies.** Enable alerts, security-update PRs, and scheduled
version-update PRs as separate controls. Configure `pip` for both dependency
roots and `github-actions` for workflow references in `.github/dependabot.yml`.
Weekly updates are a reasonable starting point. Review generated lockfile hashes,
notices, and passing CI before merging.
[Dependabot version updates](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-version-updates)

**Give contributors a usable entry point.** Keep README scope/setup, license,
contribution instructions, code of conduct, security reporting, and issue/PR
templates discoverable. Name supported versions and a monitored private security
contact; avoid promising an unstaffed response deadline.
[GitHub community profile](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)

## Availability while private and after publication

Availability is not proof that a feature is enabled. Read settings back after
changes and preserve the evidence separately from this research note.

| Control | While private | After the owner makes it public |
| --- | --- | --- |
| Branch/tag protection | Requires the applicable Pro, Team, or Enterprise plan. | Available on GitHub Free; recheck effective rules. |
| Dependabot alerts, security updates, version updates | Available on all plans; configure now. | Keep enabled and monitor PRs/alerts. |
| GitHub secret scanning and push protection | Private organization repositories require Secret Protection entitlement. Do not buy or enable a trial implicitly. | Free public features are available; verify repository scanning and push protection explicitly. |
| GitHub code scanning | Private organization repositories require Code Security entitlement. | Public platform availability is free; CodeQL tool licensing still needs the separate check below. |
| Private vulnerability reporting | Public-repository feature; maintain the private email reporting route meanwhile. | Enable it, verify the Security reporting link, and ensure maintainers receive security notifications. |
| Fork-contributor workflow approval | Public-fork approval policy may be unavailable until publication. | Select all external contributors and verify it. |

Sources: [GitHub security feature availability](https://docs.github.com/en/code-security/getting-started/github-security-features),
[ruleset availability](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets),
[private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository),
[fork workflow settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).

A follow-up configuration script can refuse private repositories, apply the
public-only settings, and read them back. It must never change visibility. This is
a recommended implementation boundary, not a GitHub requirement.

## Static analysis and commit signatures

Use Bandit for the Python static-analysis gate, with narrow, explained
false-positive suppressions. CodeQL is optional future work, not a publication
blocker: its CLI license defines an Open Source Codebase using an OSI license and
restricts ordinary unpaid analysis and automated CI to qualifying codebases.
This repository's custom license does not meet that definition. Confirm a
covering paid agreement or explicit permission before adopting CodeQL CLI or an
Action that invokes it. Public code-scanning availability alone does not establish
that permission.
[CodeQL CLI terms](https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md),
[CodeQL platform availability](https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-code-scanning)

Requiring signed commits is optional hardening. GitHub can reject a squash merge
because an introduced head-branch commit is unsigned, even if the final squash
would be signed. Defer that rule until contributor and bot signing workflows are
tested; retain required reviews and CI.
[GitHub signed-commit protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-signed-commits)

## Protect future releases

For `refs/tags/v*`, use one active tag ruleset preventing update/deletion without
bypass, plus a separate creation rule limited to release maintainers. Rulesets
layer, so permission to create a tag need not permit rewriting it. Protect any
future maintained release branches like `main`; feature branches can remain
editable.
[GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)

If representing repository admins in the creation-rule bypass, GitHub's Terraform
provider documents `RepositoryRole` ID `5` as admin. Verify the resulting role in
GitHub rather than treating a successful request as sufficient evidence.
[GitHub provider role mapping](https://github.com/integrations/terraform-provider-github/blob/main/docs/resources/repository_ruleset.md#bypass_actors)

Enable release immutability for future releases where available. It locks
published tags and attached assets and creates a release attestation; tag rules
alone do not freeze assets. Prepare a draft, attach every asset, then publish.
Existing releases are not retroactively covered merely by enabling the setting.
[Immutable release behavior](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
[enabling immutability](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes)

## Evidence for the final readiness decision

Recommended project-specific acceptance evidence:

- Required CI passes on the actual proposed merge, including software tests,
  dependency audit, secret scans, and static analysis.
- Scan the publishable tree and history, plus public-facing PR text, logs, and
  artifacts. Triage findings; rotate any real exposed credential before release.
- Read back protections, allowed Actions, token permissions, dependency settings,
  ownership eligibility, and release controls. Record unavailable features
  explicitly.
- Clearly distinguish tested software behavior from hardware and live-provider
  qualification. Ensure the README does not promise the private product backend.
- Prepare the public-only follow-up and leave visibility unchanged. Recheck those
  controls immediately after the owner publishes.

These checks reduce exposure and make maintenance safer. They support a scoped
readiness judgment; they do not prove the absence of vulnerabilities.
