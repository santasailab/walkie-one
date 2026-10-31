"""Public-only protections must never publish a repo or enable paid private features."""

import runpy
from pathlib import Path

import pytest

FINISH = runpy.run_path(str(Path(__file__).parents[1] / "finish-public-security.py"))[
    "finish_public_security"
]


class GitHubFixture:
    def __init__(self, *, public=True, admin=True, protected=False):
        self.public, self.admin, self.protected = public, admin, protected
        self.reporting = protected
        self.approval = (
            "all_external_contributors" if protected else "first_time_contributors"
        )
        self.calls = []

    def __call__(self, method, suffix="", payload=None):
        self.calls.append((method, suffix, payload))
        if not suffix and method == "GET":
            status = "enabled" if self.protected else "disabled"
            return {
                "visibility": "public" if self.public else "private",
                "permissions": {"admin": self.admin},
                "security_and_analysis": {
                    "secret_scanning": {"status": status},
                    "secret_scanning_push_protection": {"status": status},
                },
            }
        if method == "PATCH":
            self.protected = True
        elif suffix == "/private-vulnerability-reporting":
            if method == "PUT":
                self.reporting = True
            return {"enabled": self.reporting}
        elif suffix == "/actions/permissions/fork-pr-contributor-approval":
            if method == "PUT":
                self.approval = payload["approval_policy"]
            return {"approval_policy": self.approval}
        return {}


@pytest.mark.parametrize("apply", [False, True])
def test_private_repository_is_never_mutated(apply):
    api = GitHubFixture(public=False)
    with pytest.raises(RuntimeError, match="not public"):
        FINISH(apply=apply, api=api)
    assert api.calls == [("GET", "", None)]


def test_public_apply_is_narrow_and_verified():
    api = GitHubFixture()
    assert all(FINISH(apply=True, api=api).values())
    mutations = [call for call in api.calls if call[0] != "GET"]
    assert mutations == [
        (
            "PATCH",
            "",
            {
                "security_and_analysis": {
                    "secret_scanning": {"status": "enabled"},
                    "secret_scanning_push_protection": {"status": "enabled"},
                }
            },
        ),
        ("PUT", "/private-vulnerability-reporting", None),
        (
            "PUT",
            "/actions/permissions/fork-pr-contributor-approval",
            {"approval_policy": "all_external_contributors"},
        ),
    ]


def test_apply_requires_admin_before_any_write():
    api = GitHubFixture(admin=False)
    with pytest.raises(RuntimeError, match="administration access"):
        FINISH(apply=True, api=api)
    assert api.calls == [("GET", "", None)]


def test_verify_reports_missing_protections_without_changing_them():
    api = GitHubFixture()
    with pytest.raises(RuntimeError, match="Public protections incomplete"):
        FINISH(api=api)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_already_protected_repository_needs_no_mutation():
    api = GitHubFixture(protected=True)
    assert all(FINISH(api=api).values())
    assert all(method == "GET" for method, _, _ in api.calls)
