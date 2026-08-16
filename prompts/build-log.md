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
