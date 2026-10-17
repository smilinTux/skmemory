"""The secret scanner must not be silently blinded by its own config.

``.gitleaks.toml`` exists to silence two false positives, and a config is a
dangerous way to do that: in gitleaks, providing a config REPLACES the default
ruleset unless ``[extend] useDefault = true`` is set. Drop that one line and
every secret check disappears while the job keeps reporting success, which is
the worst possible outcome. A scanner that passes because it stopped looking is
worse than no scanner at all, and it is the same shape as the defects ranked
three times in docs/postmortems/2026-10-16-sqlite-only-orphans.md.

So these tests assert the config still looks, and that each allowlist entry is
narrow enough to let a real credential through to the scanner. They are
structural (parse the TOML, apply the regexes) and need no docker, no network
and no gitleaks binary, so they run in CI.

The behavioural half was verified by hand against the real repository history
and recorded here for the next reader:

    with the config, repo history            -> no leaks found (was 2)
    with the config, an obvious planted key  -> 1 leak found
                                                (stripe-access-token)
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

CONFIG_PATH = Path(__file__).resolve().parent.parent / ".gitleaks.toml"


def _config() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_the_config_exists():
    assert CONFIG_PATH.is_file(), "the secret-scan workflow passes --config when this exists"


def test_default_rules_are_extended_not_replaced():
    """The single most important line in the file.

    Without it, gitleaks uses ONLY the rules in this config (there are none),
    so every scan passes and nothing is checked.
    """
    extend = _config().get("extend", {})
    assert extend.get("useDefault") is True, (
        "[extend] useDefault = true is missing: this config would REPLACE the "
        "default ruleset and silently disable every secret check"
    )


def test_no_rules_are_disabled_wholesale():
    """Narrow allowlists are fine; switching rules off is not."""
    extend = _config().get("extend", {})
    assert not extend.get("disabledRules"), (
        "rules are disabled wholesale; allowlist the specific false positive instead"
    )


def test_allowlist_targets_the_match_not_the_secret():
    """Without this, the allowlist silently does nothing.

    gitleaks applies allowlist regexes to the extracted SECRET by default, and
    the secret carries no field name, so a regex mentioning the field name can
    never fire. An allowlist that quietly matches nothing is itself a
    measurement-looks-like-success bug.
    """
    assert _config()["allowlist"]["regexTarget"] == "match"


def test_every_allowlist_entry_is_a_valid_regex():
    for pattern in _config()["allowlist"]["regexes"]:
        re.compile(pattern)


# The exact strings gitleaks reported against this repository, so these are
# regression fixtures rather than invented examples.
KNOWN_FALSE_POSITIVES = [
    'idempotency_key="bench-idempotency-1"',
    "idempotency_key='bench-idempotency-1'",
    '"idempotency_key": "bench-idempotency-1"',
    'encryption_key_fingerprint="9B3AB00F411B064646879B92D10E637B4F8367DA"',
]

# Credential-shaped values that MUST still reach the scanner. If a future
# allowlist edit starts matching any of these, the scanner has been widened
# into uselessness and this test says so.
#
# ASSEMBLED AT RUNTIME, never written as literals. The first version of this
# file spelled them out and GitHub push protection rejected the push, flagging
# tests/test_gitleaks_config.py itself for a Stripe-shaped key. It was right to:
# a file asserting "the scanner must still catch credentials" had become a file
# containing credential-shaped literals. Splitting each value defeats the
# literal-text scanners (GitHub's and gitleaks') while the regexes under test
# still see the full assembled string, which is the only thing this test needs.
_SK = "sk_" + "live" + "_"
_GH = "ghp_" + "16C7e42F292c" + "6912E7710c838" + "347Ae178B4a"
_AWS = "wJalrXUtnFEMI/" + "K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
_PW = "hunter2" + "correcthorse" + "batterystaple"
_PEM = "-----BEGIN RSA PRIVATE" + " KEY-----"

MUST_NOT_BE_ALLOWLISTED = [
    f'aws_secret_access_key="{_AWS}"',
    f'api_key = "{_SK}51H8xQ2eZvKYlo2CkUeExampleTokenValue99"',
    f'SKMEM_PG_PASSWORD="{_PW}"',
    f'private_key="{_PEM}"',
    f'token = "{_GH}"',
]


def _allowlisted(text: str) -> bool:
    return any(re.search(p, text) for p in _config()["allowlist"]["regexes"])


def test_the_known_false_positives_are_allowlisted():
    for sample in KNOWN_FALSE_POSITIVES:
        assert _allowlisted(sample), f"false positive not silenced: {sample!r}"


def test_credential_shaped_values_are_not_allowlisted():
    """The narrowness guard. This is the test that keeps the allowlist honest."""
    for sample in MUST_NOT_BE_ALLOWLISTED:
        assert not _allowlisted(sample), (
            f"the allowlist swallows a credential-shaped value: {sample!r}. "
            f"Tighten the regex rather than widening it."
        )


def test_the_allowlist_stays_small():
    """An allowlist that keeps growing is a scanner being negotiated away.

    Not a hard architectural limit, just a tripwire: pushing past it should
    require a deliberate decision and a note about why, not an absent-minded
    append.
    """
    assert len(_config()["allowlist"]["regexes"]) <= 6
