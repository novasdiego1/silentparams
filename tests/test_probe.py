"""End-to-end tests against a local server with one lax and one strict endpoint.

The lax endpoint reproduces the case that motivated the tool: it knows `authors`
and `limit`, validates their values, and silently drops any other name.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import pytest

from silentparams.cli import main
from silentparams.core import Response, classify, decoy_names, http_fetch, normalize, probe

ROWS = [{"id": i, "author": ("alice" if i % 2 else "bob")} for i in range(10)]
KNOWN = {"authors", "limit"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        q = dict(parse_qsl(parts.query))
        if parts.path == "/strict" and set(q) - KNOWN:
            return self._send(422, {"error": f"unknown parameter: {sorted(set(q) - KNOWN)[0]}"})
        rows = ROWS
        if "authors" in q:
            rows = [r for r in rows if r["author"] in q["authors"].split(",")]
        if "limit" in q:
            if not q["limit"].isdigit() or not 0 < int(q["limit"]) <= 100:
                return self._send(422, {"error": "bad limit"})
            rows = rows[: int(q["limit"])]
        self._send(200, {"rows": rows, "generated_at": self.server.tick()})

    def _send(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    clock = iter(range(10**9))
    srv.tick = lambda: next(clock)  # a volatile field, like a timestamp
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def fixed_rng(n):
    return "ab" * n


def test_lax_api_is_caught(server):
    report = probe(f"{server}/lax?authors=alice&limit=3", http_fetch(), delay=0,
                   ignore_keys=["generated_at"], rng=fixed_rng)
    assert report.stable
    assert report.ignores_unknown_names is True
    authors = report.params[0]
    assert authors.effective
    assert "author" in authors.silently_ignored  # the singular typo returns unfiltered rows with 200
    assert "zzabababab" in authors.silently_ignored


def test_strict_api_passes(server):
    report = probe(f"{server}/strict?authors=alice", http_fetch(), delay=0,
                   ignore_keys=["generated_at"], rng=fixed_rng)
    assert report.ignores_unknown_names is False
    assert all(d.verdict == "rejected" for d in report.params[0].decoys)


def test_volatile_field_makes_results_unstable_without_ignore_key(server):
    report = probe(f"{server}/lax?authors=alice", http_fetch(), delay=0, rng=fixed_rng)
    assert not report.stable


def test_request_budget_is_enforced(server):
    with pytest.raises(RuntimeError, match="budget"):
        probe(f"{server}/lax?authors=alice&limit=3", http_fetch(), delay=0, max_requests=4,
              ignore_keys=["generated_at"])


def test_cli_exit_codes(server, capsys):
    assert main([f"{server}/lax?authors=alice", "--delay", "0", "--ignore-key", "generated_at"]) == 1
    assert "silently ignores unknown parameter names" in capsys.readouterr().out
    assert main([f"{server}/strict?authors=alice", "--delay", "0", "--ignore-key", "generated_at"]) == 0
    assert main([f"{server}/lax", "--delay", "0"]) == 2  # nothing to probe


def test_decoy_names():
    names = decoy_names("authors", rng=fixed_rng)
    assert names[0] == "author" and names[-1] == "zzabababab" and "authors" not in names
    assert "pageSize" in decoy_names("page_size", limit=10, rng=fixed_rng)
    assert "page_size" in decoy_names("pageSize", limit=10, rng=fixed_rng)
    assert len(decoy_names("q", limit=3, rng=fixed_rng)) == 3


def test_normalize_ignores_key_order_and_volatile_keys():
    a, n = normalize(b'{"rows":[1,2],"t":1}', "application/json", ["t"])
    b, _ = normalize(b'{"t":2,  "rows":[1,2]}', "application/json", ["t"])
    assert a == b and n == 2


def test_classify():
    base, without = Response(200, "aaa", 1), Response(200, "bbb", 1)
    assert classify(Response(200, "bbb", 1), base, without) == "ignored"
    assert classify(Response(422, "x", 1), base, without) == "rejected"
    assert classify(Response(200, "aaa", 1), base, without) == "alias"
    assert classify(Response(200, "ccc", 1), base, without) == "different"
    assert classify(Response(503, "x", 1), base, without) == "error"
