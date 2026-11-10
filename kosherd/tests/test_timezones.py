"""The first-boot time zone question: the list, the guess, and what the
text setup accepts. Run against a small zoneinfo of its own, so the
answers do not depend on the tzdata of the machine running the tests."""

import io
import json

import pytest

from kosherd import timezones


@pytest.fixture
def root(tmp_path):
    (tmp_path / "iso3166.tab").write_text(
        "# comment\nIL\tIsrael\nUS\tUnited States\nGB\tBritain (UK)\nTT\tTrinidad & Tobago\n")
    (tmp_path / "zone.tab").write_text(
        "# comment\n"
        "IL\t+314650+0351326\tAsia/Jerusalem\n"
        "US\t+404251-0740023\tAmerica/New_York\tEastern (most areas)\n"
        "GB\t+513030-0000731\tEurope/London\n"
        "TT\t+1039-06131\tAmerica/Port_of_Spain\n")
    (tmp_path / "tzdata.zi").write_text(
        "# version\nZ Asia/Jerusalem 2:20:54 - LMT 1880\n"
        "Z America/New_York -4:56:2 - LMT 1883\n"
        "Z Europe/London -0:1:15 - LMT 1847\n"
        "L Asia/Jerusalem Asia/Tel_Aviv\n")
    return tmp_path


def test_the_list_is_by_city_with_the_country_and_utc_last(root):
    listed = timezones.zones(root)
    assert [z.label for z in listed] == [
        "Jerusalem, Israel", "London, Britain (UK)", "New York, United States",
        "Port of Spain, Trinidad & Tobago", "UTC"]
    assert listed[-1].id == "UTC"


def test_known_names_include_links_and_utc_and_refuse_the_rest(root):
    assert timezones.valid("Asia/Tel_Aviv", root)
    assert timezones.valid("UTC", root)
    assert timezones.valid("America/Port_of_Spain", root)   # from zone.tab
    assert not timezones.valid("Mars/Olympus_Mons", root)
    assert not timezones.valid("", root)
    assert not timezones.valid("../../etc/passwd", root)


def test_a_search_matches_city_country_or_name():
    z = timezones.Zone("America/New_York", "New York", "United States")
    assert z.matches("new york") and z.matches("New_York") and z.matches("united")
    assert z.matches("america") and z.matches("") and not z.matches("israel")


def test_the_text_setup_takes_a_name_in_any_case_or_a_city(root):
    assert timezones.resolve("asia/jerusalem", root) == "Asia/Jerusalem"
    assert timezones.resolve("Jerusalem", root) == "Asia/Jerusalem"
    assert timezones.resolve(" new york ", root) == "America/New_York"
    assert timezones.resolve("new_york", root) == "America/New_York"
    assert timezones.resolve("utc", root) == "UTC"
    assert timezones.resolve("Atlantis", root) == ""
    assert timezones.resolve("", root) == ""


def _answer(monkeypatch, body: bytes | Exception):
    def fake_urlopen(request, timeout):
        assert request.full_url == timezones.GEOIP_URL
        assert timeout <= 5, "the guess must never hold the page up for long"
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body)

    monkeypatch.setattr(timezones.urllib.request, "urlopen", fake_urlopen)


def test_the_guess_is_the_geoip_time_zone(monkeypatch, root):
    _answer(monkeypatch, json.dumps({"city": "Beit Shemesh",
                                     "time_zone": "Asia/Jerusalem"}).encode())
    assert timezones.guess(root=root) == "Asia/Jerusalem"


@pytest.mark.parametrize("body", [
    OSError("no route to host"),
    TimeoutError("timed out"),
    b"<html>blocked</html>",
    json.dumps({"time_zone": None}).encode(),
    json.dumps({"time_zone": "Mars/Olympus_Mons"}).encode(),
    json.dumps(["Asia/Jerusalem"]).encode(),
])
def test_no_answer_or_a_wrong_one_is_no_guess(monkeypatch, root, body):
    _answer(monkeypatch, body)
    assert timezones.guess(root=root) == ""


def test_an_unknown_zone_is_refused_before_timedated_is_asked(monkeypatch):
    monkeypatch.setattr(timezones, "valid", lambda zone_id, root=None: False)
    with pytest.raises(ValueError):
        timezones.set_zone("Mars/Olympus_Mons")
