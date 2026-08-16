# Build log — prompts and decisions

The assignment encourages using coding agents and asks candidates to keep their
prompts/threads. This project was built in Claude Code sessions; this file logs
the substantive prompts and the decisions they produced, in order. The full
session transcripts are available on request.

## Session 1 — analysis and project selection

**Prompt (paraphrased):** "Here is the take-home PDF. Give me your comprehensive
analysis, then let's discuss how to structure this project. My interview with
Arcade figured heavily around the agentic generation of MCP servers and tools."

Decisions made in discussion (human-selected at each fork):

1. **Project:** a meta-agent ("Handyman") that turns an API description into a
   working, eval-gated Arcade MCP server — chosen over a conventional
   catalog-tools personal agent because it targets the problem the interview
   conversations centered on, and the real-life problem is genuine: agents
   can't use the niche APIs in my life, and hand-writing MCP tools takes hours.
2. **Role framing:** AI engineer → emphasize productization (typed contracts at
   every LLM boundary, evals as a publish gate, feature pitch grounded in
   friction actually hit during the build).
3. **Demo targets:** National Weather Service (`api.weather.gov`, OpenAPI
   ingest, no auth — chosen because its raw API is awkward enough that agentic
   tool *design* visibly beats 1:1 endpoint wrapping) + Hacker News (docs-page
   ingest, no auth — generality proof in a second domain). Chosen over TMDB to
   keep the reviewer's setup to a single Anthropic key.
4. **Scope cuts declared up front:** OAuth flows declared but not exercised by
   the generator; no pagination/streaming handling; composite tools limited to
   declarative two-step chains; evals test tool selection, not execution
   correctness.

## Session 1 — build

**Prompt (verbatim):** "OK build it. Remember, I'm going to be expected to
evaluate this code live in an onsite interview, so the code must be structured
cleanly and be optimized for readability with clear documentation."

Build proceeded stage by stage (scaffold → IR/ingest → design → generate →
verify → agent → docs ingest → docs/writeup), with the coding agent verifying
each stage against the live `arcade_mcp_server` package and real APIs before
moving on. Notable mid-build findings are logged below as they occurred.

- `arcade_mcp_server` namespaces served tool names (`greet` on server `hello`
  is listed as `Hello_Greet`), so nothing downstream hardcodes tool names —
  the eval runner and demo agent resolve names from the live `tools/list`.

## Session 2 — provider pivot, tracing, project name

**Prompt (paraphrased):** "Configure the project to run on my own model
credits, wire in the monitoring stack I use elsewhere (pydantic-ai + Logfire),
and then let's design an eval for the finished workflow. Also: the project is
now called 'handyman'."

Decisions:

1. **Model provider → AWS Bedrock.** The original plan assumed a direct
   Anthropic API key; my existing infrastructure runs Claude via Bedrock, so
   the pipeline moved to Bedrock-hosted Claude under a project-scoped,
   invoke-only IAM user. Reproducibility note: Claude 5 models on Bedrock
   require an explicit foundation-model agreement before first invocation
   (`create-foundation-model-agreement`); invoke permissions alone produce a
   misleading marketplace-authorization error.
2. **LLM layer → pydantic-ai.** Chosen over a minimal SDK swap because it
   turns the provider into a config string (bedrock/anthropic/openai) instead
   of code, natively enforces schema-validated outputs with retries, and
   bridges MCP-over-stdio for the consumer agent — a bare OpenAI-SDK swap
   would have meant hand-rolling the agent's tool loop, since that SDK has no
   local-stdio MCP bridge.
3. **Eval gate stays Arcade-native.** `arcade_evals` drives an openai- or
   anthropic-style client itself; Bedrock rides through the anthropic SDK's
   Bedrock client, which `arcade_evals` accepts unchanged because it
   duck-types the Messages API. The reviewer-key story improves: any one of
   AWS creds, an Anthropic key, or an OpenAI key runs the whole pipeline.
4. **Tracing: Logfire, token-gated.** With `LOGFIRE_TOKEN` set, every
   pipeline stage and the demo agent stream spans; without it, tracing is a
   no-op — the reviewer path stays zero-config.

