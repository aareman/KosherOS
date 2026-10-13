import json
import subprocess

import pytest

from kosherstore import metadata


def test_flatpak_lookup_requests_only_the_selected_app(monkeypatch):
    calls = []
    payload = {"id": "org.example.App", "description": "<p>Edit <em>photos</em> &amp; drawings.</p><ul><li>Crop</li><li>Resize</li></ul>",
               "developer_name": "Example", "project_license": "GPL-3.0-or-later",
               "urls": {"homepage": "https://example.org"},
               "releases": [{"type": "development", "version": "3-beta"},
                            {"type": "stable", "version": "2"}],
               "screenshots": [{"caption": "Second", "sizes": [{"src": "https://example.org/2.png"}]},
                               {"default": True, "caption": "First", "sizes": [{"src": "https://example.org/1.png"}]},
                               {"sizes": [{"src": "file:///etc/passwd"}]}]}

    def fetch(url, limit):
        calls.append(url)
        return json.dumps(payload).encode()

    monkeypatch.setattr(metadata, "fetch_bytes", fetch)
    result = metadata.load_details({"ref": "org.example.App"})
    assert calls == ["https://flathub.org/api/v2/appstream/org.example.App"]
    assert result["description"] == "Edit photos & drawings.\n\n• Crop\n• Resize"
    assert result["version"] == "2"
    assert [s["caption"] for s in result["screenshots"]] == ["First", "Second"]
    assert result["homepage"] == "https://example.org"


def test_flatpak_wrong_id_and_invalid_ref_are_rejected(monkeypatch):
    monkeypatch.setattr(metadata, "fetch_bytes", lambda *a, **kw: b'{"id":"org.other.App"}')
    with pytest.raises(metadata.DetailsError):
        metadata.flatpak_details("org.example.App")
    with pytest.raises(metadata.DetailsError):
        metadata.flatpak_details("../etc/passwd")


def test_descriptions_are_plain_text():
    assert metadata.plain_description('<p>Fish &amp; chips</p><script>bad()</script><p>Next</p>') == \
        "Fish & chips\n\nNext"
    assert not metadata.web_url("javascript:alert(1)")
    assert not metadata.web_url("file:///tmp/image.png")


def test_nix_evaluates_one_package_without_installing(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "description": "Short", "longDescription": "A longer explanation.\n\nMore information.",
            "version": "1.2", "license": [{"spdxId": "MIT"}, {"fullName": "Other license"}],
            "homepage": ["not a url", "https://example.org"],
            "maintainers": [{"name": "A Maintainer"}, {"github": "another"}],
            "mainProgram": "example"}), "")
    monkeypatch.setattr(metadata.subprocess, "run", run)
    result = metadata.load_details({"ref": "nixpkgs#example"})
    argv, kwargs = calls[0]
    assert argv[:5] == ["nix", "eval", "--json", "--impure", "nixpkgs#example"]
    assert "install" not in argv and "build" not in argv
    assert kwargs["timeout"] == 90
    assert result == {"description": "A longer explanation.\n\nMore information.",
                      "version": "1.2", "license": "MIT, Other license", "homepage": "https://example.org",
                      "maintainers": "A Maintainer, another", "main_program": "example", "screenshots": []}


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("nix", 90)])
def test_nix_lookup_errors_can_be_reported(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(metadata.subprocess, "run", run)
    with pytest.raises(metadata.DetailsError):
        metadata.nix_details("nixpkgs#hello")


def test_nix_string_license_and_short_description(monkeypatch):
    monkeypatch.setattr(metadata.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, '{"description":"Short","license":"unfree"}', ""))
    result = metadata.nix_details("nixpkgs#example")
    assert result["description"] == "Short" and result["license"] == "unfree"
