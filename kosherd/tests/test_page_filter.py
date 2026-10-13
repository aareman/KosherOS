"""The page filter: what the proxy cannot see, the page shows it (#83).

A chat message, a feed item, a search result fetched as JSON and drawn by
script never passes through the proxy as text. A script of the proxy's
own, served on a reserved address and injected into every page it
filters, runs the same word list over what the page adds and sends the
words the page adds back to be judged by the same scorer.
"""

import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from kosherd import content as content_mod
from kosherd import language as language_mod
from kosherd import siterules as siterules_mod
from test_mitm_addon import addon  # noqa: F401  (the addon with mitmproxy stubbed)

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "mitm/kosher_page.js"
SHIPPED = ROOT / "os-image/files/usr/share/kosher"
WORDS = SHIPPED / "wordlist.json"
TERMS = SHIPPED / "content-terms.json"
HARNESS = Path(__file__).with_name("page_filter_harness.js")


def _policy(language="substitute", media="none", blocked=(), rules=()):
    return types.SimpleNamespace(
        language_filter_for=lambda uid: language,
        media_level_for=lambda uid: media,
        blocked_categories_for=lambda uid: list(blocked),
        rules_for=lambda uid: list(rules),
        uid_for_listener_port=lambda port: None,
    )


def _filter(addon, **policy):
    filt = addon.KosherFilter.__new__(addon.KosherFilter)
    filt.policy = _policy(**policy)
    filt.wordlist = language_mod.load(WORDS)
    filt.scorer = content_mod.load(TERMS)
    filt.siterules = siterules_mod.load()
    filt.page_levels = {}
    filt.notes = []
    filt._note = lambda kind, flow, **fields: filt.notes.append((kind, fields))
    return filt


def _flow(url="https://example.test/page", method="GET", body=b"", headers=None,
          response_headers=None, html=None):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    request = types.SimpleNamespace(
        method=method, path=path, pretty_host=parts.hostname, pretty_url=url,
        headers=dict(headers or {}), get_text=lambda strict=False: body.decode())
    response = None
    if html is not None:
        response = _Response(html, {"content-type": "text/html; charset=utf-8",
                                    **(response_headers or {})})
    return types.SimpleNamespace(request=request, response=response, metadata={"kosher_uid": 1001})


class _Response:
    """A response the addon can read and rewrite, as mitmproxy's can."""

    def __init__(self, text: str, headers: dict):
        self.text = text
        self.headers = headers
        self.status_code = 200

    def get_text(self, strict: bool = False) -> str:
        return self.text

    @property
    def content(self) -> bytes:
        return self.text.encode()


# -- the script and the words are served, cacheable, on any host ----------------

def test_the_script_is_served_on_every_host_and_cached(addon):
    filt = _filter(addon)
    flow = _flow("https://anything.example/__kosheros__/filter.js")
    assert filt._serve_page_filter(flow, 1001) is True
    assert flow.response.status_code == 200
    assert flow.response.content == SCRIPT.read_bytes()
    assert "javascript" in flow.response.headers["Content-Type"]
    assert "max-age=86400" in flow.response.headers["Cache-Control"]
    etag = flow.response.headers["ETag"]
    again = _flow("https://other.example/__kosheros__/filter.js", headers={"if-none-match": etag})
    assert filt._serve_page_filter(again, 1001) is True
    assert again.response.status_code == 304


def test_the_words_are_the_proxys_own_patterns(addon):
    filt = _filter(addon)
    flow = _flow("https://x.example/__kosheros__/words.json")
    assert filt._serve_page_filter(flow, 1001) is True
    doc = json.loads(flow.response.content)
    assert doc["patterns"] and doc["words"]
    sources = {pat.pattern for _p, pat in filt.wordlist._patterns}
    assert {p["pattern"] for p in doc["patterns"]} == sources
    # Longest first, each with its own pattern and the milder word.
    assert [w[0] for w in doc["words"]] == list(filt.wordlist._order)
    assert all(len(w) == 3 and w[1] and w[2] for w in doc["words"])


