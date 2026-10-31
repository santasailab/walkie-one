# Contributing

The Walkie One team maintains the official edition. You can adapt the material
identified in [NOTICE.md](NOTICE.md) under [LICENSE](LICENSE). We may accept pull
requests, but opening one does not guarantee inclusion, review time, or support.
Discuss substantial upstream changes before investing in them.

The Walkie One Open Community License is source available and includes a
hardware-sales allowance. Read its terms before distributing files or products.
Private modifications do not have to be published.

## Contribution rights

Identify the origin and applicable licenses of submitted material, including
employer or co-owner rights. Preserve existing notices. A pull request does not
transfer ownership or count as acceptance of a separate contributor agreement.
Because the project also offers commercial hardware permission, maintainers must
arrange a separate written contribution agreement with sufficient licensing
rights before merging external source or documentation contributions. Keep the
agreement and personal contact details in private records. Until that agreement
is in place, maintainers may discuss a proposal but must not merge the contribution.
Contact [contact@walkieone.com](mailto:contact@walkieone.com) to arrange it.

## Development

Use Python 3.12 on macOS or Linux. Run `./scripts/setup.sh`, then
`./scripts/check.sh`. Tests use isolated temporary state, generated credentials,
fixture assistants, and loopback sockets. They do not need a company account.
With Gitleaks 8.30.1 installed, also run `./scripts/security.sh` before pushing.
CI checks the same commands. Never disable a security check to get a green run;
investigate the finding and keep any sensitive reproduction outside Git.

To update dependencies, install `pip-tools` in your development environment,
edit the owning `.in` file, and regenerate both locks:

```sh
.venv/bin/pip-compile --generate-hashes --strip-extras \
  --output-file=personal-server/requirements.txt personal-server/requirements.in
.venv/bin/pip-compile --allow-unsafe --generate-hashes \
  --output-file=requirements-dev.txt requirements-dev.in
```

Reinstall with `./scripts/setup.sh`, rerun both checks, and update the dependency
inventory when versions or license notices change.

Use a branch and pull request for changes after the initial repository import.
Keep each PR focused, include meaningful regression tests, and update the
existing guide when behavior changes. Use a Conventional Commit title such as
`fix(server): reject replies routed to another device`.

Explain what changes for the user, how you verified it, and any compatibility
impact. Report hardware tests separately from simulation. Keep new dependencies
small, pin their resolved versions, and record their licenses. Human and
AI-assisted contributions follow the same review standard; the author remains
responsible for the code, its provenance, and its tests.

Use issues for reproducible bugs and proposals. Include platform, Python version,
commit, expected behavior, and redacted steps. Keep discussion respectful and
specific. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
