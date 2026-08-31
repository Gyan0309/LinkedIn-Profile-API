# LinkedIn Profile API

Give it a LinkedIn profile URL, get the profile back as structured JSON.

It works by calling **Voyager**, the private API `linkedin.com` uses for itself,
over ordinary authenticated HTTP. No browser, no page scraping.

There's a web UI at `/` if you'd rather click than curl.

```bash
curl -H "X-LinkedIn-Cookie: li_at=...; JSESSIONID=..." \
     -H "X-LinkedIn-UA: $YOUR_BROWSERS_USER_AGENT" \
     "https://linkedin-profile-api.fly.dev/v1/profile?url=https://www.linkedin.com/in/williamhgates"
```

> Automated access breaches LinkedIn's User Agreement §8.2, and the account
> behind a request can be restricted. Use one you can afford to lose.

---

## Getting a session

**The service stores no credentials.** You send your own LinkedIn session with
each request, so it spends your account's budget rather than anyone else's, and
there's nothing on the server to leak.

1. Open linkedin.com signed in, press <kbd>F12</kbd>, go to the **Network** tab.
2. Reload, click any request to `www.linkedin.com`.
3. Under **Request Headers**, copy the whole `cookie` value.
4. Send it as the `X-LinkedIn-Cookie` header.
5. Send that browser's User-Agent as `X-LinkedIn-UA`. The web UI does this for
   you; from curl, copy it from the same Request Headers panel.

Use the Network tab, not Application. `li_at` on its own isn't enough — LinkedIn
also wants `JSESSIONID`, `lidc` and `bcookie`, and without them it redirects to
a login page.

**Send the User-Agent.** LinkedIn ties a session to the device that created it,
and the User-Agent is most of that fingerprint. Replay the cookie under a
different one and LinkedIn reads it as a stolen session: it invalidates the
token everywhere, which signs you out of your own browser too. The request works
without the header, and sending it is still the difference between a session
that survives and one that dies after a call or two.

Create the session at home rather than on a server. A login from a datacenter IP
is the quickest way to trigger a verification challenge.

---

## API

### `GET /v1/profile`

| | |
|---|---|
| `url` (query, required) | A profile URL, or just the identifier |
| `refresh` (query) | `true` skips the cache |
| `X-LinkedIn-Cookie` (header, required) | Your session |
| `X-LinkedIn-UA` (header) | The User-Agent of the browser the cookie came from |
| `X-LinkedIn-TZ` (header) | Your UTC offset in hours, e.g. `5.5` |

Any URL shape works — locale subdomains, tracking parameters, trailing paths
like `/details/experience/`, the legacy `/pub/` form, percent-encoded unicode
names. Company and job URLs are rejected with a message saying which you pasted.

Returns `meta` and `profile`. The profile carries name, headline, about,
location, industry, images, experience, education, skills, certifications,
languages, projects, publications, honours, volunteering, courses, patents and
organisations.

**One thing to know before using the output.** An empty list always means the
person has none. It never means the fetch failed — anything that couldn't be
retrieved is named in `meta.sections_unavailable`:

```jsonc
{
  "meta": { "source": "mixed", "sections_unavailable": ["patents"] },
  "profile": {
    "skills":  [],   // fetched fine, this person lists none
    "patents": []    // NOT fetched, see above
  }
}
```

Without that split a broken fetch is indistinguishable from an empty section,
and anything built on top will quietly treat one as the other.

`meta.source` says which endpoint answered: `voyager-dash-collections`,
`voyager-rest-dash`, `voyager-graphql`, `voyager-rest-profileview`, or `mixed`.

Several roles at one employer stay grouped — the outer entry holds the company
and full tenure, `sub_positions` holds the roles, so a promotion doesn't read as
two unrelated jobs.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Web UI |
| `GET /docs` | Interactive OpenAPI reference |
| `GET /healthz` | Liveness. Never touches LinkedIn |
| `GET /v1/session` | Circuit breaker and cache state |

### Errors

Every failure has a stable `error` code so callers can branch without parsing
prose. A failure is never returned as a 200 with an empty profile.

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_profile_url` | Not a profile URL |
| 429 | `rate_limited` | Your quota. Carries `Retry-After` |
| 503 | `linkedin_session_unavailable` | No cookie sent |
| 503 | `linkedin_session_rejected` | Cookie incomplete or expired |
| 503 | `linkedin_blocked` | HTTP 999 or 403 upstream |
| 503 | `endpoint_retired` | LinkedIn withdrew that endpoint |
| 503 | `upstream_unavailable` | Everything failed |

---

## Running it

```bash
git clone https://github.com/Gyan0309/linkedin-profile-api.git
cd linkedin-profile-api
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Then <http://localhost:8000>. No configuration needed — there are no secrets to
set. `.env.example` lists the optional tuning knobs.

