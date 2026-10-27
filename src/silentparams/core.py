"""Probe an HTTP API for query parameters it silently ignores.

The failure this catches: a client sends `?author=alice` to an API that only
knows `authors`. Instead of rejecting the unknown name, the API answers 200
with the unfiltered result, and the client believes it filtered.

Method, per query parameter in the URL you give:

1. baseline  - the URL as given.
2. without   - the same URL with that parameter removed.
3. decoys    - the parameter renamed (typos, plural/singular, case, separators)
               and one nonsense name, each with the same value.

A decoy that answers 2xx with the same response as `without` was ignored
silently. A decoy answered with 4xx was rejected (the safe behaviour). A decoy
that answers like `baseline` is an accepted alias.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

USER_AGENT = "silentparams/0.1 (read-only API probe)"


@dataclass(frozen=True)
class Response:
    status: int
    fingerprint: str  # hash of the normalized body
    size: int
    items: int | None = None  # number of records, when the body is a JSON list or wraps one


@dataclass
class DecoyResult:
    name: str
    response: Response
    verdict: str  # "ignored", "rejected", "alias", "different", "error"


@dataclass
class ParamReport:
    name: str
    value: str
    effective: bool  # removing it changes the response
    without: Response
    decoys: list[DecoyResult] = field(default_factory=list)

    @property
    def silently_ignored(self) -> list[str]:
        return [d.name for d in self.decoys if d.verdict == "ignored"]


@dataclass
class Report:
    url: str
    baseline: Response
    stable: bool  # the same request twice gave the same response
    params: list[ParamReport]
    requests: int

    @property
    def ignores_unknown_names(self) -> bool | None:
        """True if any decoy was silently ignored, False if all were rejected, None if inconclusive."""
        verdicts = [d.verdict for p in self.params for d in p.decoys]
        if "ignored" in verdicts:
            return True
        if verdicts and all(v == "rejected" for v in verdicts):
            return False
        return None


# --- Decoy names ------------------------------------------------------------------------------

def _split_words(name: str) -> list[str]:
    out, word = [], ""
    for ch in name:
        if ch in "_-.":
            if word:
                out.append(word)
            word = ""
        elif ch.isupper() and word and not word[-1].isupper():
            out.append(word)
            word = ch
        else:
            word += ch
    if word:
        out.append(word)
    return [w.lower() for w in out]


def decoy_names(name: str, limit: int = 6, rng: Callable[[int], str] = secrets.token_hex) -> list[str]:
    """Plausible mistakes for `name`, most likely first, plus one nonsense name. Never returns `name`."""
    words = _split_words(name) or [name.lower()]
    candidates = [
        name[:-1] if name.endswith("s") and len(name) > 2 else name + "s",  # authors <-> author
        name + "_" if not name.endswith("_") else name.rstrip("_"),
        name.upper() if name != name.upper() else name.lower(),
        "_".join(words),
        "-".join(words),
        words[0] + "".join(w.capitalize() for w in words[1:]),  # camelCase
        name[:-1] + name[-1] * 2 if len(name) > 1 else name * 2,  # doubled last letter
    ]
    seen, out = {name}, []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    out = out[: max(limit - 1, 0)]
    out.append("zz" + rng(4))  # nonsense: no API should know it
    return out


# --- HTTP -------------------------------------------------------------------------------------

def normalize(body: bytes, content_type: str, ignore_keys: Iterable[str] = ()) -> tuple[str, int | None]:
    """Fingerprint a body. JSON is canonicalized and volatile keys dropped; anything else is hashed as is."""
    ignore = set(ignore_keys)
    items = None
    if "json" in content_type or body[:1] in (b"{", b"["):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if data is not None:
            def strip(x):
                if isinstance(x, dict):
                    return {k: strip(v) for k, v in x.items() if k not in ignore}
                if isinstance(x, list):
                    return [strip(v) for v in x]
                return x
            data = strip(data)
            if isinstance(data, list):
                items = len(data)
            elif isinstance(data, dict):
                lists = [v for v in data.values() if isinstance(v, list)]
                if len(lists) == 1:
                    items = len(lists[0])
            body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()[:16], items


Fetcher = Callable[[str], tuple[int, bytes, str]]


def http_fetch(headers: dict[str, str] | None = None, timeout: float = 20.0) -> Fetcher:
    """A GET-only fetcher. Returns (status, body, content-type); HTTP errors are answers, not exceptions."""
    base = {"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5", **(headers or {})}

    def fetch(url: str) -> tuple[int, bytes, str]:
        req = urllib.request.Request(url, headers=base, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "") if e.headers else ""

    return fetch


def with_params(url: str, params: list[tuple[str, str]]) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query=urlencode(params)))


# --- Probe ------------------------------------------------------------------------------------

def classify(decoy: Response, baseline: Response, without: Response) -> str:
    if decoy.status >= 500 or decoy.status == 0:
        return "error"
    if 400 <= decoy.status < 500:
        return "rejected"
    same_as_without = decoy.fingerprint == without.fingerprint
    same_as_baseline = decoy.fingerprint == baseline.fingerprint
    if same_as_baseline and not same_as_without:
        return "alias"
    if same_as_without:
        return "ignored"
    return "different"


def probe(
    url: str,
    fetch: Fetcher,
    *,
    decoys_per_param: int = 6,
    max_requests: int = 60,
    delay: float = 0.5,
    ignore_keys: Iterable[str] = (),
    rng: Callable[[int], str] = secrets.token_hex,
) -> Report:
    params = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if not params:
        raise ValueError("the URL has no query parameters to probe")
    ignore = list(ignore_keys)
    count = 0

    def get(u: str) -> Response:
        nonlocal count
        if count >= max_requests:
            raise RuntimeError(f"request budget exhausted ({max_requests}); raise --max-requests")
        if count and delay:
            time.sleep(delay)
        count += 1
        try:
            status, body, ctype = fetch(u)
        except OSError:
            return Response(0, "", 0)
        fp, items = normalize(body, ctype, ignore)
        return Response(status, fp, len(body), items)

    baseline = get(url)
    stable = get(url).fingerprint == baseline.fingerprint
    reports = []
    for i, (name, value) in enumerate(params):
        rest = params[:i] + params[i + 1:]
        without = get(with_params(url, rest))
        report = ParamReport(name, value, effective=without.fingerprint != baseline.fingerprint, without=without)
        for decoy in decoy_names(name, decoys_per_param, rng):
            if any(decoy == n for n, _ in params):
                continue
            r = get(with_params(url, rest[:i] + [(decoy, value)] + rest[i:]))
            report.decoys.append(DecoyResult(decoy, r, classify(r, baseline, without)))
        reports.append(report)
    return Report(url, baseline, stable, reports, count)
