"""The access policy: which tier has which capability (ADR-0005 §2).

Pure data and pure functions. This module imports nothing from ``app`` so the
rules stay readable on their own and could move to a shared package later
(``AGENTS.md``: keep mechanism in files with zero app-specific imports).

Two ideas, kept apart
---------------------

* A **capability** is one named thing a caller may do. Every route declares
  exactly one (``app.access.deps.requires``). The route says WHAT it needs.
* A **tier** is the kind of account the caller has. :data:`POLICY` says WHICH
  tiers have each capability. No route checks a tier directly.

The documented copy of this table is ``docs/ACCESS_POLICY.md``, and
``tests/test_access_policy.py`` fails when the two disagree.

Quotas are NOT numbers here. The number of answers is a setting
(``CITEVYN_ACCESS_FREE_TRIAL_ANSWERS``, ``CITEVYN_ACCESS_PRO_MONTHLY_ANSWERS``)
so the owner can change it without a code change; this table only says which
KIND of allowance a tier has.
"""

from __future__ import annotations

import dataclasses
import enum


class Capability(enum.StrEnum):
    """One named thing a caller may do. Plain strings, not a database enum."""

    # --- held by routes today -------------------------------------------
    #: Needs nothing: health probes and public pages.
    public = "public"
    #: Sign up, sign in, sign out, and "who am I". Anonymous callers need these
    #: to BECOME signed in, so every tier has them.
    sign_in = "sign_in"
    #: Change one's own password.
    manage_account = "manage_account"
    #: Ask a live question (create a session, post a message).
    chat = "chat"
    #: Read or delete one's own conversations.
    history = "history"
    #: Exact flag / command lookup.
    exact_search = "exact_search"
    #: Rate an answer, report it as wrong, request a missing source (ADR-0005 §6).
    feedback = "feedback"
    #: Start, view or manage one's own paid plan (ADR-0005 Phase 4).
    billing = "billing"
    #: Operator routes. Granted by the admin API key, never by a tier.
    operate = "operate"

    # --- declared now, routes arrive in later phases (ADR-0005) ----------
    weekly_digest = "weekly_digest"
    mcp_ask = "mcp_ask"
    api_keys = "api_keys"
    share_answer = "share_answer"
    usage_insights = "usage_insights"
    watch_alerts = "watch_alerts"
    byok = "byok"


class Tier(enum.StrEnum):
    """The kind of account a caller has. ``team`` is designed for, not built."""

    anonymous = "anonymous"
    free = "free"
    pro = "pro"


class Allowance(enum.StrEnum):
    """Which kind of answer allowance a tier has; the NUMBER is a setting."""

    none = "none"
    #: A one-time allowance per verified account (the free trial).
    once = "once"
    #: A monthly allowance that resets each billing period.
    monthly = "monthly"


@dataclasses.dataclass(frozen=True)
class TierPolicy:
    capabilities: frozenset[Capability]
    allowance: Allowance


# Capabilities every tier has, including anonymous.
_EVERYONE = frozenset({Capability.public, Capability.sign_in})

# What a signed-in account adds.
_ACCOUNT = frozenset(
    {
        Capability.manage_account,
        Capability.chat,
        Capability.history,
        Capability.exact_search,
        Capability.feedback,
        Capability.billing,
        Capability.weekly_digest,
    }
)

# What Pro adds.
_PRO = frozenset(
    {
        Capability.mcp_ask,
        Capability.api_keys,
        Capability.share_answer,
        Capability.usage_insights,
        Capability.watch_alerts,
        Capability.byok,
    }
)

#: THE policy table. One row per tier.
POLICY: dict[Tier, TierPolicy] = {
    Tier.anonymous: TierPolicy(_EVERYONE, Allowance.none),
    Tier.free: TierPolicy(_EVERYONE | _ACCOUNT, Allowance.once),
    Tier.pro: TierPolicy(_EVERYONE | _ACCOUNT | _PRO, Allowance.monthly),
}

#: Capabilities no tier grants: they come from a separate credential.
CREDENTIAL_ONLY: frozenset[Capability] = frozenset({Capability.operate})


def has_capability(tier: Tier, capability: Capability) -> bool:
    return capability in POLICY[tier].capabilities


def lowest_tier_with(capability: Capability) -> Tier | None:
    """The cheapest tier that has ``capability``, for "sign in" vs "upgrade" copy."""
    for tier in (Tier.anonymous, Tier.free, Tier.pro):
        if has_capability(tier, capability):
            return tier
    return None
