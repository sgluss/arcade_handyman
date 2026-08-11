"""LLM extraction of an APISpec from human-written API docs.

The long tail of useful APIs has no OpenAPI spec — a README is the interface
(Hacker News, most self-hosted services). Extraction is judgment work, so it
is an LLM boundary like design: the model reads the docs and must emit a
schema-valid APISpec. Everything downstream is identical to the spec-first
path — the pipeline cannot tell which ingest produced its input.

The caller supplies the base URL. Docs pages state it inconsistently or not
at all, and a wrong base URL fails at the first live call rather than at
parse time — not a guess worth delegating to a model.
"""

import re

import httpx

from toolsmith.ir import APISpec
from toolsmith.llm import parse

_FETCH_HEADERS = {"User-Agent": "toolsmith (https://github.com/sgluss)"}
_MAX_DOC_CHARS = 60_000

EXTRACTION_SYSTEM = """\
You extract a mechanical API inventory from documentation. Report only what
the docs actually state — no invented endpoints, parameters, or auth.

- One endpoint per documented operation: id (short snake_case), HTTP method,
  and path with `{param}` placeholders for path parameters.
- Record each parameter's location (path/query/header), type, whether it is
  required, and its documented meaning. Include enum values when listed.
- Summarize documented example responses into a shallow response_hint sketch
  like `{by, title, kids: [...]}` so a tool designer knows the shape.
- Record auth schemes only if the docs describe one.
- `base_url` will be overridden by the caller; leave it empty if unstated.
"""


def spec_from_docs(url: str, base_url: str) -> APISpec:
    """Fetch a docs page and extract an APISpec from it."""
    text = _fetch_text(url)
    spec = parse(
        f"Extract the API inventory from this documentation page ({url}):\n\n{text}",
        APISpec,
        system=EXTRACTION_SYSTEM,
    )
    spec.base_url = base_url.rstrip("/")  # caller-supplied, authoritative
    return spec


def _fetch_text(url: str) -> str:
    response = httpx.get(url, headers=_FETCH_HEADERS, follow_redirects=True, timeout=30)
    response.raise_for_status()
    text = response.text
    if "<html" in text[:1000].lower():
        text = _strip_html(text)
    return text[:_MAX_DOC_CHARS]


def _strip_html(html: str) -> str:
    """Good-enough HTML -> text for docs pages; a rendering-grade converter
    would be more dependency than this path deserves."""
    html = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", html)
