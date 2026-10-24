"""
House writing style for everything the model writes for a learner: no em dashes.

Two layers, because a prompt rule alone is ignored some of the time:
  WRITING_RULES  goes into every generation prompt (via safety.content_rules)
  clean(...)     removes any long dash that slipped through; safety.ensure_safe applies it to every result
                 before it is checked, stored or shown

The long dash characters are built with chr() so this file, like the rest of the project, contains none.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

WRITING_RULES = (
    "Writing style: never use the em dash (the long dash) anywhere, including inside labels and captions. "
    "Use a comma, a colon, parentheses, a hyphen or a new sentence instead.\n"
)

EM_DASH = chr(0x2014)
HORIZONTAL_BAR = chr(0x2015)  # the other long dash

# A dash with spaces around it becomes a spaced hyphen, a dash glued to the words becomes a plain hyphen.
# Spaces and tabs only, so line breaks survive.
LONG_DASH = re.compile("[ \t]*[" + EM_DASH + HORIZONTAL_BAR + "][ \t]*")


def _swap(match: re.Match) -> str:
    text = match.group(0)
    return " - " if text != text.strip() else "-"


def plain(text: str) -> str:
    return LONG_DASH.sub(_swap, text)


def clean(value: Any) -> Any:
    """`value` with every long dash removed from all the text inside it (models, lists, dicts, tuples).
    Returns a copy; the original is untouched. Non-text values pass through."""
    if isinstance(value, str):
        return plain(value)
    if isinstance(value, BaseModel):
        return value.model_copy(
            update={name: clean(getattr(value, name)) for name in type(value).model_fields}
        )
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return tuple(clean(v) for v in value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value
