#!/usr/bin/env python3
"""Verify or enable free public-repository protections; never change visibility."""

import argparse
import json
import subprocess

REPOSITORY = "santasailab/walkie-one"


def github_api(method, suffix="", payload=None):
    command = ["gh", "api", "--method", method, f"repos/{REPOSITORY}{suffix}"]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"GitHub rejected {method} {suffix or '/repository'}. "
            "Check gh authentication, repository administration access and feature availability."
        )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def finish_public_security(*, apply=False, api=github_api):
    repository = api("GET")
    if repository.get("visibility") != "public":
        raise RuntimeError(
            "Repository is not public. No settings changed. The owner must change "
            "visibility separately, then rerun this command."
        )
    if apply:
        if not repository.get("permissions", {}).get("admin"):
            raise RuntimeError(
                "Repository administration access is required. No settings changed."
            )
        api(
            "PATCH",
            payload={
                "security_and_analysis": {
                    "secret_scanning": {"status": "enabled"},
                    "secret_scanning_push_protection": {"status": "enabled"},
                }
            },
        )
        api("PUT", "/private-vulnerability-reporting")
        api(
            "PUT",
            "/actions/permissions/fork-pr-contributor-approval",
            {"approval_policy": "all_external_contributors"},
        )

    repository = api("GET")
    settings = repository.get("security_and_analysis", {})
    checks = {
        "public visibility": repository.get("visibility") == "public",
        "secret scanning": settings.get("secret_scanning", {}).get("status")
        == "enabled",
        "push protection": settings.get("secret_scanning_push_protection", {}).get(
            "status"
        )
        == "enabled",
        "private vulnerability reporting": api(
            "GET", "/private-vulnerability-reporting"
        ).get("enabled")
        is True,
        "approval for every external contributor's workflow": api(
            "GET", "/actions/permissions/fork-pr-contributor-approval"
        ).get("approval_policy")
        == "all_external_contributors",
    }
    missing = [name for name, enabled in checks.items() if not enabled]
    if missing:
        raise RuntimeError("Public protections incomplete: " + ", ".join(missing))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="enable the listed protections after publication",
    )
    args = parser.parse_args()
    try:
        checks = finish_public_security(apply=args.apply)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")
    for name in checks:
        print(f"PASS: {name}")
    print("Repository visibility was not changed by this command.")


if __name__ == "__main__":
    main()
