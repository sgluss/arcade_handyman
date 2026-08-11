"""Shared LLM plumbing for the pipeline's judgment stages.

Every LLM boundary in this project goes through `parse`: the model must emit a
schema-valid instance of a contract from `ir.py` (or fail loudly). No stage
consumes free-form model prose — that discipline is what makes the LLM stages
retryable and unit-testable like any other function.
"""

import os
from typing import TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# One model for every judgment stage; override for experiments without edits.
MODEL = os.environ.get("TOOLSMITH_MODEL", "claude-opus-5")

_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


def parse(prompt: str, output: type[T], *, system: str, max_tokens: int = 16000) -> T:
    """Ask the model for a schema-validated `output` instance.

    One retry on a malformed response, with the validation error shown to the
    model — in practice structured outputs make this path rare.
    """
    messages = [{"role": "user", "content": prompt}]
    for attempt in (1, 2):
        response = client().messages.parse(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_format=output,
        )
        if response.parsed_output is not None:
            return response.parsed_output
        if attempt == 1:
            text = next((b.text for b in response.content if b.type == "text"), "")
            messages += [
                {"role": "assistant", "content": text or "(no output)"},
                {"role": "user", "content": "That did not validate against the schema. "
                                            "Emit a corrected, complete object."},
            ]
    raise RuntimeError(f"model failed to produce a valid {output.__name__}")
