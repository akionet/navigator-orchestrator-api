# DESIGN-KYC-001 — Client onboarding and KYC screening

The reference workflow for the open-source `navigator-orchestrator`. It replaces
the recipe/editorial scenario, which was specific to a private product and is not
being carried across.

KYC is a better reference case than recipes for the same reason it is harder: the
source material is messy free text, most of the decisions are *rules* rather than
judgement, and the two places judgement is genuinely needed are exactly the two
places a human wants a say. That makes it a natural demonstration of the engine's
actual thesis — deterministic steps where the answer is defined, agents where it
is not, and human gates where the cost of being wrong is high.

## 1. The deterministic / agent split

The default is deterministic. An agent is used only where the input is
unstructured natural language and the output is a judgement no lookup can settle.

| Step | Kind | Why this kind |
|---|---|---|
| `load_client` | deterministic | data fetch |
| `validate_jurisdiction` | deterministic | a country is present or it is not |
| `extract_entities` | **agent** | proper-noun extraction over free-text news; no list can enumerate the world's names |
| `adjudicate_media` | **agent** | "is this adverse, and does it implicate *this* person?" is a judgement |
| `pep_check` | deterministic | exact lookup against a reference list |
| `pep_gate` | **human** | a PEP match is not a rejection; it is a decision a compliance officer owns |
| `resolve_sanctions_jurisdiction` | deterministic | a stated rule over stated facts |
| `country_sanctions_check` | deterministic | list cross-reference |
| `individual_sanctions_check` | deterministic | list lookup |
| `compute_eligibility` | deterministic | threshold arithmetic |
| `art_provenance_gate` | **human** | requires an attestation only a person can make |
| `emit_outcome` | deterministic | assembles the record |

Two agents, two human gates, eight deterministic steps. That ratio is the point.

### Why adverse media is split across two agents

One agent does extraction, a second does adjudication, and they are deliberately
not the same call.

**Agent 1 — `extract_entities`** reads the article corpus and returns proper
nouns (people and businesses) with the article ids they appear in. It makes no
judgement about wrongdoing. It is a wide, shallow pass over a large corpus and
suits a small, cheap model.

**Agent 2 — `adjudicate_media`** sees only the articles that mentioned the
subject, and answers two separate questions per article:

1. Does this article describe adverse conduct at all?
2. Is *the subject* implicated in that conduct, or merely present?

The second question is the one that matters and the one a naive implementation
gets wrong. A lawyer representing a fraudster appears in every article about the
fraud. So does the arresting officer, the judge, the journalist, and the victim.
Name-match alone produces a false positive for all of them. The adjudicator is
therefore required to return a `role` for the subject and to treat
`representative`, `witness`, `victim`, `investigator` and `commentator` as
non-implicating regardless of how adverse the article is.

Splitting the two also means the expensive judgement runs over a handful of
articles rather than the whole corpus.

## 2. Flow

```
load_client
  └─ validate_jurisdiction ──(no country)──► error: jurisdiction_unknown
       └─ extract_entities (agent, corpus-wide)
            └─ adjudicate_media (agent, subject's articles only)
                 └─ pep_check
                      └─ [pep_gate] ──(if PEP)──► HUMAN: proceed / decline
                           └─ resolve_sanctions_jurisdiction
                                └─ country_sanctions_check ──(hit)──► decline
                                     └─ individual_sanctions_check ──(hit)──► decline
                                          └─ compute_eligibility
                                               └─ [art_provenance_gate] ──(if art)──► HUMAN: attest
                                                    └─ emit_outcome
```

Sanctions checks are ordered before eligibility on purpose: a sanctions hit ends
the case, and computing a wealth tier for someone who cannot be onboarded is
wasted work that also creates a record nobody should be holding.

## 3. Rules

### 3.1 Jurisdiction resolution (which country's exposure applies)

Stated rule, applied in order:

1. If the client holds a **controlling stake** (≥ 25%) in a business, the
   relevant country is that business's **country of operations**.
2. Otherwise, if the client is **HNWI or above**, the relevant country is their
   **country of legal residency**.
3. If neither can be established, the check **errors** — it does not pass.

A client whose `address_detail.country` is absent fails `validate_jurisdiction`
before any screening runs. This is deliberate and is exercised by fixture
`CL-0007`: a missing country is an error, never a silent pass.

### 3.2 Eligibility tiers

**Premium (consumer banking)**

- Resident in Germany **with right to reside** — a gate, not a scoring factor; and
- at least one of: annual income ≥ **€120,000**, or savings ≥ **€100,000**.

> ⚠️ **Ambiguity flagged.** The source requirement reads "income of at least one
> of the following in Euros 120k income, at least 100k in savings, based in
> Germany with right to reside there." That can be read as *any one of three*, or
> as *residency AND one of two financial thresholds*. This design implements the
> second, because a purely financial qualification with no residency link would
> make the German residency clause meaningless. **Confirm before implementation.**

**Private banking (HNWI)**

- ≥ **€2,000,000** investable or liquid assets.
- **Art and crypto do not count** toward this threshold at all.

**Private banking (UHNWI)**

- ≥ **€10,000,000** investable or liquid assets.
- Art and crypto **may** count, but their combined value must not exceed **50%**
  of the total portfolio. Above 50% the excess is excluded and the tier is
  recomputed on the reduced figure.

**Art triggers a human gate.** Any portfolio containing art routes to
`art_provenance_gate` regardless of tier. The reviewer must attest to all three:

1. the work was reviewed by a qualified art historian;
2. full provenance exists; and
3. the work has never been subject to a theft or forgery claim.

The attestation is recorded as the gate's decision payload, so the audit trail
carries *who* attested and *when*, not merely that the tier was granted.

## 4. Data separation

Basic client identity is deliberately isolated from financial data. The screening
steps that touch free-text media never receive assets, and the eligibility step
never receives article text. This keeps each agent's input to the minimum the
decision needs — the same discipline the judge configs use.

| File | Holds | Read by |
|---|---|---|
| `data/clients.json` | identity, profession, address | all steps |
| `data/client_financials.json` | income, savings | `compute_eligibility` |
| `data/client_assets.json` | investable, liquid, art, crypto | `compute_eligibility` |
| `data/client_business_interests.json` | stakes, business country | `resolve_sanctions_jurisdiction` |
| `reference/pep_list.json` | PEP register | `pep_check` |
| `reference/sanctioned_individuals.json` | individual sanctions | `individual_sanctions_check` |
| `reference/sanctioned_countries.json` | country programmes | `country_sanctions_check` |
| `reference/adverse_media_articles.json` | headlines + summaries | `extract_entities` |

All data is synthetic. See `workflows/kyc/data/README.md` for the fixture matrix
— each client is built to exercise a specific path, including the failure paths.

## 5. Open questions

1. The Premium tier ambiguity in §3.2. **Blocking** for that step only.
2. `employee_id` was named as a basic client field. Staged as `client_id`, with
   `relationship_manager_id` alongside it, on the assumption that an employee id
   on a *client* record was either a slip or a reference to the owning staff
   member. Trivial to rename — confirm which was meant.
3. Controlling stake is set at ≥ 25% (the common regulatory threshold). Not
   specified in the source requirement.
4. Sanctions reference data here is synthetic and shaped like OFAC's SDN/country
   programmes without being it. A real deployment must pull the actual lists;
   that is an ingestion concern, not a workflow one.
