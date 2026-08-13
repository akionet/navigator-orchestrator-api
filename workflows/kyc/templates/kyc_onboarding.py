"""`kyc-onboarding` — screen a prospective client and assign an eligibility tier.

The reference workflow. See `../DESIGN.md` for why each step is the kind it is;
in short: rules where the answer is defined, agents where the input is
unstructured and the judgement is open, humans where being wrong is expensive.

Six deterministic steps, two agents, two gates. Every step ships a working
default, so `flows/kyc.py` is one line and each default is an *optional*
override — a workflow file is a diff against this template, not a program.

This template lives in the project rather than the engine package. Deleting
`workflows/kyc/` removes it and breaks nothing, which
`tests/test_example_workflow_is_removable.py` asserts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.templates import Param, Step, Template

__all__ = ["ENTITY_PROMPT", "VERDICT_PROMPT", "kyc_onboarding"]

ENTITY_PROMPT = "kyc-entities@1"
VERDICT_PROMPT = "kyc-adjudicate@1"

#: Roles that mean the subject is *named in* adverse coverage without being
#: implicated by it — the defence lawyer, the auditor, the victim. See DESIGN §1.
NON_IMPLICATING = frozenset(
    {"representative", "witness", "victim", "investigator", "commentator", "namesake"}
)

CONTROLLING_STAKE_PCT = 25.0
PREMIUM_INCOME_EUR = 120_000
PREMIUM_SAVINGS_EUR = 100_000
HNWI_MINIMUM_EUR = 2_000_000
UHNWI_MINIMUM_EUR = 10_000_000
UHNWI_MAX_ALTERNATIVE_SHARE = 0.50


def _read(ctx: Ctx, relative: str) -> Any:
    """Read one JSON fixture from the project directory."""
    return json.loads(Path(ctx.files.resolve(relative)).read_text(encoding="utf-8"))


def _row(rows: list[dict[str, Any]], client_id: str) -> dict[str, Any]:
    return next((row for row in rows if row.get("client_id") == client_id), {})


# ── 1. identity ──────────────────────────────────────────────────────────────


def _load_client(ctx: Ctx, client_id: str) -> dict[str, Any]:
    client = _row(_read(ctx, "data/clients.json"), client_id)
    ctx.require(bool(client), f"no client {client_id!r} in data/clients.json")
    ctx.note(f"screening {client['name']} ({client_id})")
    return client


def _validate_jurisdiction(ctx: Ctx, client: dict[str, Any]) -> str:
    """A missing country is an error, not a pass.

    A screening step that silently succeeds when it cannot establish
    jurisdiction is worse than no step: it produces a clean record with nothing
    behind it. Fixture CL-0007 exists to hold this behaviour in place.
    """
    country = (client.get("address_detail") or {}).get("country")
    ctx.require(
        bool(country),
        f"client {client['client_id']} has no country on its address; "
        "country-scoped sanctions screening cannot be performed",
    )
    return str(country)


# ── 2. adverse media: extract, then adjudicate ───────────────────────────────


async def _extract_entities(ctx: Ctx, client: dict[str, Any]) -> list[dict[str, Any]]:
    """Wide, shallow pass over the corpus. Makes no judgement — that is step 3."""
    corpus = _read(ctx, "reference/adverse_media_articles.json")
    raw = await ctx.ai.draft(ENTITY_PROMPT, corpus=json.dumps(corpus, indent=2))
    mentions = _as_list(raw)
    ctx.note(f"indexed {len(corpus)} article(s), {len(mentions)} distinct name(s)")
    return mentions


async def _adjudicate_media(
    ctx: Ctx, client: dict[str, Any], mentions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Only the subject's articles reach the expensive judgement."""
    corpus = {a["article_id"]: a for a in _read(ctx, "reference/adverse_media_articles.json")}
    subject = client["name"]
    article_ids = sorted(
        {
            article_id
            for mention in mentions
            if str(mention.get("name", "")).strip().lower() == subject.strip().lower()
            for article_id in mention.get("article_ids", ())
        }
    )
    if not article_ids:
        ctx.note(f"no article names {subject}")
        return []
    candidates = [corpus[a] for a in article_ids if a in corpus]
    raw = await ctx.ai.draft(
        VERDICT_PROMPT, subject=subject, candidate_articles=json.dumps(candidates, indent=2)
    )
    findings = _as_list(raw)
    implicating = [f for f in findings if f.get("implicates_subject")]
    ctx.note(f"{len(findings)} article(s) reviewed, {len(implicating)} implicating")
    return findings


