"""`Policy` — the one config surface (SPEC-AIP-002 §3.2, C-4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from navigator_orchestrator.engine.policy import DEFAULT_MODEL, Policy, with_overrides


def test_default_is_claude_on_bedrock() -> None:
    """SPEC-AIP-002 §3.3: `bedrock:` Claude is the golive default."""
    policy = Policy()
    assert policy.model == DEFAULT_MODEL
    assert policy.provider == "bedrock"
    assert policy.model_id.startswith("anthropic.claude-")


def test_uat_model_is_a_pure_config_change() -> None:
    """The GCP-UAT → AWS-golive port is a `model` string, nothing else."""
    uat = Policy(model="vertex:gemini-3.5-pro", temperature=0.4)
    assert uat.provider == "vertex"
    assert uat.model_id == "gemini-3.5-pro"


def test_sampling_params_are_off_by_default() -> None:
    """Provider-dependent: Gemini accepts it, current Claude models 400 on it."""
    assert Policy().temperature is None


def test_temperature_spans_the_wider_gemini_range() -> None:
    assert Policy(temperature=2.0).temperature == 2.0
    with pytest.raises(ValidationError):
        Policy(temperature=2.1)
    with pytest.raises(ValidationError):
        Policy(temperature=-0.1)


def test_provider_and_model_id_split_on_the_first_colon() -> None:
    policy = Policy(model="bedrock:anthropic.claude-opus-5")
    assert policy.provider == "bedrock"
    assert policy.model_id == "anthropic.claude-opus-5"


def test_model_needs_a_provider_prefix() -> None:
    with pytest.raises(ValidationError, match="provider"):
        Policy(model="claude-opus-5")


def test_policy_is_frozen() -> None:
    policy = Policy()
    with pytest.raises(ValidationError):
        policy.model = "fake:echo"  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy(temprature=0.5)  # type: ignore[call-arg]


def test_fingerprint_is_order_independent_and_distinguishing() -> None:
    assert Policy(model="fake:echo").fingerprint() == Policy(model="fake:echo").fingerprint()
    assert Policy(model="fake:echo").fingerprint() != Policy(model="fake:alt").fingerprint()
    assert Policy(effort="high").fingerprint() != Policy(effort="low").fingerprint()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_all_effort_levels_are_accepted(effort: str) -> None:
    assert Policy(effort=effort).effort == effort  # type: ignore[arg-type]


def test_bad_effort_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy(effort="extreme")  # type: ignore[arg-type]


# ------------------------------------------------------------ request overrides


def test_overrides_replace_only_what_is_given() -> None:
    base = Policy(model="fake:echo", max_tokens=999, effort="high")
    result = with_overrides(base, model="vertex:gemini-3.5-pro", temperature=0.7)
    assert result.model == "vertex:gemini-3.5-pro"
    assert result.temperature == 0.7
    # Untouched fields survive the round trip.
    assert result.max_tokens == 999
    assert result.effort == "high"


def test_overrides_with_nothing_set_is_the_base_policy() -> None:
    base = Policy(model="fake:echo")
    assert with_overrides(base) == base


def test_overrides_are_revalidated() -> None:
    """`model_copy(update=...)` would skip validators; the constructor does not."""
    base = Policy(model="fake:echo")
    with pytest.raises(ValidationError, match="provider"):
        with_overrides(base, model="gemini-3.5-pro")  # no `<provider>:` prefix
    with pytest.raises(ValidationError):
        with_overrides(base, temperature=5.0)


def test_overrides_do_not_mutate_the_base() -> None:
    base = Policy(model="fake:echo")
    with_overrides(base, model="vertex:gemini-3.5-pro")
    assert base.model == "fake:echo"
