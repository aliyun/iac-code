"""Model output defaults shared by request construction and settings display."""

from __future__ import annotations

import re


class OutputTokenPolicy:
    """Resolve request-local output limits without overriding GLM server defaults."""

    DEFAULT_LIMIT = 8192
    _GLM_MODEL_PATTERN = re.compile(r"glm-[0-9]+v?(?:[.-][a-z0-9]+)*")
    _GLM_MODEL_ALIASES = frozenset({"glm-latest", "glm-flash-latest"})

    def __init__(self, model: str) -> None:
        # OpenRouter appends routing variants such as :free to the model name.
        self._model_name = model.rsplit("/", 1)[-1].partition(":")[0].strip().lower()

    @property
    def default_limit(self) -> int | None:
        """Return the local default, or None when the model uses its server default."""
        if self._GLM_MODEL_PATTERN.fullmatch(self._model_name) or self._model_name in self._GLM_MODEL_ALIASES:
            return None
        return self.DEFAULT_LIMIT

    def resolve(self, requested_limit: int, configured_limit: int | None = None) -> int | None:
        """Keep explicit configuration and helper limits; omit GLM's shared default."""
        if configured_limit is not None:
            return configured_limit
        if requested_limit == self.DEFAULT_LIMIT:
            return self.default_limit
        return requested_limit