def _as_list(raw: Any) -> list[dict[str, Any]]:
    """Tolerate a model returning JSON text, a dict, or a list.

    The offline `fake:` model echoes its prompt, so a demo run yields nothing
    parseable — an empty finding list is the correct reading of that, and is why
    this never raises. A real provider returns the documented shape.
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, str):
        try:
            return _as_list(json.loads(raw))
        except json.JSONDecodeError:
            return []
    return []


# ── 3. PEP ───────────────────────────────────────────────────────────────────


def _pep_check(ctx: Ctx, client: dict[str, Any]) -> dict[str, Any]:
    register = _read(ctx, "reference/pep_list.json")
    name = client["name"].strip().lower()
    match = next((e for e in register["entries"] if e["name"].strip().lower() == name), None)
    if match is None:
        return {"is_pep": False, "source_list": register["source_list"]}
    ctx.note(f"PEP match: {match['position']} ({match['country']})")
    return {"is_pep": True, "source_list": register["source_list"], **match}


# ── 4. sanctions ─────────────────────────────────────────────────────────────


def _sanctions_check(ctx: Ctx, client: dict[str, Any], client_id: str) -> dict[str, Any]:
    """Jurisdiction rules are *ordered*, and the order is the whole point.

    A controlling stake in a business decides the relevant country before
    residency does. CL-0008 has unremarkable Cyprus residency and a 74.5% stake
    in an Iran-operating shipper: check residency first and he passes.
    """
    interests = (_row(_read(ctx, "data/client_business_interests.json"), client_id)).get(
        "interests", []
    )
    controlling = next(
        (i for i in interests if i.get("stake_pct", 0) >= CONTROLLING_STAKE_PCT), None
    )
    address = client.get("address_detail") or {}
    if controlling:
        country = controlling["country_of_operations"]
        basis = f"controlling stake in {controlling['business_name']}"
    else:
        country = address.get("country_of_legal_residency") or address.get("country")
        basis = "legal residency"
    ctx.require(bool(country), "sanctions jurisdiction could not be resolved")

    programmes = _read(ctx, "reference/sanctioned_countries.json")["programmes"]
    country_hit = next((p for p in programmes if p["country"] == country), None)

    designated = _read(ctx, "reference/sanctioned_individuals.json")["entries"]
    name = client["name"].strip().lower()
    # Exact match only. Fuzzy matching would flag "Viktor Aslanoff" against
    # "Viktor Aslanov" — see data/README.md; the trade is documented, not hidden.
    individual_hit = next((e for e in designated if e["name"].strip().lower() == name), None)

    result = {
        "country": country,
        "basis": basis,
        "country_programme": country_hit["programme"] if country_hit else None,
        "country_effect": country_hit["effect"] if country_hit else "clear",
        "individual_listed": individual_hit is not None,
    }
    ctx.note(f"sanctions jurisdiction {country} via {basis}: {result['country_effect']}")
    # `decline`, not `require`: a sanctions hit is the control working, not a
    # fault. It reaches the caller as status "declined" rather than "failed", so
    # a week of ordinary refusals does not read as a week of crashes.
    if result["country_effect"] == "decline":
        ctx.decline(
            f"sanctions screening declines this client: {country} is {result['country_programme']}"
        )
    if result["individual_listed"]:
        ctx.decline(f"{client['name']} is a designated person")
    return result


# ── 5. eligibility ───────────────────────────────────────────────────────────


def _compute_eligibility(ctx: Ctx, client: dict[str, Any], client_id: str) -> dict[str, Any]:
    """Thresholds are arithmetic. No agent, and no room for one."""
    financials = _row(_read(ctx, "data/client_financials.json"), client_id)
    assets = _row(_read(ctx, "data/client_assets.json"), client_id)
    holdings = assets.get("holdings", {})
    address = client.get("address_detail") or {}

    conventional = holdings.get("investable", 0) + holdings.get("liquid", 0)
    alternative = holdings.get("art", 0) + holdings.get("crypto", 0)
    total = conventional + alternative

    # UHNWI may count art and crypto, but not past half the portfolio; the
    # excess is excluded and the tier recomputed on what is left.
    cap = total * UHNWI_MAX_ALTERNATIVE_SHARE
    counted_alternative = min(alternative, cap)
    uhnwi_qualifying = conventional + counted_alternative

    # Residency is a gate for Premium only, and the financial test is any-of.
    premium_resident = address.get("country") == "DE" and bool(address.get("right_to_reside"))
    premium_means = (
        financials.get("annual_income_eur", 0) >= PREMIUM_INCOME_EUR
        or financials.get("savings_eur", 0) >= PREMIUM_SAVINGS_EUR
    )

    if uhnwi_qualifying >= UHNWI_MINIMUM_EUR:
        tier = "private_banking_uhnwi"
    elif conventional >= HNWI_MINIMUM_EUR:
        tier = "private_banking_hnwi"
    elif premium_resident and premium_means:
        tier = "premium"
    else:
        tier = "ineligible"

    if alternative > cap:
        ctx.note(
            f"art+crypto {alternative:,.0f} exceeds 50% of {total:,.0f}; "
            f"counted {counted_alternative:,.0f}"
        )
    ctx.note(f"tier: {tier}")
    return {
        "tier": tier,
        "conventional_eur": conventional,
        "alternative_eur": alternative,
        "uhnwi_qualifying_eur": uhnwi_qualifying,
        "premium_residency_met": premium_resident,
        "premium_means_met": premium_means,
        "has_art": bool(assets.get("has_art")),
        "art_items": assets.get("art_items", []),
    }


# ── 6. outcome ───────────────────────────────────────────────────────────────


def _emit_outcome(  # noqa: PLR0917 - each pool key is a named hook argument
    ctx: Ctx,
    client: dict[str, Any],
    adverse_media: list[dict[str, Any]],
    pep: dict[str, Any],
    sanctions: dict[str, Any],
    eligibility: dict[str, Any],
    pep_decision: Any = None,
    art_decision: Any = None,
) -> dict[str, Any]:
    implicating = [f for f in adverse_media if f.get("implicates_subject")]
    return {
        "client_id": client["client_id"],
        "name": client["name"],
        "tier": eligibility["tier"],
        "adverse_media_implicating": len(implicating),
        "adverse_media_reviewed": len(adverse_media),
        "is_pep": pep.get("is_pep", False),
        "sanctions_country": sanctions["country"],
        "sanctions_basis": sanctions["basis"],
        "decisions": {"pep_gate": pep_decision, "art_gate": art_decision},
    }


kyc_onboarding = Template(
    name="kyc-onboarding",
    doc="Screen a prospective client and assign an eligibility tier.",
    prompt_refs=(ENTITY_PROMPT, VERDICT_PROMPT),
    # Typed so the console can render a launch form rather than a free-text box.
    # This is the workflow's input *edge*; everything the steps hand each other
    # afterwards stays untyped, and should.
    params=(Param("client_id", doc="Client to screen — see data/clients.json"),),
    # The output edge: which pool key is the result, and its declared shape.
    publishes="outcome",
    result_schema="onboarding-outcome",
    steps=(
        Step(
            name="load_client",
            executor="local",
            produces="client",
            kwargs=("client_id",),
            default=_load_client,
            summary_keys=("client_id", "name"),
            doc="fetch the client's identity record",
        ),
        Step(
            name="validate_jurisdiction",
            executor="local",
            produces="jurisdiction",
            kwargs=("client",),
            default=_validate_jurisdiction,
            doc="a missing country blocks the run rather than passing it",
        ),
        Step(
            name="extract_entities",
            executor="agent",
            produces="mentions",
            kwargs=("client",),
            default=_extract_entities,
            doc="index proper names across the article corpus; no judgement",
        ),
        Step(
            name="adjudicate_media",
            executor="agent",
            produces="adverse_media",
            kwargs=("client", "mentions"),
            default=_adjudicate_media,
            doc="is it adverse, and is the subject implicated or merely present?",
        ),
        Step(
            name="pep_check",
            executor="local",
            produces="pep",
            kwargs=("client",),
            default=_pep_check,
            summary_keys=("is_pep",),
            doc="exact lookup against the PEP register",
        ),
        Step(
            name="pep_gate",
            executor="gate",
            produces="pep_decision",
            # Only stop when there is something to decide. A reviewer confirming
            # eight times a day that someone is *not* a PEP stops reading.
            when="pep.is_pep",
            # These pool keys are what the reviewer is shown. The engine never
            # interprets them, which is why one CLI can review any workflow.
            kwargs=("client", "pep", "adverse_media"),
            doc="a PEP match is a decision a compliance officer owns, not a rejection",
        ),
        Step(
            name="sanctions_check",
            executor="local",
            produces="sanctions",
            kwargs=("client", "client_id"),
            default=_sanctions_check,
            summary_keys=("country", "basis"),
            doc="resolve the relevant jurisdiction, then screen country and individual",
        ),
        Step(
            name="compute_eligibility",
            executor="local",
            produces="eligibility",
            kwargs=("client", "client_id"),
            default=_compute_eligibility,
            summary_keys=("tier",),
            doc="threshold arithmetic over income, savings and assets",
        ),
        Step(
            name="art_gate",
            executor="gate",
            produces="art_decision",
            # Any art at all, at any tier — the attestation is about provenance,
            # not about how much the portfolio is worth.
            when="eligibility.has_art",
            kwargs=("client", "eligibility"),
            doc="art needs an attestation: historian review, provenance, no theft claim",
        ),
        Step(
            name="emit_outcome",
            executor="local",
            produces="outcome",
            kwargs=(
                "client",
                "adverse_media",
                "pep",
                "sanctions",
                "eligibility",
                "pep_decision",
                "art_decision",
            ),
            default=_emit_outcome,
            summary_keys=("client_id", "tier"),
            doc="assemble the onboarding record",
        ),
    ),
)


def register(registry: Any) -> None:
    registry.register(kyc_onboarding)