def test_other_reserved_methods_are_not_ours(addon):
    filt = _filter(addon)
    assert filt._serve_page_filter(_flow("https://x.example/__kosheros__/filter.js", method="POST"), 1001) is False
    assert filt._serve_page_filter(_flow("https://x.example/page"), 1001) is False


# -- it is injected where it has something to do --------------------------------

def test_the_script_is_injected_into_a_page_that_is_let_through(addon):
    filt = _filter(addon, language="substitute", media="immodest")
    flow = _flow(html="<html><head><title>t</title></head><body><p>A plain page about socks.</p></body></html>")
    filt._filter_page(flow, 1001)
    text = flow.response.text
    assert '<script src="/__kosheros__/filter.js" data-kosheros-language="substitute" data-kosheros-judge="1" defer></script></head>' in text
    assert flow.response.headers["x-kosheros-page"] == "filter-script"


def test_language_only_when_pictures_are_not_judged(addon):
    filt = _filter(addon, language="block", media="none")
    flow = _flow(html="<html><body>Socks.</body></html>")
    filt._filter_page(flow, 1001)
    assert 'data-kosheros-language="block" data-kosheros-judge="0"' in flow.response.text


def test_nothing_is_injected_when_there_is_nothing_to_do(addon):
    filt = _filter(addon, language="off", media="none")
    flow = _flow(html="<html><body>Socks.</body></html>")
    filt._filter_page(flow, 1001)
    assert "__kosheros__" not in flow.response.text


def test_a_blocked_page_carries_no_script(addon):
    filt = _filter(addon, language="substitute", media="immodest")
    flow = _flow(html="<html><body>Lingerie sale: bras, panties, thongs, bralettes, "
                      "corsets and intimate apparel. Sexy babydoll and negligee sets.</body></html>")
    filt._filter_page(flow, 1001)
    assert flow.response.status_code == 403
    assert b"__kosheros__/filter.js" not in flow.response.content


def test_a_page_with_a_script_nonce_gets_it_on_the_tag(addon):
    filt = _filter(addon, language="substitute", media="none")
    flow = _flow(html="<html><head></head><body>Socks.</body></html>",
                 response_headers={"content-security-policy": "script-src 'nonce-abc123' 'strict-dynamic'"})
    filt._filter_page(flow, 1001)
    assert 'nonce="abc123"' in flow.response.text


def test_injection_is_idempotent_and_only_for_html(addon):
    filt = _filter(addon, language="substitute", media="none")
    flow = _flow(html='<html><body><script src="/__kosheros__/filter.js"></script></body></html>')
    filt._filter_page(flow, 1001)
    assert flow.response.text.count("filter.js") == 1
    flow = _flow(html='{"a": 1}')
    flow.response.headers["content-type"] = "application/json"
    filt._filter_page(flow, 1001)
    assert "__kosheros__" not in flow.response.text


# -- what the page shows later is judged by the same rules ------------------------

def _judge(filt, body: dict, url="https://example.test/page"):
    flow = _flow(f"https://example.test/__kosheros__/judge", method="POST",
                 body=json.dumps({"url": url, **body}).encode())
    assert filt._serve_page_filter(flow, 1001) is True
    assert flow.response.status_code == 200
    return json.loads(flow.response.content), flow


def test_rendered_content_is_judged_with_the_accounts_tolerance(addon):
    filt = _filter(addon, media="immodest")
    verdict, _ = _judge(filt, {"text": "Lingerie sale: bras, panties, thongs, bralettes, corsets and "
                                       "intimate apparel. Sexy babydoll and negligee sets."})
    assert verdict["blocked"] is True and verdict["why"].startswith("content:")
    assert "This page is blocked" in verdict["page"] and "example.test/page" in verdict["page"]
    assert filt.notes and filt.notes[0][1]["why"] == verdict["why"]


def test_clean_rendered_content_passes(addon):
    filt = _filter(addon, media="immodest")
    verdict, _ = _judge(filt, {"text": "Weather for tomorrow: sunny with a light breeze, 21 degrees."})
    assert verdict == {"blocked": False, "level": "clean"}
    assert filt.notes == []


