# Handyman — decisions, tradeoffs, and a feature suggestion

The [README](README.md) covers what Handyman is and how to run it. This
document covers why it is shaped the way it is: the decisions, what each one
cost, what was cut, and — grounded in friction actually hit during the build —
one feature I think Arcade should own. The chronological account of the build,
including which prompt produced which decision, is in
[prompts/build-log.md](prompts/build-log.md).

## Why this project

My interviews with Arcade kept returning to one topic: agentic generation of
MCP servers and tools. It is also the honest version of "a real problem in
your life": the APIs I actually want my agents to use are mostly niche ones
with no MCP server, and hand-writing a good one takes hours per API. So the
project is a meta-agent. Handyman turns an API description into a working,
eval-gated Arcade MCP server, and a deliberately thin consumer agent then
proves the result on a real task.

The scope bet followed the assignment's own weighting — a small thing that
works over a grand thing that doesn't. Two live targets are carried all the
way through: the National Weather Service (OpenAPI ingest; its
point→gridpoint indirection is the tool-design showcase) and Hacker News
(docs-page ingest; the generality proof, since the long tail of useful APIs
has no spec at all).

## The architecture bet: hybrid by stage

The organizing rule: **use an LLM exactly where the work is judgment, keep
everything checkable deterministic, and verify at the boundary between.**

- *Judgment stages* — reading human docs into an inventory, deciding which
  tools deserve to exist, writing descriptions, authoring eval cases — are
  LLM calls that must emit a schema-valid object or fail loudly.
- *Mechanical stages* — parsing OpenAPI, rendering code, static checks,
  scoring — are ordinary deterministic Python.
- *The boundary* is a typed IR (`src/handyman/ir.py`): `APISpec` (what the
  API offers — facts, no opinions), `ToolPlan` (what the server should be —
  the design decision), `EvalSuite` (how to check it reads correctly).

No model ever free-writes server code, and no deterministic stage ever
guesses at judgment. The payoff shows in the two ingest paths: OpenAPI is
parsed deterministically, a docs page is read by a model, and both emit the
same `APISpec` — the pipeline downstream cannot tell which one produced its
input.

## Decisions and what they cost

### One typed IR, and no other inter-stage language

Every LLM boundary goes through a single `parse()` helper that demands a
schema-valid instance of an IR model; no stage consumes free-form model
prose. That makes LLM stages retryable and unit-testable like any other
function, and it makes `ir.py` triple-duty: the data model, the validation
layer, and the first file a reviewer should read.

The cost: structured outputs forbid free-form JSON objects
(`additionalProperties`), so what would naturally be a dict is a list of
named pairs (`ParamBinding`, `Extract`, `ExpectedArg`), and expected eval
values are stored as strings. That last constraint later produced a real
bug — a string `'-74.0060'` can never exactly-match the float `-74.006` an
agent actually sends — fixed by re-typing expected values against the plan's
declared argument types before scoring, in one shared rule used by both
verification stages.

### Generated code comes from a template with validated slots

The design stage's judgment is already captured in the `ToolPlan`, so
generation (`generate.py` + `templates/server.py.j2`) is deterministic
assembly. Free-form LLM codegen was rejected because it trades away the
three properties that matter most for generated artifacts: syntactic
validity by construction, one uniform reviewable style, and diffable
regeneration.

LLM-authored strings enter the file as validated identifiers, Python
literals, or escaped docstrings — never spliced in as code. Proposed
argument defaults are admitted only if they parse as pure Python literals;
anything else is demoted to a string literal. Binding templates may only
reference argument and extract names that exist, and plan defects the
template can catch (an unknown endpoint, an unbound path parameter) raise
errors written to be fed back to the design stage. The threat model here is
a careless model, not a malicious one — the checks exist to catch sloppy
plans deterministically, before anything runs.

### Tools are designed, not wrapped

A 1:1 endpoint wrapper would make the agent learn NWS's internal topology:
call `/points/{lat},{lon}`, read grid coordinates out of the response, call
`/gridpoints/{wfo}/{x},{y}/forecast`. Instead the design stage may chain up
to two calls inside one tool, extracting intermediate values by declared
dot-paths, so the consumer sees `get_forecast(latitude, longitude)` and the
indirection disappears. In the committed NWS server, three of five tools are
two-call chains — including a current-conditions tool that routes through
station discovery, a chain I never scripted.

Selection is judgment too: from 69 NWS endpoints the design kept five tools
and recorded 64 rejections with one-clause reasons in `plan.json`. The
rejections are a first-class artifact — "what we chose not to build" is most
of what tool design is.

