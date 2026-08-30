# Test fixtures

Voyager payloads in the shapes this service parses, used by the offline test
suite. **Everything here is synthetic.** No real member's data is committed, and
no cookies, tokens or identifiers from a live session appear in any file.

The shapes are modelled on what the endpoints return, so the parsing logic is
exercised against realistic nesting -- the `included[]` URN graph, the rendered
component trees, the `vectorImage` root/artifact split -- rather than against
flattened dictionaries that would make the extractors look simpler than they are.

| File | Shape |
|---|---|
| `profileview_dense.json` | Legacy `profileView` REST, every section populated |
| `profileview_sparse.json` | Legacy `profileView`, name and headline only |
| `graphql_profile.json` | Vanity-name lookup, `included[]` carrying the Profile entity |
| `graphql_experience_card.json` | Profile card with a grouped multi-role company |
| `graphql_skills_card.json` | Profile card with endorsement insight lines |
| `normalized_cyclic.json` | Minimal payload whose URN graph contains a cycle |

`scripts/capture_fixture.py` regenerates these from a live session. It redacts as
it captures: identifiers are replaced with synthetic ones and the session is
never written to disk. Anything it produces still needs reading before it is
committed.
