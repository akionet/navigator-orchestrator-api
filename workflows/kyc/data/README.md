# KYC fixtures — what each client proves

All data is synthetic. Names, companies, addresses and list entries are invented;
none derives from a real person, register or sanctions list.

Every client exists to exercise one path, including the paths that must fail.
`_fixture_note` fields inside the JSON record the intent inline so a fixture
cannot quietly stop testing what it was written for.

| Client | Scenario | Expected outcome |
|---|---|---|
| **CL-0001** Anneliese Vogt | Clean retail client, DE resident, income €135k | `premium` — income clears, savings do not; any-of satisfied |
| **CL-0002** Marcus Feldhoff | Named as **principal** in a fraud investigation (AM-0001) | Adverse media finding, implicating → review |
| **CL-0003** Priya Raghunathan | Defence solicitor named in the *same* case (AM-0002) | **Not implicated.** The false-positive test |
| **CL-0004** Dmitri Sokolov | PEP (deputy minister); 18% stake is below the controlling threshold | PEP gate → human decision; jurisdiction falls through to residency (KZ) |
| **CL-0005** Elena Marchetti | UHNWI whose art + crypto is 58.7% of portfolio | Breaches 50% cap → exclude excess, recompute to €11.5M → still UHNWI; **art gate** fires |
| **CL-0006** Tobias Krenz | HNWI, €2.4M qualifying, crypto excluded at this tier | `private_banking_hnwi` |
| **CL-0007** Farida Nasser | **No country on the address** | `jurisdiction_unknown` **error** — never a silent pass |
| **CL-0008** Viktor Aslanov | Controlling stake (74.5%) in a business operating in Iran | Jurisdiction resolves to **IR (business)**, not CY (residency) → decline |

## The cases worth keeping

Three of these are doing more work than the rest and should survive any future
trimming of the fixture set:

**CL-0003** is the reason adverse media is split across two agents. She appears
in an adverse article about someone else's alleged fraud, because she is the
defence lawyer. Any implementation that matches on names alone flags her. That
failure is not hypothetical — it is the standard failure mode of media screening,
and it degrades the whole control, because a queue full of lawyers and journalists
is a queue nobody reads.

**CL-0007** proves a missing country errors rather than passes. A screening step
that silently succeeds when it cannot determine jurisdiction is worse than no
step at all, because it produces a clean record with nothing behind it.

**CL-0008** proves the jurisdiction rules are *ordered*. His residency (Cyprus)
is unremarkable; his controlling stake operates in a comprehensively sanctioned
country. Check residency first and he passes.

## Near-miss matching

`sanctioned_individuals.json` contains **Viktor Aslanoff** (RU, b. 1970) while
`clients.json` contains **Viktor Aslanov** (CY). One letter apart, different
nationality, different people.

This pair is deliberate and has no "correct" answer baked in. Exact matching
misses genuine hits that use transliteration variants; fuzzy matching floods the
queue with false positives. Whichever is implemented must be a documented,
deliberate choice with a stated threshold — not an accident of whichever string
comparison was reached for first. CL-0008 is declined on country grounds anyway,
so this fixture tests the matcher without the outcome depending on it.
