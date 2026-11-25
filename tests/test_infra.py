"""Static checks on infra/main.tf that would otherwise surface only in a deploy.

The deploy identity may grant only the roles, and only to the principals, that
its condition lists (#234). A grant added to infra/ without a matching entry
fails the deploy with a 403 - after merge, on main. These tests fail it here
instead.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Every file of the configuration the deploy applies, not just main.tf: a grant
# in a new file is as much the deploy's as one in main.tf. Bootstrap is not
# here because the owner applies it, never the deploy.
INFRA = Path(__file__).resolve().parent.parent / "infra"

# The deploy identity's own grants. They are deliberately outside its
# condition, so it can neither widen them nor lift the condition itself.
DEPLOY_OWN_GRANTS = {"deploy_builds_group", "deploy_grants_app_roles"}


def _role_grants(source: str) -> dict[str, str]:
    """Map each azurerm_role_assignment block's name to its body.

    The parse expects `terraform fmt` layout. Anything it cannot read fails
    the test rather than being skipped: a loose search counts every
    declaration, and each must have been parsed. Otherwise a grant the parse
    missed - one with a hyphen in its name, as Terraform allows (#236 review),
    or one laid out by hand - would pass every check unread.
    """
    grants = dict(
        re.findall(
            r'^resource "azurerm_role_assignment" "([^"]+)" \{\n(.*?)^\}',
            source,
            flags=re.MULTILINE | re.DOTALL,
        )
    )
    declared = re.findall(r'\bresource\s+"azurerm_role_assignment"\s+"([^"]+)"', source)
    unread = sorted(set(declared) - set(grants))
    assert not unread, f"Role assignments the test could not read: {unread}"
    return grants


def _attribute(body: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*(.+)$", body, flags=re.MULTILINE)
    assert match, f"no {name} in:\n{body}"
    return match.group(1).strip()


def _local_block(source: str, name: str, opener: str, closer: str) -> str:
    match = re.search(
        rf"^\s*{name}\s*=\s*\{opener}\n(.*?)^\s*\{closer}",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"no local {name} in infra/main.tf"
    return match.group(1)


@pytest.fixture(scope="module")
def source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(INFRA.glob("*.tf"))
    )


@pytest.fixture(scope="module")
def app_grants(source) -> dict[str, str]:
    grants = {
        name: body
        for name, body in _role_grants(source).items()
        if name not in DEPLOY_OWN_GRANTS
    }
    # Guards the parsing as much as the configuration: a regex that matched
    # nothing would let every test below pass vacuously.
    assert grants
    return grants


def test_the_deploy_may_grant_every_role_infra_grants(source, app_grants):
    # Given the roles the deploy's condition allows
    allowed = set(
        re.findall(
            r'^\s*"([^"]+)"\s*=',
            _local_block(source, "deploy_may_grant", "{", "}"),
            flags=re.MULTILINE,
        )
    )

    # When every role infra/ grants is compared against them
    missing = {
        name: _attribute(body, "role_definition_name").strip('"')
        for name, body in app_grants.items()
        if _attribute(body, "role_definition_name").strip('"') not in allowed
    }

    # Then none is missing, so the deploy will not be refused it with a 403
    assert not missing, (
        f"Add these roles to local.deploy_may_grant in infra/main.tf: {missing}"
    )


def test_the_deploy_may_grant_to_every_principal_infra_grants_to(source, app_grants):
    # Given the principals the deploy's condition allows
    block = _local_block(source, "deploy_may_grant_to", "[", "]")
    allowed = {line.strip().rstrip(",") for line in block.splitlines() if line.strip()}

    # When every principal infra/ grants a role to is compared against them
    missing = {
        name: _attribute(body, "principal_id")
        for name, body in app_grants.items()
        if _attribute(body, "principal_id") not in allowed
    }

    # Then none is missing
    assert not missing, (
        f"Add these principals to local.deploy_may_grant_to in infra/main.tf: {missing}"
    )


def test_the_deploy_does_not_hold_owner(source):
    # Given infra/'s role grants
    # When their roles are read
    roles = {
        _attribute(body, "role_definition_name").strip('"')
        for body in _role_grants(source).values()
    }

    # Then none is Owner, which could grant anything to anyone (#234)
    assert "Owner" not in roles


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_terraform_is_checked_out_with_lf_everywhere():
    # Given every Terraform file the deploy applies
    files = sorted(path.name for path in INFRA.glob("*.tf"))

    # When git is asked which line endings it checks them out with
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["git", "check-attr", "eol", "--", *files],  # noqa: S607
        cwd=INFRA,
        capture_output=True,
        text=True,
        check=True,
    )

    # Then it is LF for each, so a heredoc builds the same string on Windows
    # as on the deploy runner, and an apply by hand cannot store a value every
    # later deploy plans to change
    not_lf = [line for line in result.stdout.splitlines() if not line.endswith(": lf")]
    assert not not_lf, not_lf