Docker works too: `docker build -t linkedin-profile-api .`


---

## Approach

LinkedIn's web app is a single-page app, so every profile section it renders
arrives as JSON from `linkedin.com/voyager/api/*`. Watching the network tab
while a profile loads gives you the endpoints, headers and query syntax.

Four headers make an authenticated Voyager request work:

- `Cookie` — the session, with `JSESSIONID` keeping its literal quotes
- `csrf-token` — the same JSESSIONID value, quotes stripped. A mismatch is a silent 403
- `x-restli-protocol-version: 2.0.0`
- `Accept: application/vnd.linkedin.normalized+json+2.1`

That last header is load-bearing. It returns a deduplicated object graph instead
of a tree: `data` holds URN references, `included` holds each entity once.
Efficient on the wire, unusable for extraction, so `normalize.py` rebuilds the
tree. Two things there aren't optional — the graph is genuinely cyclic (a
position points at a company that points back at its positions), and references
go missing when the session isn't entitled to see them.

**The interesting part is what turned out not to work.** This was originally
built around the GraphQL `ProfileComponents` query that most write-ups describe.
Capturing every request a real profile page makes shows linkedin.com never calls
it any more — the sections are server-rendered into the HTML now. And the legacy
`profileView` endpoint answers 410 Gone.

What does work is a family of dash REST collections, one per section, keyed on
the profile URN:

```
GET /voyager/api/identity/dash/profileSkills?q=viewee&profileUrn=<urn>&count=100
```

All thirteen return 200. They need no `queryId`, so nothing rotates and nothing
has to be discovered — and discovery was the real problem, because finding a
queryId means fetching a profile *page*, which is the request LinkedIn's bot
detection scores hardest. It expects a browser running JavaScript; an `httpx`
GET draws HTTP 999. This path never touches a page. It also returns typed
fields, so dates are integers rather than strings like `"Jan 2020 · Present ·
3 yrs"` that have to be parsed back.

So the chain is identity first (dash profiles), then sections (dash collections,
then GraphQL cards if a queryId is pinned, then `profileView`), merged per
section so a partial failure still returns everything else.

Three details that would otherwise have shipped as quiet bugs:

- `count=100` is required. LinkedIn pages at 20 and reports the truth only in
  `paging.total`. A 21-skill profile returned 20 — wrong, and well-formed.
- Position groups don't contain their roles, so the grouping is rebuilt by
  joining positions on `companyUrn`.
- The Profile entity carries bare URNs for location and industry; a
  `decorationId` is needed to make LinkedIn resolve them.

On not retrying a block: 429 gets exponential backoff with jitter, 999 and 403
get none. A block trips a circuit breaker that stops outbound traffic for five
minutes. Retrying a block is how a throttled account becomes a banned one.

---

## Known limitations

- **Datacenter IPs draw HTTP 999.** Authenticated calls fare much better than
  anonymous ones, but a flagged host needs `OUTBOUND_PROXY_URL` pointed at a
  residential proxy. Datacenter proxies don't help.
- **The account can be restricted.** Inherent to the exercise. Sending
  `X-LinkedIn-UA` removes the most common trigger -- a session replayed
  under the wrong browser -- but does not make automated access invisible.
- **A new account sees less.** Visibility depends on network distance, so
  completeness is a property of the session rather than this code.
- **`connections` and `followers` are always null.** They aren't on the Profile
  entity under any decoration tried; they likely need a separate endpoint.
- **Company and school logos are null.** Dash positions carry `companyUrn` but
  not the company object.
- **Endpoints shift without notice.** Two of the four strategies here died
  during development. `sections_unavailable` means that degrades honestly rather
  than silently.
- **Profiles only.** No company, job or search endpoints.
- **English.** Requests set `en_US`.

---

## Tests

```bash
pytest        # 168 tests
ruff check .
```

The suite runs with no LinkedIn account, no credentials and no network — it
replays saved responses, and the environment is scrubbed of credential variables
first so a populated `.env` can't change the result. Fixtures in
`tests/fixtures/` are synthetic; no real profile data is committed.

Beyond the happy path they pin the things that would break quietly: that a 999
is never retried and a 429 is, that the circuit breaker stops the second call
before it leaves the process, that cycle detection terminates, that a section
LinkedIn says is empty isn't reported as unavailable, that two callers with
different sessions never share a cache entry, and that a URN parameter is
percent-encoded while rest.li tuple syntax isn't.

---

Not affiliated with LinkedIn. Built as an engineering exercise.