def test_an_allow_rule_is_honoured_for_rendered_content(addon):
    from kosherd.urlrules import parse_rules

    filt = _filter(addon, media="immodest",
                   rules=parse_rules([{"action": "allow", "pattern": "example.test"}]))
    verdict, _ = _judge(filt, {"text": "Lingerie sale: bras, panties, thongs, bralettes, corsets and "
                                       "intimate apparel. Sexy babydoll and negligee sets."})
    assert verdict["blocked"] is False


def test_a_listed_word_rendered_later_blocks_in_block_mode_only(addon):
    word = next(iter(language_mod.load(WORDS).replacements))
    filt = _filter(addon, language="block")
    verdict, _ = _judge(filt, {"language": word})
    assert verdict["blocked"] is True and verdict["why"] == "language"
    filt = _filter(addon, language="substitute")
    verdict, _ = _judge(filt, {"language": word})
    assert verdict["blocked"] is False
    # ...and the page's word is checked against the list, not believed.
    filt = _filter(addon, language="block")
    verdict, _ = _judge(filt, {"language": "weather"})
    assert verdict["blocked"] is False


def test_a_judge_call_is_never_cached_and_survives_junk(addon):
    filt = _filter(addon)
    flow = _flow("https://x.example/__kosheros__/judge", method="POST", body=b"not json")
    assert filt._serve_page_filter(flow, 1001) is True
    assert json.loads(flow.response.content)["blocked"] is False
    assert flow.response.headers["Cache-Control"] == "no-store"


# -- the script itself -----------------------------------------------------------------

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_the_script_parses():
    subprocess.run(["node", "--check", str(SCRIPT)], check=True)


def _words_file(addon, tmp_path) -> Path:
    filt = _filter(addon)
    flow = _flow("https://x.example/__kosheros__/words.json")
    filt._serve_page_filter(flow, 1001)
    path = tmp_path / "words.json"
    path.write_bytes(flow.response.content)
    return path


@node
def test_every_pattern_the_proxy_serves_compiles_in_a_browser(addon, tmp_path):
    words = _words_file(addon, tmp_path)
    check = tmp_path / "compile.js"
    check.write_text(
        'const d = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));\n'
        'for (const p of d.patterns) { new RegExp(p.pattern, "gi"); if (p.presence) new RegExp(p.presence); }\n'
        'for (const w of d.words) new RegExp("^(?:" + w[1] + ")$", "i");\n'
        'console.log(d.patterns.length, d.words.length);\n')
    out = subprocess.run(["node", str(check), str(words)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.split()[1]) == len(language_mod.load(WORDS).replacements)


@node
def test_the_page_cleans_what_it_adds_with_the_same_words(addon, tmp_path):
    wl = language_mod.load(WORDS)
    word = next(w for w in wl.replacements if w.isascii() and len(w) >= 4)
    milder = wl.replacements[word]
    words = _words_file(addon, tmp_path)
    out = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT), str(words), "substitute",
         f"A message with {word} in it", f"And {word.upper()} shouted", "Nothing to see"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == f"A message with {milder} in it"
    assert lines[1] == f"And {milder.upper()} shouted"
    assert lines[2] == "Nothing to see"
    # ...and what it added was sent to be judged, cleaned.
    assert lines[3].startswith("judged: ") and word not in lines[3] and milder in lines[3]


@node
def test_in_block_mode_the_page_asks_for_the_block_page(addon, tmp_path):
    wl = language_mod.load(WORDS)
    word = next(w for w in wl.replacements if w.isascii() and len(w) >= 4)
    words = _words_file(addon, tmp_path)
    out = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT), str(words), "block", f"so {word} then"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == f"so {word} then"          # not rewritten: blocked instead
    assert lines[1] == f"judged: language={word}"


@node
def test_with_the_language_filter_off_only_judging_happens(addon, tmp_path):
    words = _words_file(addon, tmp_path)
    out = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT), str(words), "off", "Plain words"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["Plain words", "judged: Plain words"]
