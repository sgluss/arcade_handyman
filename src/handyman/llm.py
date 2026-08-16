"""Shared LLM plumbing for the pipeline's judgment stages.

Every LLM boundary in this project goes through `parse`: the model must emit a
schema-valid instance of a contract from `ir.py` (or fail loudly). No stage
consumes free-form model prose — that discipline is what makes the LLM stages
retryable and unit-testable like any other function.

pydantic-ai owns the provider mechanics (transport, structured output,
validation retries), so the model is pure configuration: any of its model
strings works — "bedrock:...", "anthropic:...", "openai:..." — and switching
providers never touches code.
"""

import os
from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "bedrock:us.anthropic.claude-sonnet-5"


def model_name() -> str:
    """One model for every judgment stage; override via HANDYMAN_MODEL.

    Read at call time, not import time, so it honors `.env` no matter when
    the environment gets loaded.
    """
    return os.environ.get("HANDYMAN_MODEL", DEFAULT_MODEL)


def parse(prompt: str, output: type[T], *, system: str, max_tokens: int = 16000) -> T:
    """Ask the model for a schema-validated `output` instance.

    pydantic-ai retries a malformed response once, with the validation error
    shown to the model — in practice structured outputs make this path rare.
    """
    agent = Agent(
        model_name(),
        output_type=output,
        system_prompt=system,
        model_settings=ModelSettings(max_tokens=max_tokens),
    )
    return agent.run_sync(prompt).output
