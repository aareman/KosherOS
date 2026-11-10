"""Encryption is selected without authorizing Anaconda to erase a disk."""

import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("installer_encryption", ROOT / "scripts/installer-encryption.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.mark.parametrize("old", ["", '<property name="active">False</property>'])
def test_native_checkbox_is_selected_without_changing_disk_confirmation(tmp_path, old):
    path = tmp_path / "storage.glade"
    path.write_text(f'<interface><object class="GtkCheckButton" id="encryptionCheckbox">{old}'
                    '<property name="label">Encrypt my data.</property>'
                    '<child><object class="GtkLabel" id="encryptionLabel"/></child></object>'
                    '<object class="GtkButton" id="done_button"/></interface>')
    installer.enable_default(path)
    tree = ET.parse(path)
    obj = tree.find(".//object[@id='encryptionCheckbox']")
    assert obj.find("property[@name='active']").text == "True"
    assert obj.find("property[@name='label']").text == "Encrypt my data."
    assert list(obj).index(obj.find("property[@name='active']")) < list(obj).index(obj.find("child"))
    assert tree.find(".//object[@id='done_button']") is not None
    installer.enable_default(path)
    assert len(ET.parse(path).findall(".//property[@name='active']")) == 1


def test_an_unknown_installer_fails_instead_of_defaulting_to_plaintext(tmp_path):
    path = tmp_path / "storage.glade"
    path.write_text('<interface><object class="GtkButton" id="another"/></interface>')
    with pytest.raises(ValueError):
        installer.enable_default(path)


def test_pre_script_runs_the_helper_and_treats_failure_as_fatal():
    config = (ROOT / "os-image/iso-config.toml").read_text()
    assert "%pre --interpreter=/bin/bash --erroronfail" in config
    assert "python3 /usr/share/kosher/installer-encryption.py" in config
    assert "set -eu" in config
