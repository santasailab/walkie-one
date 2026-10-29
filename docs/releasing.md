# Preparing the public release

The repository is private. Completing software tests does not authorize changing
its visibility, publishing packages, or releasing firmware.

Before the team makes it public:

- Select and approve the source-code license. Add its exact license text and
  confirm company ownership/redistribution rights for every included source.
- Finish the third-party inventory for the final source and any distributed
  binaries. Preserve required notices and verify asset provenance.
- Review the complete Git history with Gitleaks and review file contents for
  credentials, customer data, production configuration, and private references.
  Removing a secret from the latest commit does not remove it from history.
- Run `./scripts/check.sh`, the clean-clone rehearsal, dependency audit, and CI
  without company credentials. Review any remaining advisory explicitly.
- Verify README commands and internal links from a fresh clone. Keep supported
  software behavior and pending hardware work clearly distinguished.
- Enable and test private vulnerability reporting; assign a security contact.
  Check available secret scanning and push protection before making visibility
  public. Keep branch protection and CI's read-only token permissions.
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
