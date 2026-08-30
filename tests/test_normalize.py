"""The URN graph resolver.

The two properties worth guaranteeing are the ones that bite in production: the
graph is cyclic, and references go missing. Both are normal, and neither may
crash or hang.
"""

from __future__ import annotations

from app.linkedin import normalize


def test_builds_index_from_included(fixture) -> None:
    payload = fixture("normalized_cyclic.json")
    index = normalize.build_index(payload)
    assert set(index) == {
        "urn:li:fsd_profile:CYCLE1",
        "urn:li:fsd_company:CYCLE2",
    }


def test_resolves_star_prefixed_references(fixture) -> None:
    """A `*key` reference is replaced by the entity, and the marker is dropped."""
    resolved = normalize.resolve(fixture("normalized_cyclic.json"))

    assert "*profile" not in resolved
    assert resolved["profile"]["firstName"] == "Loop"
    assert resolved["profile"]["currentCompany"]["name"] == "Ouroboros AB"


def test_cycle_becomes_a_stub_rather_than_recursing(fixture) -> None:
    """Profile -> company -> employees -> the same profile must terminate."""
    resolved = normalize.resolve(fixture("normalized_cyclic.json"))

    employees = resolved["profile"]["currentCompany"]["employees"]
    assert employees == [
        {"entityUrn": "urn:li:fsd_profile:CYCLE1", "$ref_cycle": True}
    ]


def test_missing_reference_is_marked_not_dropped(fixture) -> None:
    """An entity the session cannot see is reported, not silently omitted.

    Dropping it would make "not permitted to see this" indistinguishable from
    "this does not exist", which is the distinction the whole API is built on.
    """
    resolved = normalize.resolve(fixture("normalized_cyclic.json"))

    assert resolved["profile"]["missingReference"] == {
        "entityUrn": "urn:li:fsd_company:NOT_IN_INCLUDED",
        "$unresolved": True,
    }


def test_list_references_resolve_elementwise() -> None:
    payload = {
        "data": {"*elements": ["urn:li:a", "urn:li:b"]},
        "included": [
            {"entityUrn": "urn:li:a", "name": "first"},
            {"entityUrn": "urn:li:b", "name": "second"},
        ],
    }
    resolved = normalize.resolve(payload)
    assert [item["name"] for item in resolved["elements"]] == ["first", "second"]


def test_depth_is_capped_on_a_deep_chain() -> None:
    """A chain longer than MAX_DEPTH stops expanding instead of blowing the stack."""
    depth = normalize.MAX_DEPTH + 20
    included = [
        {"entityUrn": f"urn:li:n{i}", "*next": f"urn:li:n{i + 1}"}
        for i in range(depth)
    ]
    included.append({"entityUrn": f"urn:li:n{depth}", "leaf": True})
    payload = {"data": {"*next": "urn:li:n0"}, "included": included}

    resolved = normalize.resolve(payload)

    # Terminates and returns something; the tail is left unexpanded.
    node = resolved
    hops = 0
    while isinstance(node, dict) and isinstance(node.get("next"), dict):
        node = node["next"]
        hops += 1
    assert hops < depth


def test_resolve_tolerates_a_payload_with_no_data() -> None:
    assert normalize.resolve({"included": []}) == {}
    assert normalize.resolve({}) == {}


def test_entities_of_type_matches_on_suffix(fixture) -> None:
    """Suffix matching survives LinkedIn renaming a package path."""
    payload = fixture("graphql_profile.json")

    found = normalize.entities_of_type(payload, "identity.profile.Profile")

    assert len(found) == 1
    assert found[0]["publicIdentifier"] == "ada-sundqvist-synthetic"


def test_entities_of_type_returns_empty_when_absent(fixture) -> None:
    payload = fixture("graphql_profile.json")
    assert normalize.entities_of_type(payload, "organization.Company") == []


def test_resolve_entity_by_urn(fixture) -> None:
    payload = fixture("normalized_cyclic.json")

    entity = normalize.resolve_entity(payload, "urn:li:fsd_company:CYCLE2")

    assert entity is not None
    assert entity["name"] == "Ouroboros AB"
    assert normalize.resolve_entity(payload, "urn:li:fsd_company:NOPE") is None


def test_non_dict_and_scalar_values_pass_through() -> None:
    payload = {
        "data": {"count": 3, "flag": True, "nothing": None, "items": [1, "two"]},
        "included": [],
    }
    resolved = normalize.resolve(payload)
    assert resolved == {"count": 3, "flag": True, "nothing": None, "items": [1, "two"]}
