"""Ingest stage: turn an API description into an APISpec.

Two paths produce the identical contract:

    openapi.py  deterministic parse of an OpenAPI 3.x document
    docs.py     LLM extraction from human-written API docs (for APIs with no spec)

Everything downstream (design, generate, verify) is source-agnostic.
"""

from toolsmith.ingest.docs import spec_from_docs
from toolsmith.ingest.openapi import spec_from_openapi

__all__ = ["spec_from_openapi", "spec_from_docs"]
