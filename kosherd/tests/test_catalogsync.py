"""Keeping the five-million-domain catalogue current.

It is 190 MB, so it cannot ride inside a signed JSON document the way the
word lists do. The portal signs a manifest — a URL, a size and a hash —
and the device checks the bytes against that hash before letting them near
the filter. Everything here is about that check being the thing that holds.
"""

import hashlib
import json
import sqlite3

import pytest

from kosherd import catalogsync


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogsync, "CATALOG_PATH",
                        tmp_path / "categories" / "categories.sqlite")
    monkeypatch.setattr(catalogsync, "VERSION_PATH",
                        tmp_path / "categories" / "version.json")
    return tmp_path


def a_catalogue(path, domains=200_000) -> bytes:
    """A real SQLite catalogue, in the shape SqliteBundle expects."""
    from kosherd import categories

    db = sqlite3.connect(path)
    db.executescript(getattr(categories, "SQLITE_SCHEMA", "") or """
        CREATE TABLE IF NOT EXISTS domains (
            domain TEXT PRIMARY KEY, category TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    db.executemany("INSERT OR REPLACE INTO domains (domain, category) VALUES (?, ?)",
                   ((f"host{i}.example", "adult") for i in range(domains)))
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', '1')")
    db.commit()
    db.close()
    return path.read_bytes()


def serve(monkeypatch, payload: bytes):
    """Stand in for the CDN the manifest points at."""
    import io
    import urllib.request

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: Response(payload))


def test_a_catalogue_matching_its_hash_is_installed(paths, monkeypatch):
    blob = a_catalogue(paths / "source.sqlite")
    serve(monkeypatch, blob)
    count = catalogsync.update({
        "version": 4, "url": "https://cdn.example/c.sqlite",
        "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)})
    assert count == 200_000
    assert catalogsync.installed_version() == 4
    assert catalogsync.CATALOG_PATH.exists()


def test_a_catalogue_that_does_not_match_its_hash_is_refused(paths, monkeypatch):
    # The signature covers the hash, so this is what makes the download
    # itself need no trust: a CDN, a mirror, plain HTTP, all fine.
    blob = a_catalogue(paths / "source.sqlite")
    serve(monkeypatch, blob + b"tampered")
    with pytest.raises(catalogsync.CatalogError) as caught:
        catalogsync.update({"version": 1, "url": "https://cdn.example/c",
                            "sha256": hashlib.sha256(blob).hexdigest()})
    assert "signed hash" in str(caught.value)
    assert not catalogsync.CATALOG_PATH.exists()


def test_a_catalogue_of_the_wrong_size_is_refused(paths, monkeypatch):
    blob = a_catalogue(paths / "source.sqlite")
    serve(monkeypatch, blob)
    with pytest.raises(catalogsync.CatalogError):
        catalogsync.update({"version": 1, "url": "https://cdn.example/c",
                            "sha256": hashlib.sha256(blob).hexdigest(),
                            "size": len(blob) + 1})


def test_something_that_is_not_a_database_is_refused(paths, monkeypatch):
    # A redirect page, an error page, an HTML 404 with a correct hash
    # because the manifest was generated from it.
    blob = b"<html>404</html>"
    serve(monkeypatch, blob)
    with pytest.raises(catalogsync.CatalogError):
        catalogsync.update({"version": 1, "url": "https://cdn.example/c",
                            "sha256": hashlib.sha256(blob).hexdigest()})
    assert not catalogsync.CATALOG_PATH.exists()


def test_a_truncated_catalogue_is_refused(paths, monkeypatch):
    # The worst case: it opens, it answers queries, and it is missing most
    # of the web. That looks like it is working.
    blob = a_catalogue(paths / "small.sqlite", domains=50)
    serve(monkeypatch, blob)
    with pytest.raises(catalogsync.CatalogError) as caught:
        catalogsync.update({"version": 1, "url": "https://cdn.example/c",
                            "sha256": hashlib.sha256(blob).hexdigest()})
    assert "only 50 domains" in str(caught.value)


def test_the_old_catalogue_survives_a_failed_update(paths, monkeypatch):
    good = a_catalogue(paths / "good.sqlite")
    serve(monkeypatch, good)
    catalogsync.update({"version": 1, "url": "https://cdn.example/c",
                        "sha256": hashlib.sha256(good).hexdigest()})
    before = catalogsync.CATALOG_PATH.read_bytes()

    serve(monkeypatch, b"rubbish")
    with pytest.raises(catalogsync.CatalogError):
        catalogsync.update({"version": 2, "url": "https://cdn.example/c",
                            "sha256": hashlib.sha256(b"rubbish").hexdigest()})
    assert catalogsync.CATALOG_PATH.read_bytes() == before
    assert catalogsync.installed_version() == 1


def test_an_older_manifest_is_ignored(paths, monkeypatch):
    good = a_catalogue(paths / "good.sqlite")
    serve(monkeypatch, good)
    catalogsync.update({"version": 5, "url": "https://cdn.example/c",
                        "sha256": hashlib.sha256(good).hexdigest()})

    def explode(*a, **k):
        raise AssertionError("an older manifest must not be downloaded")

    monkeypatch.setattr(catalogsync, "download", explode)
    assert catalogsync.update({"version": 5, "url": "x", "sha256": "y"}) is None
    assert catalogsync.update({"version": 4, "url": "x", "sha256": "y"}) is None


def test_an_incomplete_manifest_is_refused(paths):
    for manifest in ({"url": "x", "sha256": "y"},
                     {"version": 1, "sha256": "y"},
                     {"version": 1, "url": "x"},
                     {"version": "one", "url": "x", "sha256": "y"}):
        with pytest.raises(catalogsync.CatalogError):
            catalogsync.update(manifest)


def test_nothing_installed_yet_is_version_zero(paths):
    assert catalogsync.installed_version() == 0
