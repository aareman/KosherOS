"""An image is built only with its category lists in full.

On 2026-10-21 the UT1 site answered 404 for every list, the import script
logged a warning per list and carried on, and two :edge images shipped with
no adult, gambling or proxy domains at all (#202). These tests hold the
script to the two things that stop that: the GitHub mirror is read when
UT1 is down, and a category a family can switch on that comes out short
fails the build.
"""

import email.message
import importlib.util
import io
import sqlite3
import tarfile
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]

spec = importlib.util.spec_from_file_location(
    "fetch_categories_floors", ROOT / "scripts/fetch-categories.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

REF = "0" * 40


def not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)


def tarball(name: str, domains: list[str]) -> bytes:
    """A UT1 archive: <name>/domains, one domain per line."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = ("\n".join(domains) + "\n").encode()
        info = tarfile.TarInfo(f"{name}/domains")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class Web:
    """A stand-in for fetch.get: a table of URL -> bytes, 404 for the rest,
    and a record of what was asked for."""

    def __init__(self, pages: dict[str, bytes]):
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, url: str, tries: int = 3) -> bytes:
        self.asked.append(url)
        if url in self.pages:
            return self.pages[url]
        raise not_found(url)


def primary(name: str) -> str:
    return fetch.SOURCE.format(name=name)


def mirror(name: str, file: str = "domains") -> str:
    return fetch.MIRROR.format(ref=REF, name=name, file=file)


def rows(path: Path, category: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {d for (d,) in db.execute(
            "SELECT domain FROM domains WHERE category = ?", (category,))}
    finally:
        db.close()


# -- the fallback ----------------------------------------------------------------


def test_the_primary_is_used_when_it_answers(monkeypatch, tmp_path):
    web = Web({primary("gambling"): tarball("gambling", ["bet.example", "casino.example"])})
    monkeypatch.setattr(fetch, "get", web)
    assert fetch.ut1_domains("gambling", tmp_path, REF) == ["bet.example", "casino.example"]
    assert not any("githubusercontent" in url for url in web.asked), \
        "the mirror was read although UT1 answered"


def test_the_mirror_is_read_when_ut1_is_down(monkeypatch, tmp_path, capsys):
    web = Web({mirror("gambling"): b"bet.example\ncasino.example\n"})
    monkeypatch.setattr(fetch, "get", web)
    assert fetch.ut1_domains("gambling", tmp_path, REF) == ["bet.example", "casino.example"]
    assert web.asked[0] == primary("gambling"), "UT1 is tried first"
    assert "reading gambling from the GitHub mirror" in capsys.readouterr().out


def test_a_list_the_mirror_keeps_in_pieces_is_read_whole(monkeypatch):
    # The adult list is over GitHub's file limit, so the mirror stores it
    # as domains.0, domains.1, ... with no plain `domains` file.
    web = Web({mirror("adult", "domains.0"): b"a.example\n",
               mirror("adult", "domains.1"): b"b.example\nc.example\n"})
    monkeypatch.setattr(fetch, "get", web)
    assert fetch.fetch_mirror("adult", REF) == ["a.example", "b.example", "c.example"]
    assert web.asked[-1] == mirror("adult", "domains.2"), "stops at the first missing piece"


def test_a_name_ut1_serves_as_an_alias_is_not_fetched_from_the_mirror(monkeypatch):
    # UT1 answers `porn` with the adult list; the mirror has only `adult`,
    # which is imported under its own name. Nothing to fetch, nothing lost.
    web = Web({})
    monkeypatch.setattr(fetch, "get", web)
    assert fetch.fetch_mirror("porn", REF) == []
    assert web.asked == []


def test_a_list_neither_source_has_comes_back_empty_not_fatal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fetch, "get", Web({}))
    assert fetch.ut1_domains("sect", tmp_path, REF) == []
    assert "not on the mirror" in capsys.readouterr().err


def test_every_name_in_the_mapping_is_on_the_mirror_or_an_alias_of_one():
    # The mirror's directory listing on 2026-10-21. A new UT1 name added to
    # MAPPING must either be here or be declared an alias, or the fallback
    # cannot fetch it.
    on_mirror = set("""
        adult agressif ai arjel associations_religieuses astrology audio-video
        bank bitcoin blog celebrity chat child cleaning cooking cryptojacking
        dangerous_material dating ddos dialer doh download drogue dynamic-dns
        educational_games examen_pix exceptions_liste_bu fakenews filehosting
        financial forums gambling games hacking jobsearch lingerie liste_blanche
        liste_bu malware manga marketingware mixed_adult mobile-phone phishing
        press publicite radio reaffected redirector remote-control
        residential-proxies sect sexual_education shopping shortener
        social_networks special sports stalkerware strict_redirector
        strong_redirector translation tricheur tricheur_pix update vpn warez
        webhosting webmail""".split())
    for name in fetch.MAPPING:
        assert name in on_mirror or name in fetch.MIRROR_ALIASES, name
    for alias, real in fetch.MIRROR_ALIASES.items():
        assert real in on_mirror, f"{alias} points at {real}, which the mirror lacks"
        assert fetch.MAPPING[alias] == fetch.MAPPING[real], \
            f"{alias} and {real} feed different categories; the alias cannot be skipped"


def test_a_dropped_connection_is_retried_and_a_404_is_not(monkeypatch):
    calls = []

    def flaky(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionResetError("reset")
        return io.BytesIO(b"ok")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    assert fetch.get("https://x.example/list") == b"ok"
    assert len(calls) == 3

    calls.clear()

    def gone(url, timeout):
        calls.append(url)
        raise not_found(url)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", gone)
    with pytest.raises(urllib.error.HTTPError):
        fetch.get("https://x.example/list")
    assert len(calls) == 1, "a 404 is an answer, not a hiccup"


# -- the floors -----------------------------------------------------------------


def test_every_category_a_family_can_switch_on_has_a_floor():
    from kosherd.categories import CATEGORY_LABELS

    assert set(fetch.FLOORS) == set(CATEGORY_LABELS), \
        "a category the admin app offers must have a floor, and only those"
    assert all(floor > 0 for floor in fetch.FLOORS.values())
    # Zero is a broken build, never a quiet one.
    assert fetch.FLOORS["adult"] >= 1_000_000
    assert fetch.FLOORS["proxy"] >= 10_000


def test_a_short_category_fails_the_build(monkeypatch, tmp_path, capsys):
    # Every source is down: UT1 and the mirror. Only the curated supplement
    # lands, a few dozen gambling sites, and that is not a gambling list.
    monkeypatch.setattr(fetch, "get", Web({}))
    out = tmp_path / "categories.sqlite"
    rc = fetch.main(["--out", str(out), "--only", "gambling", "--mirror-ref", REF])
    assert rc == 1
    captured = capsys.readouterr()
    assert "gambling" in captured.err and "below their floor" in captured.err
    assert "SHORT" in captured.out, "the build log shows which one"


def test_a_category_above_its_floor_passes_through_the_mirror(monkeypatch, tmp_path, capsys):
    enough = "\n".join(f"site{i}.example" for i in range(fetch.FLOORS["gambling"])).encode()
    monkeypatch.setattr(fetch, "get", Web({mirror("gambling"): enough}))
    out = tmp_path / "categories.sqlite"
    rc = fetch.main(["--out", str(out), "--only", "gambling", "--mirror-ref", REF])
    assert rc == 0, capsys.readouterr().err
    assert "site0.example" in rows(out, "gambling")
    assert "888.com" in rows(out, "gambling"), "the curated supplement is still merged"
    assert "every required category is above its floor" in capsys.readouterr().out


def test_check_mode_holds_an_existing_catalogue_to_the_floors(tmp_path, capsys):
    path = tmp_path / "categories.sqlite"
    db = sqlite3.connect(path)
    db.executescript(fetch.SCHEMA)
    db.executemany("INSERT INTO domains VALUES (?, ?)",
                   [(f"ad{i}.example", "ads") for i in range(fetch.FLOORS["ads"])])
    db.commit()
    db.close()
    assert fetch.main(["--check", str(path)]) == 1
    err = capsys.readouterr().err
    assert "adult (0)" in err and "proxy (0)" in err

    assert fetch.shortfalls({c: f for c, f in fetch.FLOORS.items()}) == {}
    assert fetch.shortfalls({"adult": 10}, only={"adult"}) == {"adult": 10}
    assert fetch.shortfalls({"adult": 10}, only={"ads"}) == {"ads": 0}


# -- the build and the CI guard ------------------------------------------------


def test_the_image_build_does_not_shrug_off_a_failed_import():
    text = (ROOT / "os-image/Containerfile").read_text()
    step = text[text.index("RUN python3 /tmp/fetch-categories.py"):]
    step = step[:step.index("\nRUN ")]
    assert "||" not in step, "a failed import must fail the build, not print a warning"


@pytest.mark.parametrize("workflow,tag", [("ci.yml", "$IMAGE:edge"), ("try-image.yml", "$IMAGE:$TAG")])
def test_ci_checks_the_built_images_lists_before_pushing_it(workflow, tag):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    check = text.index(f'fetch-categories.py --check-image "{tag}"')
    assert text.index("podman build") < check < text.index("podman push"), \
        "the check sits between the build and the push"


# -- video-call services are in no category a family switches off (#209) --------


def test_call_services_leave_social_and_keep_what_marks_danger(monkeypatch, tmp_path):
    # UT1 files Teams and Skype under chat, which feeds social: a Teams
    # meeting link opened on the block page for every account that blocks
    # social, which is the default.
    chat = ["teams.microsoft.com", "go.trouter.teams.microsoft.com", "skype.com",
            "teams.live.com", "chat.example"]
    web = Web({mirror("chat"): ("\n".join(chat) + "\n").encode(),
               mirror("publicite"): b"click.zoom.us\nads.skype.com\n",
               mirror("social_networks"): b"lookalike-skype.com\n"})
    monkeypatch.setattr(fetch, "get", web)
    out = tmp_path / "categories.sqlite"
    fetch.build(out, {"social", "ads"}, REF)

    social = rows(out, "social")
    assert not social & {"teams.microsoft.com", "go.trouter.teams.microsoft.com",
                         "skype.com", "teams.live.com"}
    # Only the call services: a chat site, and a name that merely ends
    # like one, are still social.
    assert {"chat.example", "lookalike-skype.com", "facebook.com"} <= social
    calls = rows(out, fetch.CALLS)
    assert {"teams.microsoft.com", "teams.live.com", "skype.com",
            "meet.google.com"} <= calls
    assert {"click.zoom.us", "ads.skype.com"} <= rows(out, "ads"), \
        "an ad host under a call service is still an ad host"


def test_the_calls_bucket_is_never_a_filter_toggle():
    from kosherd import categories

    assert fetch.CALLS == categories.CALLS
    assert categories.CALLS not in categories.CATEGORY_LABELS
    assert categories.CALLS not in categories.DEFAULT_BLOCKED
    assert set(fetch.CALLS_KEEP) <= set(categories.CATEGORY_LABELS)
