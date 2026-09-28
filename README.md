# SVC-C2-033 — Tour Planning & Procurement Routing Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

This agent supports a travel agency's product-planning team. It joins the agency's own internal
knowledge — past-tour performance, sales figures, the supplier master — with public tourism data,
then does two things: it drafts a tour plan grounded in the patterns that have actually worked for
that agency, and it turns the plan into a procurement action list saying who to contact, on what
terms, and what to provisionally hold.

It suits planning work that today depends on an individual's experience: producing a first plan
draft for a new product quickly and from the agency's own record, matching required components
against the in-house supplier master, and standardising the path from concept to procurement
preparation so it does not vary by who happens to be planning.

Two design decisions shape everything else:

- **A human approves the plan before any procurement step runs.** The draft pauses the pipeline
  and the run comes back as awaiting-human. The procurement action list is produced only after a
  person has approved or corrected the draft — the agent never routes procurement off its own
  output.
- **Supplier contact details and terms never enter persisted state.** Everything inside the graph
  works on supplier *references*. Because a checkpointer persists state at every super-step,
  resolving a contact inside a node would write it into the checkpoint database; instead the
  resolution happens in the API adapter, on the invocation's return value, which is not
  checkpointed. The output gate independently asserts that no resolved sensitive value is present
  in what the graph returns.

Internal knowledge arrives as spreadsheet data — an agency without a structured knowledge base
can use it as it is.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- caller authentication in front of the entry adapter;
- your own knowledge base and supplier master. The repository ships **sample seed files**
  (`config/past_tours.sample.json`, `config/suppliers.sample.json`) so the agent runs out of the
  box; blank those paths and point `TOUR_KB_DB_PATH` at a populated database for real use;
- the secrets named in the manifest: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_DEPLOYMENT` and `TOUR_KB_DB_PATH`.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `tenant_id` (`default`) | Tenant identifier used to separate rows in the database. A deployment configuration value, not a per-request field |
| `market_source_url` | Public tourism data endpoint; replaceable |
| `memory_enabled` (`true`) / `hitl.enabled` (`true`) | Human review is part of the pipeline and needs a checkpointer |
| `hitl.max_hitl` (`8`) | Cap on plan-revision cycles per session |
| `sample_data.*` | Seed files described above |
| `max_retry` (`3`) / `timeout_seconds` (`30`) | Graph backbone tunables |

If the public tourism source is unavailable, planning proceeds from internal history alone.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent runs inside the platform runtime and takes its secrets from the platform secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when it is absent. Without the framework package installed, `src/api/server.py` cannot be
imported at all — it imports `framework.*` at module level, so start-up ends in `ImportError`
rather than in a partially working server. The test suite runs without a platform connection.

## Input

Three routes:

| Route | Body | Purpose |
|---|---|---|
| `POST /invoke` | `input`, `session_id` | Start a planning run |
| `POST /resume` | `thread_id`, `feedback`, `session_id` | Approve or correct the paused plan draft |
| `GET /health` | — | Liveness |

`input` is a **JSON string** carrying the planning request — area, theme, season and an explicit
`consent` flag. Consent is validated at the input gate: a request that declares `consent: false`
does not proceed. Free-text input is capped at 4,000 characters and rejected outright if it
carries personal data, on top of the framework's own scan.

Callers must be authenticated. A verified trust level from upstream middleware is used as-is;
otherwise `INVOKE_AUTH_TOKEN` Bearer authentication promotes the caller to `VERIFIED_EXTERNAL`,
with a mismatch answered by **401**. An unauthenticated caller is rejected with 401 rather than
reaching the graph, and a caller below verified trust with **403**.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "session_id": "s1",
    "input": "{\"area\":\"箱根\",\"theme\":\"温泉\",\"season\":\"2026-autumn\",\"consent\":true}"
  }'
# → awaiting_human, with the plan draft and a thread_id — approve or correct it, then /resume
```

`/resume` is guarded on four counts: the thread must exist, the session must own it, the caller
must own it, and the thread must actually be waiting on human input. A resume replayed after the
run already completed is rejected with **409** rather than re-running the pipeline. Check and
resume are serialised per thread inside the process; a durable multi-process deployment must add
a per-thread lease or compare-and-set in the shared checkpoint store.

## Output

The standard AgentCore envelope is preserved. `output` carries `plan_draft` and
`procurement_actions`. Inside the graph those actions carry supplier **references** only; the API
adapter resolves contacts and standard terms into the response on the way out, so the values
appear in what the caller receives and never in the checkpoint database.

While the plan draft is awaiting approval, the run returns as awaiting-human with the draft and a
`thread_id` instead of a procurement list.

## Security and Limitations

The entry node requires verified-external trust and consent is checked before planning starts.
Input is size-bounded and rejected if it contains personal data. The output gate asserts, as
defence in depth, that no resolved sensitive supplier value is present in the graph's own output —
the resolution deliberately happens outside the checkpointed path.

Known limits:

- **Sensitive data is protected in the checkpoint, not after delivery.** Once the response leaves
  the agent, distribution and retention of supplier contacts and terms are the operator's
  responsibility under their own information-handling policy.
- **Per-thread serialisation is in-process.** A multi-Pod deployment needs a shared lease or CAS;
  without one, two concurrent resumes on one thread are not mutually excluded.
- **The public tourism source is third-party.** Its accuracy and availability are outside this
  agent's control, and a fetch failure silently narrows planning to internal history.
- **Plan quality tracks the agency's own record.** With little past-tour data, the draft has
  little to be grounded in.

Output is machine-generated and assistive. Plans and procurement actions are for use **after**
human review, and the commercial decisions remain the planner's.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm`
- HITL: **enabled** (`max_hitl: 8`), with `/resume`
- Persistence: checkpointing enabled for pause/resume; sensitive supplier values are resolved
  outside the checkpointed path
- Secrets: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
  `TOUR_KB_DB_PATH`

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using fakes for the model and the knowledge base.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the HITL design and the
sensitive-value handling rationale. `docs/03_test_spec.md` records the test specification and the
verification results, including a live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
