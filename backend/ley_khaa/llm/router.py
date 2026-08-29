from dataclasses import dataclass
from enum import Enum

# Exact model ids. Never append a date suffix.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"

# Haiku 4.5 predates adaptive thinking: sending `thinking` or `output_config.effort`
# to it is a 400. Only the 5-series models below accept adaptive thinking.
_THINKING_MODELS = {SONNET, OPUS}


class Stage(str, Enum):
    RELEVANCE_FILTER = "relevance_filter"
    CRYSTALLIZER = "crystallizer"
    INTERPRETER = "interpreter"
    VISION_EXTRACTION = "vision_extraction"
    SYNTHESIS = "synthesis"
    REGISTRY_MATCH = "registry_match"
    MEMORY_MATCH = "memory_match"
    PROJECT_ROUTE = "project_route"


@dataclass(frozen=True)
class ModelChoice:
    model: str
    supports_thinking: bool
    max_tokens: int


# stage -> {complexity -> model}. "routine" is the fallback for any unknown signal.
_POLICY: dict[Stage, dict[str, str]] = {
    Stage.RELEVANCE_FILTER: {"routine": HAIKU, "hard": HAIKU},
    Stage.CRYSTALLIZER: {"routine": HAIKU, "hard": OPUS},
    Stage.INTERPRETER: {"routine": OPUS, "hard": OPUS},
    Stage.VISION_EXTRACTION: {"routine": OPUS, "hard": OPUS},
    # Writing correct code from an under-specified request is the hardest thing
    # the system does, and a wrong script costs a sandbox round trip plus a
    # repair. Opus at both complexities.
    Stage.SYNTHESIS: {"routine": OPUS, "hard": OPUS},
    # Both exist to avoid an Opus call. Routing them to Opus would make
    # consulting the cache cost more than the work it saves.
    Stage.REGISTRY_MATCH: {"routine": HAIKU, "hard": HAIKU},
    Stage.MEMORY_MATCH: {"routine": HAIKU, "hard": HAIKU},
    # Exists to decide where work goes, not to do the work. Haiku at both
    # complexities, like the other two matchers.
    Stage.PROJECT_ROUTE: {"routine": HAIKU, "hard": HAIKU},
}

_MAX_TOKENS: dict[Stage, int] = {
    Stage.RELEVANCE_FILTER: 512,
    Stage.CRYSTALLIZER: 8000,
    Stage.INTERPRETER: 8000,
    Stage.VISION_EXTRACTION: 8000,
    # Emits a whole program, not a small structured object.
    Stage.SYNTHESIS: 16000,
    # A name, a float and one sentence.
    Stage.REGISTRY_MATCH: 1024,
    Stage.MEMORY_MATCH: 1024,
    # A name, a float and one sentence.
    Stage.PROJECT_ROUTE: 1024,
}


def model_for(stage: Stage, complexity: str = "routine") -> ModelChoice:
    by_complexity = _POLICY[stage]
    model = by_complexity.get(complexity, by_complexity["routine"])
    return ModelChoice(
        model=model,
        supports_thinking=model in _THINKING_MODELS,
        max_tokens=_MAX_TOKENS[stage],
    )
