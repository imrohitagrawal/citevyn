"""Every OAuth setting must be spelled out where an operator will look (#289).

WHY THIS FILE EXISTS
--------------------
`grep -i oauth` over `.env.example`, `infra/docker/prod.env.example`, the
README env table and `docs/DEPLOY_FLY.md`'s secrets list used to return **zero
env-var names**. The only spelling of any OAuth knob lived in `docs/API_SPEC.md`.

That is worse than a normal docs gap, because the failure is silent to the
operator and loud to the user: `Settings` fails closed (a half-configured
provider is refused at startup, and a missing redirect base is refused in
production), but `AuthModal` and `ConnectedAccountsDrawer` render **both**
provider buttons unconditionally and `_require_provider` 404s an unconfigured
one. So an operator who follows `DEPLOY_FLY.md` verbatim ships dead buttons
with no runbook trail leading back to the cause.

Fixing the docs once does not stop the next OAuth knob from arriving
undocumented -- ADR-0004 PR 13 added one to PR 12's five and nobody noticed.
This is the mechanical part.

SCOPE, STATED HONESTLY
----------------------
This guards OAuth settings only, not every setting. 29 of the 84 fields on
`Settings` are absent from `.env.example` (measured), so a blanket rule would
fail on arrival and belongs in its own change -- tracked as #332 rather than
smuggled in here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "backend" / "app" / "core" / "config.py"

# Files an operator actually reads when standing the service up, and what
# counts as "documented" in each.
#
# A bare substring search is NOT enough, and this is not hypothetical -- the
# first version of this test searched for the name anywhere in the file, and
# deleting `CITEVYN_OAUTH_REDIRECT_BASE_URL=` from `.env.example` left it GREEN,
# because the name also appears in the prose above it explaining how the
# callback URL is built. A guard that a passing mention satisfies is not a
# guard. So each file must contain a real DECLARATION:
#   *.env.example / DEPLOY_FLY.md -> `NAME=` (assignment or `fly secrets set` arg)
#   README.md                     -> `` `NAME` `` (a backticked cell in the env table)
_OPERATOR_DOCS: tuple[tuple[Path, str], ...] = (
    (Path(".env.example"), "{name}="),
    (Path("infra/docker/prod.env.example"), "{name}="),
    (Path("README.md"), "`{name}`"),
    (Path("docs/DEPLOY_FLY.md"), "{name}="),
)


def _oauth_setting_names() -> list[str]:
    """Every ``Settings`` field whose name mentions oauth, as its env var name.

    Read out of the source rather than by importing ``Settings``: importing it
    pulls the app's config machinery (and a live ``.env``) into a test whose
    only subject is text in documentation.
    """
    src = _CONFIG.read_text(encoding="utf-8")
    fields = re.findall(r"^\s{4}([a-z][a-z0-9_]*):\s*[^=\n]+", src, re.M)
    return [f"CITEVYN_{f.upper()}" for f in fields if "oauth" in f]


def test_the_extraction_finds_the_oauth_settings_at_all() -> None:
    """Partner: without this, every assertion below passes on an empty list.

    If the field regex stops matching -- a formatting change, a move to a
    nested model -- the parametrised test below would silently collect zero
    cases and report green while guarding nothing.
    """
    names = _oauth_setting_names()
    assert len(names) >= 6, f"expected at least the 6 known OAuth settings, found {names}"
    # The two that carry the security-relevant behaviour, named outright so a
    # rename has to be deliberate rather than quietly shrinking the guard.
    assert "CITEVYN_OAUTH_REDIRECT_BASE_URL" in names
    assert "CITEVYN_GITHUB_OAUTH_CLIENT_ID" in names


@pytest.mark.parametrize(("doc", "form"), _OPERATOR_DOCS, ids=lambda v: str(v))
def test_every_oauth_setting_is_declared_in_each_operator_doc(doc: Path, form: str) -> None:
    path = _REPO_ROOT / doc
    assert path.exists(), f"{doc} is missing"
    text = path.read_text(encoding="utf-8")
    missing = [n for n in _oauth_setting_names() if form.format(name=n) not in text]
    assert not missing, (
        f"{doc} does not DECLARE {missing} (looked for {form.format(name='<NAME>')!r}). "
        f"An operator greps for the variable, so a setting that exists on Settings but "
        f"is declared in no operator-facing file is undiscoverable -- which is exactly "
        f"how #289 happened. A mention in prose does not count."
    )
