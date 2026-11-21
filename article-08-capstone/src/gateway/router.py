"""Model catalog + routing policy.

/extract -> Sonnet (needs reasoning), /classify -> Haiku (pattern matching),
/validate -> Sonnet (semantic checks), /summarize -> Haiku,
/score -> Haiku or Sonnet depending on request complexity.
"""

from ..config import AnthropicConfig
from ..contracts import TaskType


class ModelCatalog:
    """Approved models. Teams cannot use anything not in the catalog."""

    def __init__(self, cfg: AnthropicConfig, pricing: dict):
        self.cheap = cfg.classification_model
        self.strong = cfg.extraction_model
        self._approved = set(pricing)

    def is_approved(self, model: str) -> bool:
        return model in self._approved

    def approved(self) -> list[str]:
        return sorted(self._approved)


class ModelRouter:
    def __init__(self, catalog: ModelCatalog):
        self.catalog = catalog

    def select(self, task: TaskType, complexity: str = "low") -> str:
        if task in (TaskType.EXTRACT, TaskType.VALIDATE):
            return self.catalog.strong
        if task == TaskType.SCORE:
            return self.catalog.strong if complexity == "high" else self.catalog.cheap
        return self.catalog.cheap   # classify, summarize

    def fallback(self) -> str:
        """Fallback target when the primary model keeps failing: the cheaper, simpler model."""
        return self.catalog.cheap
