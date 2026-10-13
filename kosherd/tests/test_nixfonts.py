from kosherd import nixfonts


def test_fonts_are_copied_and_upgraded_and_removed_without_touching_user_fonts(tmp_path, monkeypatch):
    cache_calls = []
    monkeypatch.setattr(nixfonts.subprocess, "run", lambda argv, **kw: cache_calls.append(argv))
    old = tmp_path / "store/old/share/fonts"
    old.mkdir(parents=True)
    (old / "Example.ttf").write_bytes(b"old font")
    (old / "README.txt").write_text("not a font")
    dest = tmp_path / "user/fonts/kosher-nix"
    dest.mkdir(parents=True)
    (dest / "Personal.otf").write_bytes(b"personal font")
    assert nixfonts.sync(old, dest) == (1, 0)
    assert not (dest / "Example.ttf").is_symlink(), "Flatpak must not need access to /nix/store"
    assert (dest / "Example.ttf").read_bytes() == b"old font"
    assert not (dest / "README.txt").exists()
    assert nixfonts.sync(old, dest) == (0, 0)
    assert len(cache_calls) == 1
    new = tmp_path / "store/new/share/fonts"
    new.mkdir(parents=True)
    (new / "Example.ttf").write_bytes(b"new font")
    assert nixfonts.sync(new, dest) == (1, 0)
    assert (dest / "Example.ttf").read_bytes() == b"new font"
    assert nixfonts.sync(None, dest) == (0, 1)
    assert not (dest / "Example.ttf").exists()
    assert (old / "Example.ttf").exists(), "removal cannot depend on Nix garbage collection"
    assert (dest / "Personal.otf").read_bytes() == b"personal font"
    assert len(cache_calls) == 3


def test_edited_fonts_and_unmanaged_name_collisions_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(nixfonts.subprocess, "run", lambda *a, **kw: None)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Edited.ttf").write_bytes(b"font")
    (source / "Personal.ttf").write_bytes(b"upstream font")
    dest = tmp_path / "fonts"
    dest.mkdir()
    (dest / "Personal.ttf").write_bytes(b"my font")
    assert nixfonts.sync(source, dest) == (1, 0)
    (dest / "Edited.ttf").write_bytes(b"my edit")
    assert nixfonts.sync(None, dest) == (0, 0)
    assert (dest / "Edited.ttf").read_bytes() == b"my edit"
    assert (dest / "Personal.ttf").read_bytes() == b"my font"


def test_desktop_sync_follows_profile_font_directories(tmp_path, monkeypatch):
    from kosherd import nixdesktop

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(nixfonts.subprocess, "run", lambda *a, **kw: None)
    home = tmp_path / "home"
    profile_fonts = home / ".nix-profile/share/fonts"
    profile_fonts.mkdir(parents=True)
    store_fonts = tmp_path / "store/package/share/fonts/truetype"
    store_fonts.mkdir(parents=True)
    (store_fonts / "NerdFont.ttf").write_bytes(b"font")
    (profile_fonts / "truetype").symlink_to(store_fonts, target_is_directory=True)
    # A malformed package must not make the desktop helper recurse forever.
    (store_fonts / "loop").symlink_to(store_fonts, target_is_directory=True)
    assert nixdesktop.sync(home) == (1, 0)
    copied = home / ".local/share/fonts/kosher-nix/truetype/NerdFont.ttf"
    assert copied.read_bytes() == b"font"
    assert not copied.is_symlink()
    (profile_fonts / "truetype").unlink()
    assert nixdesktop.sync(home) == (0, 1)
    assert not copied.exists()
