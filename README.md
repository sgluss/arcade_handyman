# Handyman

Handyman is a meta-agent: point it at an API description — an OpenAPI spec or
a human-written docs page — and it designs, generates, and **eval-gates** a
working [Arcade MCP](https://docs.arcade.dev) server, then proves the result
by letting a consumer agent solve a real task with the generated tools.

The pipeline is deliberately hybrid. Stages that need judgment (which tools
deserve to exist, how to hide an API's internal indirection, what the
descriptions should say) are LLM stages emitting schema-validated plans;
stages that must be trustworthy (code generation, verification) are
deterministic. No model ever free-writes server code.

```
   openapi.json ──► ingest/openapi.py   deterministic spec walk
   docs page ─────► ingest/docs.py      LLM extraction, same contract
                        │
                        ▼  APISpec — the mechanical inventory, no opinions
                    design.py           LLM judgment: tool selection, 2-call
                        │               chains, descriptions, secrets, and
                        │               explicit rejections
                        ▼  ToolPlan — schema-validated design decision
                    generate.py         deterministic codegen: validated
                        │               slots in a reviewed template
                        ▼  server.py + plan.json + README.md
                    verify/             static ► boot ► execution smoke ►
                        │               tool-selection evals (Arcade's evals
                        │               framework vs the live server)
                        │
                        └──failures──► back to design.py (≤3 attempts)
```

A server is not "done" when it renders — it is done when every tool has
survived a real API call and a fresh model, shown only the served tools,
picks the right tool with the right arguments for realistic asks. If the
loop cannot converge, the server is still emitted but loudly marked, with
the failing cases written next to the code.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), plus **one** LLM credential
of your choice — AWS/Bedrock, Anthropic, or OpenAI (see below).

```bash
git clone <this repo> && cd arcade_takehome
uv sync
cp .env.example .env        # pick a model line, add your credential
```

Generate a server from an OpenAPI spec (the National Weather Service — its
point→gridpoint indirection is the tool-design showcase), then let the
consumer agent use it:

```bash
uv run handyman generate https://api.weather.gov/openapi.json
uv run handyman demo weather_gov
```

(The design stage names the server — a recent run chose `weather_gov`; the
`GENERATE` stage prints the name it picked. `handyman demo` takes that name,
or a path to any generated `server.py`.)

Generality proof — same pipeline, no spec, just the Hacker News README:

```bash
uv run handyman generate https://raw.githubusercontent.com/HackerNews/API/master/README.md \
    --docs --base-url https://hacker-news.firebaseio.com/v0
uv run handyman demo hackernews
```

Every stage prints what it decided (tools designed, endpoints rejected,
smoke calls, eval scores), so a `generate` run is watchable end-to-end.

### No credential handy?

The generated artifacts for both demo targets are committed, so you can
review everything without a key:

- `uv run pytest tests/ -q` — the full deterministic machinery, including an
  end-to-end execution-smoke run against a local stand-in API (no network,
  no LLM).
- Read `generated/weather_gov/` and `generated/hackernews/` — the servers,
  their design decisions, and the eval evidence they shipped with.
- Run a generated server directly:
  `uv run generated/weather_gov/server.py stdio` (they are standalone
  arcade-mcp servers; no LLM involved).

## What gets generated

```
generated/weather_gov/
├── server.py    the arcade-mcp server: one tool per planned capability,
│                chains hidden behind single tools, typed error taxonomy
├── plan.json    the design stage's full decision — including every endpoint
│                it rejected, with reasons
├── evals.json   the gate's evidence: examiner cases, execution-smoke
│                outcomes, and tool-selection scores against the live server
└── README.md    provenance, tool table, secrets table, regeneration command
```

`plan.json` and `evals.json` are the honesty artifacts: what was decided and
why, and what was verified and how, kept next to the code they justify.

## The verify gate

Four checks, cheapest first:

1. **Static** — the file compiles and passes correctness-only lint rules.
2. **Boot** — the server starts over stdio and serves the planned tools.
3. **Execution smoke** — every tool is called once against the live API,
   with typed arguments taken from the examiner's cases. Selection evals
   can't see execution bugs (a tool can read perfectly and crash on its
   first real call); this stage exists because exactly that happened.
4. **Selection evals** — an examiner LLM that sees only the consumer surface
   (never the design rationale) writes realistic cases; Arcade's
   `EvalSuite` runs them against the live server with a fresh model and
   scores tool choice and arguments.

Failures from any stage come back as prose and feed a bounded
design-revision loop.

## Providers and cost

Every LLM stage follows `HANDYMAN_MODEL` — a
[pydantic-ai model string](https://ai.pydantic.dev/models/), so
`bedrock:...`, `anthropic:...`, and `openai:...` are drop-ins with the
matching credential. The eval gate's consumer model defaults to haiku-tier
on your provider: cheaper, and a *stricter* test — descriptions that steer a
small model steer anything.

A full regeneration of a target costs roughly $1 on claude-sonnet-5
(design + examiner), with the gate on haiku. Prompt caching is wired at
every repeated surface and engages when the model id supports it — with one
measured caveat: Bedrock silently ignores cache markers when the model id
is an application inference profile ARN, so per-project cost attribution
currently costs you caching (details in the writeup). Demos print a usage
line so cost stays visible either way.

Optional: set `LOGFIRE_TOKEN` to stream traces of every stage (and every
tool call the demo agent makes) to [Logfire](https://logfire.pydantic.dev);
without it, tracing is a silent no-op.

## Repo map

```
src/handyman/
├── ir.py            typed contracts between all stages — read this first
├── ingest/          API description → APISpec (openapi.py deterministic,
│                    docs.py LLM with the same output contract)
├── design.py        APISpec → ToolPlan (the judgment stage)
├── generate.py      ToolPlan → server.py via templates/server.py.j2
├── verify/          static.py, smoke.py, evals.py — the gate
├── agent.py         the consumer agent used by `handyman demo`
├── llm.py           one place where model selection and schema-validated
│                    LLM calls live
└── cli.py           the conductor: generate's revision loop, demo
```

Scope decisions, tradeoffs, and the feature suggestion for Arcade are in
[WRITEUP.md](WRITEUP.md). Substantive prompts and mid-build findings are
kept in [prompts/build-log.md](prompts/build-log.md).
