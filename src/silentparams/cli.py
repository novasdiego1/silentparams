"""Command line: silentparams URL [-H 'Name: value'] [--json]."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from silentparams import __version__
from silentparams.core import Report, http_fetch, probe

VERDICT_TEXT = {
    "ignored": "IGNORED silently (2xx, same as without the parameter)",
    "rejected": "rejected (4xx)",
    "alias": "accepted as an alias",
    "different": "2xx, different response (inconclusive)",
    "no-effect": "2xx, but the real parameter has no effect either (inconclusive)",
    "error": "server error or no response",
}


def render(report: Report) -> str:
    lines = [f"URL: {report.url}", f"Baseline: HTTP {report.baseline.status}, {report.baseline.size} bytes"
             + (f", {report.baseline.items} items" if report.baseline.items is not None else ""),
             f"Requests sent: {report.requests}"]
    if not report.stable:
        lines.append("WARNING: the same request gave two different responses. Results are unreliable; "
                     "use --ignore-key for volatile fields (timestamps, request ids).")
        if report.volatile:
            keys = sorted({path.split(".")[-1].removesuffix("[]") for path in report.volatile})
            lines.append(f"  Fields that changed between the two reads: {', '.join(report.volatile)}")
            lines.append("  Try: " + " ".join(f"--ignore-key {k}" for k in keys))
    for p in report.params:
        if p.effective:
            effect = "changes the response"
        elif p.known:
            effect = ("does NOT change the response, but an invalid value does: the name is known and "
                      "the value is probably the default. Decoys re-tested with an invalid value")
        else:
            effect = "does NOT change the response, and neither does an invalid value (ignored or no-op?)"
        lines += ["", f"Parameter '{p.name}={p.value}': removing it {effect}"]
        for d in p.decoys:
            lines.append(f"  {d.name:<24} HTTP {d.response.status:<4} {VERDICT_TEXT[d.verdict]}")
    verdict = report.ignores_unknown_names
    lines.append("")
    if verdict is True:
        lines.append("RESULT: this API silently ignores unknown parameter names. A typo in a filter returns "
                     "unfiltered data with HTTP 200.")
    elif verdict is False:
        lines.append("RESULT: this API rejects unknown parameter names. Typos fail loudly.")
    else:
        lines.append("RESULT: inconclusive. See the per-decoy lines above.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="silentparams", description=__doc__)
    ap.add_argument("url", help="a GET URL with at least one query parameter")
    ap.add_argument("-H", "--header", action="append", default=[], help="extra header, 'Name: value'")
    ap.add_argument("--decoys", type=int, default=6, help="decoy names per parameter (default 6)")
    ap.add_argument("--max-requests", type=int, default=60, help="hard cap on requests sent (default 60)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default 0.5)")
    ap.add_argument("--ignore-key", action="append", default=[], help="JSON key to ignore when comparing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    headers = {}
    for h in args.header:
        name, sep, value = h.partition(":")
        if not sep:
            ap.error(f"bad header {h!r}; use 'Name: value'")
        headers[name.strip()] = value.strip()
    try:
        report = probe(args.url, http_fetch(headers), decoys_per_param=args.decoys,
                       max_requests=args.max_requests, delay=args.delay, ignore_keys=args.ignore_key)
    except (ValueError, RuntimeError) as exc:
        print(f"silentparams: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({**asdict(report), "ignores_unknown_names": report.ignores_unknown_names}, indent=2))
    else:
        print(render(report))
    verdict = report.ignores_unknown_names
    return 1 if verdict else 0 if verdict is False else 3


if __name__ == "__main__":
    sys.exit(main())
