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
that answers like `baseline` is an accepted alias. If `baseline` and `without`
are the same, the parameter has no visible effect (often because its value is
the server default), so "ignored" and "alias" cannot be told apart. In that case
the probe sends the real name with an invalid value: if that changes the
response (typically a 4xx), the name is known, and each decoy is re-sent with
the same invalid value to tell an alias from an ignored name.
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

USER_AGENT = "silentparams/0.2 (read-only API probe)"
INVALID_VALUE = "__silentparams__"  # a value no real filter should accept


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
    verdict: str  # "ignored", "rejected", "alias", "different", "no-effect", "error"


@dataclass
class ParamReport:
    name: str
    value: str
    effective: bool  # removing it changes the response
    without: Response
    known: bool | None = None  # only for params without effect: an invalid value changes the response
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
    volatile: list[str] = field(default_factory=list)  # JSON paths that changed between the two baseline reads

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


def volatile_paths(a: bytes, b: bytes, limit: int = 10) -> list[str]:
    """JSON paths whose values differ between two reads of the same URL (list items shown as [])."""
    try:
        x, y = json.loads(a), json.loads(b)
    except ValueError:
        return []
    out: set[str] = set()

    def walk(u, v, path: str) -> None:
        if isinstance(u, dict) and isinstance(v, dict):
            for k in u.keys() | v.keys():
                walk(u.get(k), v.get(k), f"{path}.{k}" if path else k)
        elif isinstance(u, list) and isinstance(v, list) and len(u) == len(v):
            for i, j in zip(u, v, strict=True):
                walk(i, j, path + "[]")
        elif u != v:
            out.add(path or "(root)")

    walk(x, y, "")
    return sorted(out)[:limit]


def same(a: Response, b: Response) -> bool:
    return a.status == b.status and a.fingerprint == b.fingerprint


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
    if baseline.fingerprint == without.fingerprint:
        return "no-effect"  # the real name changes nothing, so ignored and alias look the same
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

    def get_raw(u: str) -> tuple[Response, bytes]:
        nonlocal count
        if count >= max_requests:
            raise RuntimeError(f"request budget exhausted ({max_requests}); raise --max-requests")
        if count and delay:
            time.sleep(delay)
        count += 1
        try:
            status, body, ctype = fetch(u)
        except OSError:
            return Response(0, "", 0), b""
        fp, items = normalize(body, ctype, ignore)
        return Response(status, fp, len(body), items), body

    def get(u: str) -> Response:
        return get_raw(u)[0]

    baseline, body_a = get_raw(url)
    second, body_b = get_raw(url)
    stable = second.fingerprint == baseline.fingerprint
    volatile = [] if stable else volatile_paths(body_a, body_b)
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
        if not report.effective:
            # Removing the name changes nothing. If an invalid value does, the name is known and the
            # decoys can be re-tested with that value: an alias answers like it, an ignored name like `without`.
            invalid = get(with_params(url, rest[:i] + [(name, INVALID_VALUE)] + rest[i:]))
            report.known = not same(invalid, without)
            if report.known:
                for d in report.decoys:
                    if d.verdict != "no-effect":
                        continue
                    r = get(with_params(url, rest[:i] + [(d.name, INVALID_VALUE)] + rest[i:]))
                    if same(r, invalid):
                        d.verdict = "alias"
                    elif same(r, without) and 200 <= r.status < 300:
                        d.verdict = "ignored"
        reports.append(report)
    return Report(url, baseline, stable, reports, count, volatile)
