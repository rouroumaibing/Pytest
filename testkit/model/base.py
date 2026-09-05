"""Data-model layer: builder pattern and API-response mapping.

Two complementary building blocks are provided:

* :class:`Builder` — chainable construction of request bodies via ``with_*``
  methods returning ``self``; ``build()`` emits a plain dict.
* :class:`BaseModel` — object construction from API response dicts via
  :meth:`BaseModel.from_api_response`, with optional convenience aliases for
  nested fields (e.g. ``id = metadata.uid``).

Models never hold a client reference (client/model separation), and
``from_api_response`` never swallows exceptions — a type error is raised
normally rather than silently reset to a default.
"""

from __future__ import annotations

from typing import Any, ClassVar


class Builder:
    """Base class for chainable request-body builders.

    Subclasses define semantic ``with_xxx`` methods that delegate to
    :meth:`_with`. Each returns ``self`` so calls can be chained, and
    :meth:`build` returns the accumulated ``dict``.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _with(self, key: str, value: Any) -> Builder:
        self._data[key] = value
        return self

    def build(self) -> dict[str, Any]:
        """Return the constructed request body as a plain dict."""
        return dict(self._data)


class BaseModel:
    """Base class for models constructed from API responses.

    Subclasses declare:

    ``_fields``
        Attribute names copied directly from the response dict when present.
    ``_aliases``
        Mapping ``attribute_name -> dotted_path`` for convenience properties
        derived from nested structures, e.g. ``{"id": "metadata.uid"}``.
    """

    _fields: ClassVar[list[str]] = []
    _aliases: ClassVar[dict[str, str]] = {}

    @classmethod
    def from_api_response(cls, response: dict[str, Any]) -> BaseModel:
        """Build a model instance from an API response dict.

        Type errors and other exceptions are propagated normally — they are
        never silently swallowed or reset to defaults.
        """
        obj = cls()
        for field in cls._fields:
            if field in response:
                setattr(obj, field, response[field])
        for attr, path in cls._aliases.items():
            setattr(obj, attr, cls._resolve(response, path))
        return obj

    @staticmethod
    def _resolve(data: Any, path: str) -> Any | None:
        current = data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def to_dict(self) -> dict[str, Any]:
        """Return declared fields (plus aliases) as a dict snapshot."""
        result: dict[str, Any] = {}
        for field in self._fields:
            if hasattr(self, field):
                result[field] = getattr(self, field)
        for attr in self._aliases:
            if hasattr(self, attr):
                result[attr] = getattr(self, attr)
        return result
