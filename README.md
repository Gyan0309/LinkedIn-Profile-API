# LinkedIn Profile API

Takes a LinkedIn profile URL, returns the profile as structured JSON.

It works by talking to **Voyager**, the private API `linkedin.com` itself calls —
raw authenticated HTTP, reverse-engineered from the web client's own traffic.
There is no browser anywhere in the stack: no Playwright, no Selenium, no
headless Chrome, and no HTML parsing of profile pages.

```bash
curl "https://YOUR-SERVICE.onrender.com/v1/profile?url=https://www.linkedin.com/in/williamhgates"
```

> **Before anything else.** Automated access to LinkedIn violates its User
> Agreement §8.2, and the account whose session backs this service can be
> restricted or permanently banned. Use a dedicated throwaway account, never a
> real professional identity. This is inherent to the problem as posed, and it is
> stated here rather than buried at the bottom.

---

## Contents

- [The one thing to know about the response](#the-one-thing-to-know-about-the-response)
- [API documentation](#api-documentation)
- [Setup](#setup)
- [Getting a session](#getting-a-session)
- [Approach](#approach)
- [Known limitations](#known-limitations)
- [Testing](#testing)

---

## The one thing to know about the response

**An empty list always means "this person has none". It never means "we failed to
fetch it."**

When a section cannot be retrieved, it is named in `meta.sections_unavailable`
instead of being quietly returned as `[]`:

```jsonc
{
  "meta": {
    "source": "mixed",
    "sections_unavailable": ["patents", "publications"]
  },
  "profile": {
    "skills": [],        // fetched fine. This person has no listed skills.
    "patents": [],       // NOT fetched. See sections_unavailable.
    "certifications": [ /* ... */ ]
  }
}
```

Without this, a consumer cannot tell a person with no work history from a fetch
that fell over, and will silently treat the second as the first. Everything else
in the design follows from wanting that distinction to hold.

---

## API documentation

Interactive docs are served at `/docs`, generated from the same pydantic models
that validate the responses.

### `GET /v1/profile`

| Parameter | In | Required | Description |
|---|---|---|---|
| `url` | query | yes | A LinkedIn profile URL, or a bare public identifier |
| `refresh` | query | no | `true` bypasses the cache and refetches |
| `X-API-Key` | header | see below | Unlocks arbitrary profiles |

Accepted `url` forms — all resolve to the same identifier:

```
https://www.linkedin.com/in/ada-sundqvist
https://www.linkedin.com/in/ada-sundqvist/
https://se.linkedin.com/in/ada-sundqvist          # any locale subdomain
https://www.linkedin.com/in/ada-sundqvist?trk=abc # tracking params stripped
https://www.linkedin.com/in/ada-sundqvist/detail/experience/
https://www.linkedin.com/pub/ada-sundqvist/1/b2/3c4   # legacy /pub/ form
https://www.linkedin.com/in/andr%C3%A9-m%C3%BCller    # percent-encoded unicode
ada-sundqvist                                          # bare identifier
```

Company, school, job and post URLs are rejected with a message naming what was
actually pasted.

<details>
<summary><strong>Example response</strong> (abridged)</summary>

```json
{
  "meta": {
    "profile_url": "https://www.linkedin.com/in/ada-sundqvist",
    "public_identifier": "ada-sundqvist",
    "profile_urn": "urn:li:fsd_profile:ACoAA...",
    "fetched_at": "2026-08-29T11:04:22.184Z",
    "duration_ms": 2140,
    "cache": "miss",
    "source": "voyager-graphql",
    "sections_unavailable": ["patents"]
  },
  "profile": {
    "public_identifier": "ada-sundqvist",
    "name": { "first": "Ada", "last": "Sundqvist", "full": "Ada Sundqvist" },
    "headline": "Platform Engineer at Kestrel Systems",
    "about": "Distributed systems, mostly storage.",
    "pronouns": "SHE_HER",
    "location": { "raw": "Stockholm, Sweden", "country": "Sweden", "country_code": "se" },
    "industry": "Software Development",
    "profile_picture": {
      "sizes": [
        { "width": 100, "height": 100, "url": "https://media.licdn.com/..." },
        { "width": 800, "height": 800, "url": "https://media.licdn.com/..." }
      ]
    },
    "background_image": { "sizes": [ /* ... */ ] },
    "connections": { "count": 842, "is_capped": true },
    "followers": 1503,
    "open_to_work": false,
    "hiring": false,
    "premium": true,
    "influencer": false,
    "experience": [
      {
        "title": "Staff Platform Engineer",
        "company": "Kestrel Systems",
        "company_url": "https://www.linkedin.com/company/kestrel-systems/",
        "company_urn": "urn:li:fsd_company:1000001",
        "company_logo": { "sizes": [ /* ... */ ] },
        "employment_type": "Full-time",
        "location": "Stockholm, Sweden",
        "description": "Storage layer for the ingest pipeline.",
        "start": { "year": 2020, "month": 8 },
        "end": null,
        "is_current": true,
        "duration_months": 49,
        "sub_positions": [
          { "title": "Staff Platform Engineer", "start": { "year": 2023, "month": 3 }, "is_current": true },
          { "title": "Platform Engineer", "start": { "year": 2020, "month": 8 }, "end": { "year": 2023, "month": 3 } }
        ]
      }
    ],
    "education": [
      {
        "school": "KTH Royal Institute of Technology",
        "degree": "MSc",
        "field_of_study": "Computer Science",
        "grade": "4.6",
        "start": { "year": 2014 },
        "end": { "year": 2019 }
      }
    ],
    "skills": [{ "name": "Distributed Systems", "endorsement_count": 31 }],
    "certifications": [
      {
        "name": "Certified Kubernetes Administrator",
        "issuer": "The Linux Foundation",
        "credential_id": "CKA-...",
        "credential_url": "https://...",
        "issue_date": { "year": 2021, "month": 5 },
        "expiry_date": { "year": 2024, "month": 5 }
      }
    ],
    "languages": [{ "name": "Swedish", "proficiency": "NATIVE_OR_BILINGUAL" }],
    "projects": [], "publications": [], "honors": [],
    "volunteering": [], "courses": [], "patents": [], "organizations": []
  }
}
```

</details>

**`meta.source`** reports which endpoint actually served the data —
`voyager-graphql`, `voyager-rest-profileview`, `voyager-rest-dash`, or `mixed`
when sections came from more than one. Fidelity differs between them (see
[Approach](#approach)), so this is not merely diagnostic.

**Multi-role stints.** Several roles at one employer stay grouped: the outer
entry carries the company and the full tenure, `sub_positions` carries the roles.
Flattening them would lose the fact that it was one continuous tenure with
promotions.

### Other endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. Deliberately does **not** touch LinkedIn |
| `GET /v1/demo` | none | Which profiles are callable without a key |
| `GET /v1/session` | key | Session fingerprint, queryId state, breaker status |
| `GET /docs` | none | Interactive OpenAPI documentation |

`/v1/session` never returns the cookie — only a `AAAA...AAAA` style fingerprint,
enough to tell two sessions apart and useless to a thief.

### Authentication

Two tiers, because a public endpoint in front of one LinkedIn session is an open
proxy for anyone who finds it:

- **No key** — the profiles in `DEMO_PROFILES` only, at `DEMO_RATE_LIMIT_PER_HOUR`.
  Enough to evaluate the API end to end.
- **`X-API-Key`** — any profile URL, at `KEYED_RATE_LIMIT_PER_HOUR`.

### Errors

Every failure carries a stable `error` code, so callers branch on that rather
than parsing prose.

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_profile_url` | Not a parseable profile URL |
| 400 | `invalid_request` | Missing or malformed parameters |
| 401 | `invalid_api_key` | Key presented but not recognised |
| 403 | `demo_scope_exceeded` | Profile outside the demo allowlist, no key given |
| 404 | `profile_not_found` | No such profile |
| 429 | `rate_limited` | Caller's own quota. Carries `Retry-After` |
| 503 | `linkedin_blocked` | HTTP 999 or 403 upstream. Carries `Retry-After` |
| 503 | `linkedin_session_unavailable` | Cookie missing or expired |
| 503 | `linkedin_challenge_required` | Login challenged; a human must intervene |
| 503 | `linkedin_rate_limited` | LinkedIn throttled us past our retry budget |
| 503 | `query_rejected` | GraphQL queryId rejected even after rediscovery |
| 503 | `upstream_unavailable` | Every fetch strategy failed |

```json
{
  "error": "linkedin_blocked",
  "message": "LinkedIn returned HTTP 999: this host is flagged as automated traffic...",
  "retry_after_seconds": 287
}
```

A failure is **never** returned as a 200 with an empty profile.

---

## Setup

### Local

```bash
git clone https://github.com/Gyan0309/linkedin-profile-api.git
cd linkedin-profile-api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in LINKEDIN_LI_AT — see below
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs>.

### Docker

```bash
docker build -t linkedin-profile-api .
docker run -p 8000:8000 --env-file .env linkedin-profile-api
```

### Render

`render.yaml` is a blueprint — point Render at the repo and it builds from the
Dockerfile. Secrets are declared by name with `sync: false`, so Render prompts
for them in the dashboard and never reads a value from the repo. Set
`LINKEDIN_LI_AT` and `API_KEYS` there after the first deploy.

### Configuration

Names live in `.env.example`; values never enter the repository.

| Variable | Default | Purpose |
|---|---|---|
| `LINKEDIN_LI_AT` | — | Session cookie. **The one that matters** |
| `LINKEDIN_JSESSIONID` | auto | Optional; synthesised when absent |
| `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` | — | Programmatic login instead of a cookie |
| `API_KEYS` | empty | Comma-separated. Empty = demo-only mode |
| `DEMO_PROFILES` | 3 public figures | Callable without a key |
| `OUTBOUND_PROXY_URL` | — | Residential proxy, for when the host IP is flagged |
| `LINKEDIN_QUERY_IDS_JSON` | — | Pin queryIds instead of discovering them |
| `CACHE_TTL_SECONDS` | 21600 | Response cache lifetime |
| `OUTBOUND_MAX_PER_MINUTE` | 30 | **Protects the account.** Keep it low |

---

## Getting a session

`LINKEDIN_LI_AT` is the whole authentication story. Get it from a browser:

1. Log into LinkedIn **from a normal residential connection** with the throwaway account.
2. DevTools → Application → Cookies → `https://www.linkedin.com`.
3. Copy the value of `li_at`.
4. Set it as `LINKEDIN_LI_AT` locally, and as a Render secret in production.

**Log in from home, not from the server.** A fresh login originating from a
datacenter IP is the single most reliable way to trigger a verification
challenge. Creating the session on a residential connection and only *using* it
from the server avoids that entirely. This is why the cookie path is documented
as primary and the credentials path is a convenience.

`JSESSIONID` is optional. LinkedIn's CSRF check compares the `csrf-token` header
against the JSESSIONID cookie *we send* — it does not require a server-issued
value — so a self-consistent pair is minted when only `li_at` is supplied.

The credentials path (`LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD`) posts to
`/uas/authenticate` and reads `li_at` off the response. On a challenge it raises
`linkedin_challenge_required` and stops. **This service does not solve CAPTCHAs
or work around verification** — a human logs in from a browser instead.

---

## Approach

### Finding the API

The LinkedIn web app is a single-page application. Every profile section it
renders arrives as JSON from `https://www.linkedin.com/voyager/api/*` — the same
backend the mobile clients use. Watching the network tab while loading a profile
gives the endpoints, headers, and query grammar directly.

Four headers make an authenticated Voyager request work, and three of them are
easy to get subtly wrong:

| Header | Value | Why |
|---|---|---|
| `Cookie` | `li_at=...; JSESSIONID="ajax:..."` | The session. JSESSIONID keeps its literal quotes |
| `csrf-token` | `ajax:...` | Same value, **quotes stripped**. A mismatch is a silent 403 |
| `x-restli-protocol-version` | `2.0.0` | Without it, several endpoints 400 |
| `Accept` | `application/vnd.linkedin.normalized+json+2.1` | Load-bearing — see below |

Voyager also uses rest.li query grammar, with literal parentheses and colons:
`variables=(vanityName:some-person)`. Percent-encode them and the request 400s,
so the client passes `(),:*~!` through unencoded.

### The normalized response format

That `Accept` header changes the response from a tree into a deduplicated object
graph:

```json
{ "data":     { "*elements": ["urn:li:fsd_profile:ACoAA..."] },
  "included": [ { "entityUrn": "urn:li:fsd_profile:ACoAA...", "$type": "...", "firstName": "..." } ] }
```

Keys prefixed `*` hold URN references into `included`, where each entity appears
exactly once regardless of how many places point at it. Efficient on the wire,
unusable for extraction — so `app/linkedin/normalize.py` rebuilds the tree.

Two properties there are not optional:

- **The graph is cyclic.** A position references a company, which references its
  positions. Resolution tracks the URNs on the current branch and substitutes a
  `$ref_cycle` stub rather than recursing forever.
- **References go missing.** LinkedIn omits entities the session is not entitled
  to see. Those become explicit `$unresolved` stubs, because dropping them would
  make "not permitted" indistinguishable from "not present".

### queryIds, and why they are discovered at runtime

Voyager's GraphQL endpoint does not accept arbitrary queries — only pre-registered
ones named by a `queryId` like
`voyagerIdentityDashProfileComponents.a1b2c3...` (32 hex characters). The hash
changes whenever LinkedIn ships the corresponding front-end module.

**Hardcoding one is a time bomb.** It works the day it is written and starts
returning 400s a few weeks later, with nothing in the logs saying why. So the ids
are read from the same place the web app gets them:

1. Fetch a LinkedIn page and regex out the `static.licdn.com/**.js` bundle URLs.
2. Fetch those bundles, relevance-ordered so profile bundles are scanned first.
3. Regex out `voyager*.{32 hex}` registrations.
4. Cache for 24h. A 400 naming a stale id invalidates the cache, rediscovers, and
   retries **exactly once** — bounded, because if the fresh id also fails then the
   query shape is the problem and looping just burns an account's request budget.

`LINKEDIN_QUERY_IDS_JSON` pins them when discovery breaks.

> This step reads HTML only to list `<script src>` URLs, then fetches `.js`
> files. No profile data is ever parsed out of HTML. It is `httpx.get` on a
> JavaScript file — not browser automation, and not page scraping.

### The strategy chain

LinkedIn exposes the same profile through several generations of endpoint, none
reliably available. So rather than picking one and hoping:

| | Strategy | Shape | Fidelity |
|---|---|---|---|
| **S1** | GraphQL profile cards | Rendered component trees | Lower — dates parsed from captions |
| **S2** | `identity/profiles/{id}/profileView` | Typed entities | **Higher** — real numeric dates |
| **S3** | `identity/dash/profiles` | Top card only | Name/headline/photo |

The fidelity column is the interesting part. S1 is what linkedin.com calls today,
but it returns *what the UI paints*: a position is not
`{title, companyName, startDate}`, it is an `entityComponent` with a title, a
subtitle packing several fields around a middle dot, and a caption reading
`"Jan 2020 · Present · 3 yrs 2 mos"`. Structured dates have to be recovered from
that string. `app/extract/common.py` does it conservatively — where it is not
confident it returns `null` rather than guessing, because a wrong date is worse
than a missing one.

S2 carries real typed fields and needs no such parsing. It is also being retired
unevenly — alive for some accounts and regions, gone for others — which is
precisely why it is a fallback rather than the primary.

**The chain merges per section, not all-or-nothing.** If S1 returns experience
and education but drops certifications, S2 backfills *only* the certifications.
The caller gets the fullest profile available rather than the intersection of
what one endpoint happened to serve, and `meta.source` becomes `mixed`.

Anything no strategy could fetch lands in `sections_unavailable`.

### Not retrying a block

The retry policy is deliberately asymmetric:

- **429** → exponential backoff with **full jitter**, up to 3 attempts. Fixed
  backoff would make concurrent section fetches retry in lockstep and reproduce
  the burst that caused the throttle.
- **999 / 403** → **never retried.** It trips a circuit breaker that stops all
  outbound traffic for 5 minutes, and every caller in that window gets a clean
  503. Retrying a block is how a throttled account becomes a banned one.
- **401** → the session is re-acquired **once**, not in a loop.

The outbound token-bucket limiter (`OUTBOUND_MAX_PER_MINUTE`) matters more than
the inbound one. Inbound limits protect the service from callers; the outbound
limit protects the LinkedIn account from the service, and the account is the part
that cannot be re-provisioned in thirty seconds. Section fetches also run at a
concurrency of 3 with per-request jitter — twelve simultaneous requests from one
session is a recognisable automation signature, and evenly-spaced requests are
themselves a tell.

### Logging: reading what actually happened

Every request gets a short id, carried in a `ContextVar` so that logs four layers
down (the HTTP client) are correlated without threading an id through every
signature. It comes back as `X-Request-ID`, so a caller reporting a problem can
quote it and you land on the right lines. Supply your own and it is echoed, so a
trace can span a proxy.

A full request reads as one story:

```
19:53:35 INFO  [904e5f] -> request       GET /v1/profile?url=https://www.linkedin.com/in/williamhgates
19:53:35 INFO  [904e5f] caller           tier=demo id=ip:127.0.0.1
19:53:35 INFO  [904e5f] resolved         identifier=williamhgates
19:53:35 INFO  [904e5f] authorised       tier=demo limit_per_hour=20 used=1
19:53:35 INFO  [904e5f] cache            MISS (not cached) - fetching upstream
19:53:35 INFO  [904e5f] chain            starting identifier=williamhgates sections=12
19:53:35 INFO  [904e5f] S1 graphql       trying GraphQL profile cards
19:53:35 INFO  [904e5f] session          acquired source=env-cookie li_at_fingerprint=AQED...0001
19:53:35 INFO  [904e5f]   voyager        graphql[vanityName=williamhgates] status=200 ms=812
19:53:35 INFO  [904e5f] S1 graphql       top card ok name=Bill Gates urn=urn:li:fsd_profile:ACoAA...
19:53:35 INFO  [904e5f] S1 graphql       fetching section cards count=12 concurrency=3
19:53:36 INFO  [904e5f]   voyager        graphql[sectionType=experience] status=200 ms=390
19:53:36 INFO  [904e5f]   section        experience result=ok items=2
19:53:36 WARNING [904e5f]  voyager        graphql[sectionType=patents] status=400 ms=88
19:53:36 INFO  [904e5f]   section        patents result=unavailable error=query_rejected
19:53:37 INFO  [904e5f] S2 profileview   trying legacy REST for missing sections missing=10
19:53:38 INFO  [904e5f] S2 profileview   backfilled sections still_missing=1
19:53:38 INFO  [904e5f] chain            done source=mixed served=11 unavailable=1 ms=2560
19:53:38 WARNING [904e5f] gaps           sections that could NOT be fetched sections=patents
19:53:38 INFO  [904e5f] served           Bill Gates source=mixed cache=hit unavailable=1 ms=2576
19:53:38 INFO  [904e5f] <- request       status=200 ms=2580
```

**Every upstream call is logged with its status and timing.** Upstream calls are
the scarce resource — if the count looks wrong, that *is* the bug. That property
paid for itself immediately: the first trace showed `queryid REJECTED —
rediscovering` firing six times in one request, because twelve concurrent
sections were each independently invalidating the cache. Bounded per call, but
not across calls. It would have doubled the request count against a rotated
queryId, and nothing else would have surfaced it. See
`MIN_SECONDS_BETWEEN_ROTATIONS` in `queryids.py`.

Lines are uniform, so `grep 'S1'`, `grep 'section'` or `grep 'voyager'` each pull
a coherent slice without a parser. `LOG_LEVEL=WARNING` reduces this to problems
only; uvicorn's own access log is silenced because it duplicates these lines
without the request id.

**Redaction.** A service holding a session cookie will eventually log one by
accident — in an exception repr, a retry warning, a header dump.
`app/logging_config.py` installs a filter that rewrites formatted records,
matching on **value shape** as well as key name, so a bare `ajax:` session id is
caught anywhere it appears, including via a deferred `%s` argument. A secret has
to survive both code review and the filter to reach a log aggregator, and the
filter has its own tests.

---

## Known limitations

Stated plainly. Most of these are properties of the problem, not bugs to be fixed
later.

**Datacenter IPs draw HTTP 999.** LinkedIn marks cloud-provider ASNs
aggressively. Authenticated Voyager calls survive far better than anonymous page
fetches — an authenticated session looks like a logged-in member, not a crawler —
but Render's shared IPs can still get flagged, and there is no fix from inside
the application. `OUTBOUND_PROXY_URL` routes through a residential proxy when it
happens. Datacenter proxies do not help; LinkedIn blocks those on sight.

**The backing account can be banned.** This violates LinkedIn ToS §8.2. A
dedicated throwaway account keeps a real professional identity out of the blast
radius, but the throwaway itself is expendable by design.

**A throwaway account sees less.** Profile visibility depends on network
distance. A new account with no connections gets a reduced view of most people —
fewer sections, truncated lists, sometimes just a name and headline. Data
completeness is a property of the *account*, not of this code.

**GraphQL date parsing is lossy.** Recovering `{year, month}` from
`"Jan 2020 · Present"` is locale-dependent and imperfect. Where S2 is available
the dates are exact; where only S1 answers, days are never available and months
are occasionally absent. `meta.source` tells you which you got.

**The GraphQL component mapping is written against observed shapes.** LinkedIn
changes component trees without notice. When a shape stops matching, the affected
section is reported in `sections_unavailable` rather than silently returning
wrong data — the failure mode is honest, but it is still a failure that needs a
code change.

**queryId discovery can fail.** If LinkedIn changes how bundles are served or the
registration format, discovery finds nothing and GraphQL is unavailable until
`LINKEDIN_QUERY_IDS_JSON` is pinned or the regex is updated. S2 and S3 still work.

**`profileView` availability is account-dependent.** It is being retired
unevenly. It may work for your account and not for a reviewer's.

**Rate limits are real and low.** LinkedIn tolerates far less than a public API
would. The defaults here are conservative; raising `OUTBOUND_MAX_PER_MINUTE`
meaningfully will get the account restricted.

**Profiles only.** No company, job, search, or connection endpoints.

**Single instance, in-process cache.** The cache does not survive a restart and is
not shared across instances. Losing it costs one refetch, which is cheaper than
requiring Redis to boot.

**Locale.** Requests set `en_US`. Multi-locale profiles return their primary
locale; a profile authored only in another language returns that language's text.

---

## Testing

The whole suite runs with **no credentials, no network, and no LinkedIn account**.
The environment is scrubbed of credential variables before settings are built, so
a developer with a populated `.env` gets the same run as CI does with none.

```bash
pytest          # 148 tests
ruff check .
```

Fixtures in `tests/fixtures/` are **synthetic** — Voyager payload *shapes* with
invented content. No real member's data and no session material is committed.
`scripts/capture_fixture.py` regenerates them from a live session, redacting as
it captures; its output still needs reading before it is committed, and it says
so when it runs.

What the tests actually pin down, beyond the happy path:

- **A 999 is never retried**, a 429 is retried to the attempt limit, and a 401
  re-acquires the session exactly once — asserted on call counts, not just on
  the exception type.
- **The circuit breaker stops the second call before it leaves the process.**
- **Cycle detection and missing references** in the URN resolver.
- **The per-section merge**: GraphQL serves experience, `profileView` backfills
  only certifications, source becomes `mixed`, `sections_unavailable` empties.
- **A block stops the chain** instead of falling through to the next endpoint.
- **A stale queryId self-heals**, and rediscovery is bounded to one retry.
- **A `profileView` that returns 200 with nothing does not claim to be the source.**
- **A failure is never dressed up as an empty profile** — the response has no
  `profile` key at all.
- **Session diagnostics never leak the cookie**, and the log redaction filter
  catches a cookie passed as a deferred `%s` argument, not just an inline one.
- **Concurrent section rejections trigger one rediscovery, not twelve.**
- **A pinned queryId is never silently worked around** -- it fails loudly.
- **A `.env` file with `API_KEYS=a,b` actually loads.** It did not before:
  pydantic-settings JSON-decodes list fields from dotenv before validators
  run, so the app started fine in tests and refused to start in production.

---

## Project layout

```
app/
  main.py              FastAPI app, lifespan wiring, error translation
  config.py            pydantic-settings; every secret env-only
  errors.py            Typed failures, each mapping to one HTTP status
  schema.py            The public response contract
  cache.py             In-process TTL cache
  logging_config.py    Redaction filter
  api/
    routes.py          /v1/profile, /v1/session, /v1/demo, /healthz
    deps.py            API-key gate, demo allowlist, rate limiting
  linkedin/
    urls.py            Profile URL -> public identifier
    auth.py            Session acquisition (cookie or login)
    client.py          Voyager HTTP: headers, retries, circuit breaker
    queryids.py        Runtime queryId discovery
    normalize.py       included[] URN graph resolver
    fetch.py           The S1 -> S2 -> S3 strategy chain
  extract/
    common.py          Images, dates, text nodes
    components.py      GraphQL component trees -> schema
    profileview.py     Legacy REST -> schema
```

---

## Licence and intent

Built as a technical exercise in API reverse-engineering. Not affiliated with,
endorsed by, or supported by LinkedIn. Use it against your own account, at your
own risk, and read [Known limitations](#known-limitations) before pointing it at
anything that matters.
