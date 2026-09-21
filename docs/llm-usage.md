# LLM usage — models, triggers, cost

Which model each feature uses, what triggers it, and what a call costs. It
lives in the repo so the cost of a feature is visible next to the feature.

All figures are order-of-magnitude, measured against the prices hard-coded as
constants in each module. Check those constants against current pricing before
trusting a number here; `tests/test_model_config.py` at least keeps the
constants consistent with the pinned model.

Two models are in play: **Haiku 4.5** at \$1/\$5 per MTok for classification and
extraction, **Sonnet 5** at \$2/\$10 for the two paths where language quality or
tool orchestration matters. Haiku 4.5 is the current Haiku, not a leftover —
there is no Haiku 5.

## Overview

| Feature | Model | Trigger | ~$/call |
|---|---|---|---|
| **Triage** (0–10 fit) | Haiku 4.5 | button in the job drawer | 0.001–0.002 |
| **Briefing** (company/role/fit) | Haiku 4.5 + `web_search` | button in the job drawer | 0.005–0.02 |
| **Apply-method detection** | Haiku 4.5 + `web_search` | button in the job drawer | 0.01–0.02 |
| **Experience extraction** | Haiku 4.5 | backfill script, nightly | 0.001–0.002 |
| **Company classification** | Haiku 4.5, 20 per call | `classify-companies`, nightly | 0.001 per 20 companies |
| **URL extraction** | Haiku 4.5 | manual-add modal | 0.002–0.005 |
| **Smart filter** | Sonnet 5, effort `low` | Apply in the browse sidebar | 0.005–0.015 |
| **Chat** | Sonnet 5 (+ `web_search` in interview prep) | Send in the drawer chat | 0.01–0.06 |
| **Reply classification** | Haiku 4.5 | `poll-replies`, nightly | 0.001 per reply |

Nothing here runs without being asked, except the nightly maintenance script,
whose steps are all Haiku and all bounded by "only what is new since the last
run".

## Where the cost actually goes

Two mechanisms do most of the work of keeping this cheap.

**Prompt caching.** Every repeated call sends the same large context — the
rules prompt and the CV. Those blocks are marked `cache_control: ephemeral` and
ordered stable-first, so across N postings the expensive part is written once
and read N−1 times at a tenth of the price. Triage is the clearest case: the CV
dominates the input tokens and never changes within a run.

**Batching plus persistent caching.** `company_classifier.py` sends 20
companies per call and stores the verdict in `company_profiles`. A company is
therefore classified once ever, not once per posting. Across a few thousand
postings from a few hundred companies, this is a ~10× difference.

The third mechanism is not a mechanism but a decision: **Haiku unless the task
needs Sonnet.** Classification and extraction are Haiku. Sonnet is reserved for
the two places where language quality or tool orchestration matters — the smart
filter's schema reasoning and the chat.

## Regex before model

`experience_llm.py` is only a fallback. `experience_extractor.py` handles the
explicit cases ("5+ years", "mindestens 3 Jahre") with regex at zero cost and
covers roughly 7–8% of postings. The model is asked only about the rest, and it
is allowed to answer "no reliable signal", which is stored as `NULL`.

The same order applies to workload: `workload_extractor.py` is regex-only and
there is no LLM fallback for it, because a wrong workload is worse than an
unknown one.

## Cached blocks per module

| Module | Cached system blocks |
|---|---|
| `triage.py` | rules prompt, CV |
| `briefing.py` | briefing rules, CV |
| `experience_llm.py` | extraction rules |
| `apply_method.py` | channel and document taxonomy |
| `smart_filter.py` | filter grammar, profile background |
| `chat.py` | mode prompt, job context |

## When a call costs more than one call

The briefing can spend more than its base figure, for two reasons that are both
deliberate.

**Research.** With `with_research=True` (the default) it runs one to three
searches before writing, which is most of its cost. Passing `with_research=False`
drops it back to a single cheap call; the company summary then works from the
posting alone and says so.

**Repair rounds.** If a company fact cites a URL no search returned, the
submission is rejected and sent back with the objection — up to
`MAX_REPAIR_ROUNDS` times. Each round is a full call, and the reported cost
accumulates across all of them rather than showing only the last. A briefing that
needed two rounds costs roughly three times one that needed none. That is the
price of not publishing an invented claim, and it is visible in the logs.

## Running without a key

`ANTHROPIC_MOCK=1` routes every client through `src/llm.py`'s mock, which
replays canned responses from `fixtures/llm/` keyed by the tool the request
asks for. Nothing leaves the machine, and no call is billed.

```bash
ANTHROPIC_MOCK=1 python -m src.main --profile example classify-companies
ANTHROPIC_MOCK=1 uvicorn dashboard.app:app
```

The fixtures are hand-written, and each one says so in its `_comment`. They
reproduce the *shape* of a real response — tool-use blocks, the four usage
counters, server-tool and search-result blocks — so the parsing and the cost
arithmetic are exercised honestly. They say nothing about answer quality.

One case cannot be a static file: the company classifier sends a batch and
matches answers back by name, so a fixture would fit exactly one batch size.
That one has a small handler in `src/llm.py` that reads the names out of the
prompt and answers about those, which keeps the batching and name-matching code
under test.

## Keeping an eye on it

Every module returns its own token counts and computed cost, and the dashboard
shows the per-call cost next to each chat message. `audit_log` records
operations, so `docs/sql-recipes.md` has queries for what ran when.

If a bill looks wrong, the usual cause is a cache miss from reordering system
blocks — a variable block placed before a stable one invalidates everything
after it.

## Model ids and the things attached to them

The model ids are string constants at the top of each module in `src/agent/`.
They are pinned deliberately: a silent model change would move scores without
any config change, and the deterministic score is supposed to be the stable
half of this system. Bumping one is a deliberate act that should be followed by
a `rescore` and a look at whether the triage scores still mean the same thing.

Three constraints travel with the model and are easy to get wrong, so they have
tests in `tests/test_model_config.py`:

**`max_tokens` covers thinking as well as the answer.** Sonnet 5 runs adaptive
thinking by default, so a limit sized for a thinking-off model truncates. The
two Sonnet callers therefore state `thinking` explicitly and carry a budget with
room in it — the pre-migration values (600 for the filter, 2048 for the chat)
would have been cut off mid-tool-call.

**Effort is a per-route decision.** The smart filter runs at `low`: it is a
schema-constrained extraction behind a button, where the tool schema already
enumerates every legal value, and latency is visible to the user. The chat keeps
the default.

**Server-tool versions are tier-dependent.** The chat uses
`web_search_20260209`, which brings dynamic filtering and is available on the
Sonnet tier. The apply-method detector runs on Haiku and keeps the basic
`web_search_20250305`. Sending the wrong variant is a 400 — and only in the
branch that actually searches, which is exactly the kind of error that reaches
production.

**Sampling parameters are not accepted.** No `temperature`, `top_p` or `top_k`
anywhere; tone and determinism are steered through the prompt and the tool
schema instead.

One consequence of the current tokenizer worth knowing before reading a cost
dashboard: the same text produces roughly 30% more tokens than on the previous
Sonnet generation, while the per-token price is lower. Token counts measured
against an older model are not a baseline for this one.
