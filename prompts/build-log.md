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
