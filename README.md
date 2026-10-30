# silentparams

Find HTTP APIs that silently ignore misspelled query parameters.

You call `GET /issues?states=closed`. The API only knows `state`. It does not
answer "unknown parameter": it answers **200 with every open issue**, and your
code (or your agent) believes it filtered. Nothing failed, so nothing alerts.

`silentparams` sends a handful of read-only requests with plausible misspellings
of each parameter in your URL and tells you, per name, whether the API rejects
it, accepts it as an alias, or ignores it without a word.

```console
$ silentparams "https://api.github.com/repos/python/cpython/issues?state=closed&per_page=3"
Parameter 'state=closed': removing it changes the response
  states                   HTTP 200  IGNORED silently (2xx, same as without the parameter)
  STATE                    HTTP 200  IGNORED silently (2xx, same as without the parameter)
  zzb22fbb9f               HTTP 200  IGNORED silently (2xx, same as without the parameter)
...
RESULT: this API silently ignores unknown parameter names. A typo in a filter returns unfiltered data with HTTP 200.
```

That is not a bug in GitHub. Ignoring unknown parameters is a common design
choice. It just means **a typo in your client is invisible**, so you should know
which APIs behave this way before you trust a filtered result.

## Install

```console
pip install silentparams   # no dependencies, Python 3.10+
# or the latest code: pip install git+https://github.com/novasdiego1/silentparams
```

## How it works

For each query parameter `p` in the URL:

| Request | Purpose |
|---|---|
| the URL as given, twice | baseline, and a check that the response is stable |
| the URL without `p` | what the API returns when `p` is absent |
| `p` renamed (plural/singular, trailing `_`, case, `snake`/`kebab`/`camel`, doubled letter) and one nonsense name | the decoys |

Each decoy is classified by comparing normalized response bodies (JSON is
canonicalized, so key order does not matter):

| Verdict | Meaning |
|---|---|
| `IGNORED silently` | 2xx and the same response as without `p`: the name was dropped without a word |
| `rejected` | 4xx: the API refuses unknown names (the safe behaviour) |
| `alias` | 2xx and the same response as the baseline: the variant is accepted |
| `different` | 2xx with some other response: inconclusive |
| `no-effect` | 2xx, but removing `p` changes nothing either, so an ignored name and an alias look the same: inconclusive |

When removing `p` changes nothing, `p` itself may be ignored, or its value may
be the server default (`state=open` on an API that returns open items by
default). To tell these apart, silentparams also sends `p=__silentparams__`. If
that invalid value changes the response (usually a 4xx), the name is known, and
each decoy is re-sent with the same invalid value: answering like `p` makes it an
alias, answering like no parameter at all makes it ignored. If the invalid value
changes nothing either, the decoys stay `no-effect`.

## Options

```text
-H 'Name: value'     extra header (repeatable), e.g. an Authorization token
--ignore-key KEY     JSON key to ignore when comparing (timestamps, request ids)
--decoys N           decoy names per parameter (default 6)
--max-requests N     hard cap on requests sent (default 60)
--delay S            seconds between requests (default 0.5)
--json               machine-readable report
```

Exit code: `1` if the API silently ignores unknown names, `0` if it rejects
them, `3` if the result is inconclusive, `2` on usage errors. That makes it
usable as a CI check for an API you own: treat `3` as "look at it", not as a pass.

If the same request returns different bodies (a timestamp, a request id), the
report warns that results are unreliable, lists the fields that changed between
the two reads and suggests the matching `--ignore-key` flags.

## Be polite

It only sends `GET` requests, waits between them, and stops at
`--max-requests`. Point it at APIs you are allowed to query, and mind their
rate limits.

## Why this exists

An auditor reported reaching a wrong conclusion this way. They tried four
spellings of a per-author filter (`author`, `agent`, `agent_id`, `pubkey`) on an
API that only declared `authors`. Every request returned 200 with unfiltered
rows, so they wrote that no such filter existed. The same API rejected a bad
`limit` value with 422: it checked values carefully and never checked names.

## License

MIT