One deliberate asymmetry is worth defending: between chain steps, a missing
extract path fails loudly (a wrong path there means the wiring is broken —
an authoring defect, raised as Arcade's fatal error kind), while the final
response pruning is lenient and falls back to the full payload (a bigger
answer beats a broken tool when an API's envelope drifts).

### Verification is a publish gate, not a report

A generated server is not "done" when it renders; it is done when it
survives four checks, ordered cheapest-first: static (compile +
correctness-only lint), boot (serves the planned tools over stdio),
execution smoke (every tool called once against the live API), and
selection evals (Arcade's `EvalSuite` scoring a fresh model's tool choices
against the live server). Failures from any stage become prose fed back to
the design stage for a bounded number of revision attempts. If the loop
cannot converge, the server is still emitted but loudly marked — evidence
of failure next to the code beats silent failure or no artifact.

Two design choices in the gate matter most. First, the examiner LLM that
authors eval cases sees only the consumer surface — tool names,
descriptions, argument docs — never the design rationale or the API
inventory, so it cannot write softball cases that only match the designer's
private phrasing. Second, the gate runs Arcade's own evals framework
against the actually-served tools over stdio, not a simulation — the same
harness a toolkit author would use, pointed at the same wire.

The evidence ships with the code: `plan.json` (every decision, every
rejection) and `evals.json` (the cases, smoke outcomes, and scores) sit
next to `server.py`, so a generated artifact is self-explaining. When smoke
fails, the evidence file says so explicitly rather than leaving selection
scores absent-and-ambiguous.

### Secrets are honest, and zero-config still works

NWS formally declares two apiKey schemes: a real `API-Key` header and a
courtesy `User-Agent`. The design stage maps them differently on purpose.
Real credentials become Arcade-native `requires_secrets` on the tool, so a
missing secret is a clean per-call error in Arcade's own mechanism.
Identification-only values become environment variables with sensible
defaults, so a fresh clone runs with zero configuration. In the committed
regeneration the design carried only the User-Agent scheme — NWS never
actually enforces its declared `API-Key`, and every call in the evidence ran
keyless — so the required-secret path is proven by the generator's fixture
tests instead of a live target. Both flow through the same generic
classifier — nothing in the pipeline special-cases NWS.
OAuth schemes are recorded in the IR but deliberately not generated;
Arcade's managed `requires_auth` is the right home for that, and wiring
OAuth blind from a spec is a product decision, not codegen.

## What the live runs changed

The pipeline was built stage by stage against live APIs, and the sharpest
design decisions came from three failures. The long version is in the build
log; the short version:

**The gate caught bad evals before bad tools.** The first full NWS run
failed 3 of 12 cases — gate bugs, not server bugs — and diagnosing them
produced three permanent fixes: the string-vs-float typing mismatch above,
examiner cases that implied two tool calls while expecting one (the examiner
now has a one-intent-per-message rule), and an upstream caching bug in
`arcade_evals` where stdio tool listings are memoized by command line,
silently scoring every revision attempt against the first attempt's server. The gate now clears that cache
per run; the bug is worth an upstream issue, and I'd file it.

**Then the gate passed 14/14 — and the demo still crashed.** The design had
honestly declared NWS's `API-Key` scheme as an optional secret with no
default, and the generator rendered it as a `None` request header, which
httpx rejects. A selection gate cannot see an execution bug: that is the
exact edge of the declared scope cut "evals test tool selection, not
execution correctness," demonstrated live. It motivated the third gate
stage: execution smoke, which calls every tool once with arguments taken
from the examiner's own cases, retyped exactly as the eval runner types
them. A tool that cannot survive one real call has no business being scored
on its prose.

**Smoke's first replay convicted a shipped server.** Run standalone against
the previously committed servers, smoke failed 3 of 7 NWS tools on the
exact server that had passed selection 14/14: a latent chain bug (the
extract path needed list indexing the walker didn't have — and the design's
chosen path pointed at a station URL rather than its identifier), plus two
tools whose fabricated test identifiers could only ever draw 404s and 500s.
The fixes became rules. The dot-path walkers learned numeric list indexing.
Generated servers now raise Arcade's typed error taxonomy — arcade-mcp
masks plain exceptions as "unhandled error," which had been hiding the
template's careful HTTP error messages from calling agents — and smoke
reads the machine-readable error kind from response metadata instead of
parsing prose. And smoke gained a tolerance rule: a single-call tool that
gets an HTTP error *answer* to a fabricated identifier counts as wired (the
request formed, sent, and parsed; the examiner is blind to the API, so its
IDs are inventions), while chains get no tolerance past live data and auth
errors always fail. Tolerated calls are recorded as such in the evidence —
the committed NWS evidence contains exactly one.

Both targets were then regenerated through the full four-stage gate: NWS
converged on attempt two (smoke 5/5, selection 12/12) after attempt one's
single selection failure fed a revision; Hacker News passed on attempt one
(smoke 8/8, selection 17/17). One revision-loop guardrail also came from
watching it run: given failure feedback, the first design revision removed
tools rather than sharpening their wording, so the revision instructions
now state that argument-format failures are description problems, not
grounds for dropping a tool.

## Operational choices

**Provider is configuration, not code.** Every judgment stage follows one
pydantic-ai model string (`bedrock:…`, `anthropic:…`, `openai:…`), so a
reviewer needs any one credential. The eval gate is the interesting seam:
`arcade_evals` drives a provider SDK itself, and Bedrock-hosted Claude rides
through the anthropic SDK's Bedrock client, which the framework accepts
unchanged because it only duck-types the Messages API.

**The gate's consumer model is deliberately weak.** Eval cases are answered
by a haiku-tier model by default (env-overridable; keyed to the pipeline's
provider, where a weak-tier mapping exists). This is cheaper per case and a
*stricter* test of the thing the gate actually measures: descriptions that
steer a small model correctly steer anything. The committed NWS evidence was
scored by that haiku consumer — 12/12 in `generated/weather_gov/evals.json` —
and the superseded 14-case suite had also replayed clean when the gate first
dropped to haiku. Judgment stages (design, examiner) stay on the stronger
pipeline model. A full regeneration of a target costs roughly $1.

**Cost attribution versus caching — a measured tradeoff.** This project
runs Bedrock through Project-tagged application inference profiles, which
give it its own cost line and metrics namespace, with the runtime role
narrowed to only those profiles. Prompt caching is wired at every repeated
surface (the demo agent's tool schema, system prompt, and history; the eval
runner's per-case tool schema). The demo's visible usage line then exposed
a fact I could not find documented: Bedrock silently ignores cache markers
when the model id is an application-profile ARN — an A/B probe showed 1,689
tokens written and read back through the bare model id, and zeros through
the profile, same request. At this scale attribution wins and the profiles
stay; the tension goes into the feature suggestion below, because a tool
platform should not make you choose.

**Observability is token-gated.** With `LOGFIRE_TOKEN` set, every pipeline
stage and demo tool call streams spans to Logfire; without it, tracing is a
no-op and the reviewer path stays zero-config. Generated servers themselves
are deliberately untraced — they must stay zero-dependency subprocesses.

## What was cut, and why

| Cut | Why | Where it belongs |
|---|---|---|
| OAuth generation | Wiring OAuth blind from a spec is a product decision; declared schemes are recorded in the IR and skipped | Arcade's managed `requires_auth` |
| Pagination / streaming | Real work, orthogonal to the thesis; today's tools return one pruned response | A later `CallStep` capability |
| Request bodies | Ingest records write endpoints without their body schemas; both demo APIs are read-only, so no committed tool is affected | Body params in the ingest contract, then codegen |
| Fan-out chain steps | "Top N stories with details" wants a `foreach` step; considered, rejected for scope. The design emits primitives and the demo agent composed two dozen parallel `get_item` calls itself — acceptable, but it spends agent turns on plumbing | The chain IR, eventually |
| Chains beyond two calls | Two covers the resolve-then-fetch pattern that dominates real APIs; more invites planning inside codegen | Unclear it's ever needed |
| Execution-*correctness* evals | Smoke proves wiring, not payload truth; judging answer quality needs a judged workflow eval | The feature below, tier 4 |
| Arcade cloud deploy of generated servers | No Arcade API key on hand during the build | `arcade deploy` — the servers are already arcade-mcp-native, so this is configuration, not code |

## The feature suggestion: self-serve, eval-gated toolkit generation

**Arcade should let anyone turn an API description into a published,
eval-gated toolkit — "bring your API" as a platform feature.** Paste an
OpenAPI URL or a docs page; an agentic pipeline designs the tool surface,
generates the server against Arcade's SDK, and runs it through a tiered
publish gate; what lands in the catalog carries its evidence.

This take-home is the feasibility argument: one person built the skeleton —
ingest, design, codegen, a four-stage gate, live-verified on two real APIs —
inside the assignment's ~6 hours. But everything that made it hard is
platform-shaped, which is exactly why it should be Arcade's rather than
every user's weekend project:

- **Auth is the wall a local pipeline cannot climb.** My generator stops at
  OAuth by necessity. Arcade already owns the managed-auth machinery
  (`requires_auth`, provider integrations) — a generation pipeline inside
  the platform can map declared OAuth schemes onto real flows instead of
  rejecting them. That single difference moves generation from "keyless
  public APIs" to "the APIs people actually need."
- **Evals as catalog trust, not private diligence.** Toolkit quality is
  invisible at browse time today. A tiered publish gate — static checks →
  execution smoke → tool-selection evals → judged workflow evals on
  realistic tasks (the tier I cut) — attached to every catalog listing
  turns "this toolkit works" from a claim into shipped evidence:
  `plan.json` and `evals.json` are small previews of what that page could
  show. Arcade already has the evals framework; hosting the harness and
  publishing the scores is the missing product.
- **Per-toolkit cost attribution, without the caching tax.** Builders will
  ask what each toolkit costs to run. I measured the naive answer's price:
  attributing at the model-provider layer (Bedrock application profiles)
  silently disables prompt caching. A platform that meters at its own
  gateway can attribute per toolkit *and* keep caching — an operational
  feature the model providers currently make an either/or.

The rationale is catalog economics: the long tail of APIs is where agents
need tools most, hand-authoring toolkits scales with engineering headcount,
and generation gated by evals scales with compute while keeping the
catalog's quality bar visible instead of assumed. I would also contribute
two small findings from this build upstream regardless: the `arcade_evals`
stdio listing-cache bug (real, reproducible, one-line workaround), and the
ergonomics of arcade-mcp masking unhandled exceptions — surfacing typed
tool errors by default would improve every generated and hand-written
server alike.
