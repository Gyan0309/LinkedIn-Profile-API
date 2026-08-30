"""Resolve Voyager's normalized response format into plain nested dictionaries.

Asking for `application/vnd.linkedin.normalized+json+2.1` gets a deduplicated
object graph rather than a tree:

    {"data":     {"*elements": ["urn:li:fsd_profile:ACoAA..."], ...},
     "included": [{"entityUrn": "urn:li:fsd_profile:ACoAA...", "$type": "...", ...}]}

Keys prefixed with `*` hold URN references into `included`; every entity appears
exactly once no matter how many places point at it. That is efficient on the wire
and unusable for extraction, so this module rebuilds the tree.

Two properties matter and are tested directly. The graph is genuinely cyclic — a
position references a company which references its positions — so resolution
tracks the path and stubs a repeat rather than recursing forever. And a reference
that is missing from `included` is a normal occurrence, not a bug: LinkedIn omits
entities the session is not entitled to see. Those become explicit unresolved
stubs, so an extractor can tell "not permitted" from "not present".
"""

from __future__ import annotations

from typing import Any

# Depth is bounded well below Python's recursion limit. Real profile payloads
# resolve fully within ~12 levels; the cap exists for adversarial or malformed input.
MAX_DEPTH = 24

# Total nodes expanded per document. A wide graph reachable by many paths can
# otherwise blow up combinatorially even with cycle detection in place.
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
    """Resolve a full Voyager payload, returning the inflated `data` object."""
    index = build_index(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    resolver = _Resolver(index)
    resolved = resolver.walk(data, depth=0, path=frozenset())
    return resolved if isinstance(resolved, dict) else {}


def resolve_entity(payload: dict[str, Any], urn: str) -> dict[str, Any] | None:
    """Resolve one entity from `included` by URN, inflating its references."""
    index = build_index(payload)
    entity = index.get(urn)
    if entity is None:
        return None
    resolver = _Resolver(index)
    result = resolver.walk(entity, depth=0, path=frozenset({urn}))
    return result if isinstance(result, dict) else None


def entities_of_type(payload: dict[str, Any], type_suffix: str) -> list[dict[str, Any]]:
    """Every entity in `included` whose `$type` ends with `type_suffix`.

    A blunt escape hatch for payloads whose `data` root does not reference the
    entities we want, which happens on some card responses. Matching on the
    suffix rather than the full dotted path survives LinkedIn renaming a package.
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
    """Recursive inflater over one payload's URN index.

    `path` is the set of URNs currently being expanded on this branch, so a cycle
    is detected without penalising a URN that legitimately appears in two
    unrelated branches.
    """

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
        # Already inflated by LinkedIn on some endpoints — pass through unchanged.
        return self.walk(value, depth=depth, path=path)

    def _expand_urn(self, urn: str, *, depth: int, path: frozenset[str]) -> Any:
        if urn in path:
            # Cycle. Return the identity rather than recursing; a caller that
            # actually needs this entity can look it up from the index directly.
            return {"entityUrn": urn, "$ref_cycle": True}

        entity = self._index.get(urn)
        if entity is None:
            # Not an error. LinkedIn omits entities the session cannot see.
            return {"entityUrn": urn, "$unresolved": True}

        return self.walk(entity, depth=depth, path=path | {urn})
