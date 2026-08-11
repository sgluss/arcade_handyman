"""LLM extraction of an APISpec from human-written API docs. See task: docs ingest."""

from toolsmith.ir import APISpec


def spec_from_docs(url: str, base_url: str | None = None) -> APISpec:
    raise NotImplementedError("docs ingest lands with the Hacker News demo")
