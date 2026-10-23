"""The support agents (`classifier` and `drafter`), each in its own subpackage."""

from .classifier import INSTRUCTIONS as CLASSIFIER_INSTRUCTIONS
from .classifier import PROMPT_FILE as CLASSIFIER_PROMPT
from .classifier import classifier
from .drafter import INSTRUCTIONS as DRAFTER_INSTRUCTIONS
from .drafter import PROMPT_FILE as DRAFTER_PROMPT
from .drafter import drafter, embed, search_kb

__all__ = [
    "CLASSIFIER_INSTRUCTIONS",
    "CLASSIFIER_PROMPT",
    "DRAFTER_INSTRUCTIONS",
    "DRAFTER_PROMPT",
    "classifier",
    "drafter",
    "embed",
    "search_kb",
]