**First full live run (NWS, Bedrock sonnet-5).** Design exceeded the plan on
attempt 1 — six tools including three 2-call chains (forecast, hourly, and a
current-conditions chain through station discovery we hadn't scripted), the
User-Agent scheme as an optional secret with a default, 63 rejections with
sound one-clause reasons. The eval gate then failed 3/12 cases and the
revision loop fired — which is the gate doing its job, and diagnosing the
failures produced three permanent fixes:

1. **Expected-value typing.** The IR stores expected argument values as
   strings (a structured-outputs constraint), but the model under evaluation
   sends what the tool schema declares — so `'-74.0060'` (string) could never
   equal `-74.006` (float) under an exact-match critic. The gate now re-types
   expected values against the plan's declared argument types before scoring.
2. **Examiner multi-intent leak.** Cases like "what's the forecast — and any
   storm warnings I should worry about?" imply two tool calls but expected
   one; the runner hard-fails on call count. The examiner now has a
   one-intent-per-message rule.
3. **Upstream caching bug in `arcade_evals`.** Stdio tool listings are
   memoized by command line, so every revision attempt was scored against the
   *first* attempt's server — attempt 2's score was quietly tainted and
   attempt 3 crashed on a tool-name mismatch. The gate clears the cache per
   run; worth an upstream issue/PR to Arcade.

Also observed: given failure feedback, the first design revision *removed*
tools (6 → 4) rather than sharpening wording — the revision instructions now
state that argument-format failures are description problems, not grounds for
dropping a tool.

**The gate passed 14/14 — and the demo still crashed.** The rerun's design
(seven tools, three 2-call chains) cleared the eval gate on attempt 1, then
every tool call failed at runtime: the design had honestly declared the
spec's `API-Key` scheme as an optional secret with no default, and the
generator rendered it as a `None` request header, which httpx rejects with a
TypeError. Fixed in the template (unset optional secrets are omitted
entirely) with a fixture regression test; the NWS demo then worked
end-to-end — live chained calls, real forecast, useful answer. This failure
is the clearest possible demonstration of the declared scope cut ("evals
test tool selection, not execution correctness"): the selection gate cannot
see an execution bug. It directly motivates the finished-workflow eval
designed in the next step.

**Hacker News (docs ingest) first run.** The LLM extraction read the raw
README into 10 endpoints; design produced the predicted primitive surface
(ID-list tools + get_item — fan-out chains are a declared cut) with two
articulate rejections. The static gate then earned its keep by flagging a
real generated-code defect: with no secrets at all (HN is keyless), the
template emitted an unused `import os`, and the F-lint pass caught it.
Honest limitation observed en route: static failures feed the design-
revision loop, but a template defect isn't something design can fix — the
loop was harmless yet useless here. Template made conditional; a
no-secrets rendering test added.

## Session 3 — cost discipline

**Prompt (paraphrased):** "Would it be cheaper to run on GPT-5.6 Sol?" — it
would not (Sol lists at $5/$30 per MTok vs sonnet-5's $3/$15, and an OpenAI
switch re-opens validation), so instead: "do both prompt caching and a haiku
eval gate."

Decisions and results:

1. **The eval gate's case model dropped to haiku-tier** (env-overridable,
   provider-aware default so a single reviewer credential still works).
   This is cheaper *and* stricter: the gate asks whether tool descriptions
   steer a fresh consuming model correctly, and a weaker model raises that
   bar. Replaying the committed NWS suite on haiku: **14/14 — the
   descriptions hold up even for a small model.** The examiner authoring
   cases stays on the pipeline model; judgment work isn't downgraded.
2. **Bedrock prompt caching on the multi-turn surfaces.** The demo agent
   caches its tool schema, system prompt, and growing history (the fan-out
   demo now reports ~half its input tokens as cache reads, billed at ~10%
   of fresh input); the eval gate marks its per-case repeated tool schema
   cacheable via a small client adapter. Single-shot stages (design,
   examiner) get nothing from caching and correctly skip it.
3. **Reproducibility note:** Bedrock's Converse and InvokeModel APIs
   enforced marketplace entitlement differently for haiku — Converse worked
   immediately while InvokeModel (the anthropic SDK's path) returned a
   misleading marketplace-authorization error until a marketplace-authorized
   principal performed one first invoke to materialize the subscription.
