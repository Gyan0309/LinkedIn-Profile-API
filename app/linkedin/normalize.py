"""Rebuild a tree from Voyager's deduplicated object graph.

The `normalized+json` Accept header returns `data` holding URN references and
`included` holding each entity once. Efficient on the wire, unusable for
extraction.

Two properties matter and are tested: the graph is cyclic (a position points at
a company that points back at its positions), and references go missing when the
session is not entitled to see them.
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 24

# A wide graph reachable by many paths blows up combinatorially even with cycle
# detection, so expansion is also capped by total nodes.
MAX_NODES = 200_000


def build_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map every entity in `included` by its URN."""
    index: dict[str, dict[str, Any]] = {}
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        urn = entity.get("entityUrn")
        if isinstance(urn, str) and urn:
            index[urn] = entity
    return index


def resolve(payload: dict[str, Any]) -> dict[str, Any]:
    """Inflate a full payload, returning the resolved `data` object."""
    index = build_index(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    resolved = _Resolver(index).walk(data, depth=0, path=frozenset())
    return resolved if isinstance(resolved, dict) else {}


def resolve_entity(payload: dict[str, Any], urn: str) -> dict[str, Any] | None:
    """Inflate one entity from `included` by URN."""
    index = build_index(payload)
    entity = index.get(urn)
    if entity is None:
        return None
    result = _Resolver(index).walk(entity, depth=0, path=frozenset({urn}))
    return result if isinstance(result, dict) else None


def entities_of_type(payload: dict[str, Any], type_suffix: str) -> list[dict[str, Any]]:
    """Every entity whose `$type` ends with `type_suffix`.

    Matching the suffix rather than the full dotted path survives LinkedIn
    renaming a package.
    """
    out: list[dict[str, Any]] = []
    index = build_index(payload)
    resolver = _Resolver(index)
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        entity_type = entity.get("$type")
        if isinstance(entity_type, str) and entity_type.endswith(type_suffix):
            urn = entity.get("entityUrn")
            seed = frozenset({urn}) if isinstance(urn, str) else frozenset()
            resolved = resolver.walk(entity, depth=0, path=seed)
            if isinstance(resolved, dict):
                out.append(resolved)
    return out


class _Resolver:
    """Recursive inflater. `path` holds the URNs open on this branch."""

    def __init__(self, index: dict[str, dict[str, Any]]) -> None:
        self._index = index
        self._budget = MAX_NODES

    def walk(self, node: Any, *, depth: int, path: frozenset[str]) -> Any:
        if depth > MAX_DEPTH or self._budget <= 0:
            return node

        if isinstance(node, dict):
            self._budget -= 1
            return self._walk_dict(node, depth=depth, path=path)

        if isinstance(node, list):
            return [self.walk(item, depth=depth + 1, path=path) for item in node]

        return node

    def _walk_dict(
        self, node: dict[str, Any], *, depth: int, path: frozenset[str]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("*"):
                # A reference. Drop the marker so extractors read natural names.
                out[key[1:]] = self._expand(value, depth=depth + 1, path=path)
            else:
                out[key] = self.walk(value, depth=depth + 1, path=path)
        return out

    def _expand(self, value: Any, *, depth: int, path: frozenset[str]) -> Any:
        if isinstance(value, str):
            return self._expand_urn(value, depth=depth, path=path)
        if isinstance(value, list):
            return [self._expand(item, depth=depth + 1, path=path) for item in value]
        return self.walk(value, depth=depth, path=path)

    def _expand_urn(self, urn: str, *, depth: int, path: frozenset[str]) -> Any:
        if urn in path:
            return {"entityUrn": urn, "$ref_cycle": True}

        entity = self._index.get(urn)
        if entity is None:
            # Not an error: LinkedIn omits entities the session cannot see.
            return {"entityUrn": urn, "$unresolved": True}

        return self.walk(entity, depth=depth, path=path | {urn})
