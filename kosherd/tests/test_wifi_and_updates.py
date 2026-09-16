"""The parsers behind first-boot Wi-Fi and update progress.

Both read another program's output. nmcli's terse format is stable and
documented; bootc's progress JSON is neither, so that parser is tested for
what it must survive as much as for what it must read.
"""

from __future__ import annotations

import json

from kosherd import updates, wifi

# -- nmcli -----------------------------------------------------------------------

def test_terse_lines_split_on_unescaped_colons_only():
    assert wifi.split_terse("wifi:connected:Home") == ["wifi", "connected", "Home"]
    assert wifi.split_terse(r"Caf\:e:70:WPA2:*") == ["Caf:e", "70", "WPA2", "*"]
    assert wifi.split_terse("") == [""]


def test_wired_wins_over_wireless_in_the_status():
    text = "wifi:connected:Home\nethernet:connected:Wired connection 1\nloopback:connected (externally):lo"
    assert wifi.parse_devices(text) == {"online": True, "kind": "ethernet",
                                        "name": "Wired connection 1", "wifi_hardware": True}


def test_an_offline_machine_still_says_whether_it_has_a_wifi_card():
    assert wifi.parse_devices("wifi:disconnected:\nethernet:unavailable:") == {
        "online": False, "kind": "", "name": "", "wifi_hardware": True}
    assert wifi.parse_devices("ethernet:unavailable:")["wifi_hardware"] is False


def test_networks_are_one_per_name_strongest_first_and_the_active_one_on_top():
    text = "\n".join([
        "Home:40:WPA2:",
        "Home:80:WPA2:",             # a second access point, stronger: kept
        "Shul:90:WPA2:*",            # in use: on top whatever the signal
        ":55:WPA2:",                 # hidden: dropped
        "Neighbour:20::",            # open
        "Cafe:65:WPA1 WPA2:",
    ])
    found = wifi.parse_networks(text)
    assert [n["ssid"] for n in found] == ["Shul", "Home", "Cafe", "Neighbour"]
    assert found[0]["active"] and found[1]["signal"] == 80
    assert found[3]["secured"] is False and found[1]["secured"] is True


def test_nmcli_errors_come_out_in_words_a_parent_can_act_on():
    assert "password" in wifi.friendly(
        "Error: Connection activation failed: (7) Secrets were required, but not provided.")
    assert "out of range" in wifi.friendly("Error: No network with SSID 'x' found.")
    assert "too long" in wifi.friendly("Error: Timeout 90 sec expired.")
    assert wifi.friendly("Error: something odd") == "something odd"
    assert wifi.friendly("") == "Could not connect."


# -- bootc progress --------------------------------------------------------------

def test_bytes_give_a_smooth_percentage_across_the_whole_task():
    line = json.dumps({"type": "ProgressBytes", "task": "pulling",
                       "description": "Pulling image", "bytes": 300, "bytesTotal": 1200,
                       "steps": 2, "stepsTotal": 7})
    assert updates.parse_progress(line) == (25, "Pulling image (3 of 7)")


def test_steps_stand_in_when_there_are_no_bytes():
    line = json.dumps({"type": "ProgressSteps", "task": "staging",
                       "description": "Deploying", "steps": 1, "stepsTotal": 4})
    assert updates.parse_progress(line) == (25, "Deploying (2 of 4)")


def test_unreadable_or_irrelevant_lines_are_ignored_not_fatal():
    assert updates.parse_progress("not json") is None
    assert updates.parse_progress("[1,2]") is None
    assert updates.parse_progress(json.dumps({"type": "Something"})) is None
    # A progress line with nothing countable still says what is happening.
    assert updates.parse_progress(json.dumps({"type": "ProgressBytes",
                                              "task": "pulling"})) == (-1, "pulling")


def test_the_percentage_never_leaves_0_to_100():
    over = json.dumps({"type": "ProgressBytes", "bytes": 50, "bytesTotal": 10})
    assert updates.parse_progress(over)[0] == 100


def test_run_upgrade_streams_progress_and_reports_the_end(tmp_path):
    # A stand-in for bootc that writes progress to the fd it is given.
    fake = tmp_path / "bootc"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "fd = int(sys.argv[sys.argv.index('--progress-fd') + 1])\n"
        "for b in (0, 500, 1000):\n"
        "    os.write(fd, (json.dumps({'type': 'ProgressBytes', 'description': 'Pulling',"
        " 'bytes': b, 'bytesTotal': 1000}) + '\\n').encode())\n"
        "print('Queued for next boot')\n")
    fake.chmod(0o755)
    seen, done = [], []
    thread = updates.run_upgrade(lambda p, s: seen.append((p, s)),
                                 lambda ok, err: done.append((ok, err)),
                                 argv=(str(fake), "upgrade"))
    thread.join(timeout=10)
    assert done == [(True, "")]
    assert seen[:3] == [(0, "Pulling"), (50, "Pulling"), (100, "Pulling")]
    assert seen[-1] == (100, "Update ready")


def test_a_failing_upgrade_says_so_with_the_last_lines(tmp_path):
    fake = tmp_path / "bootc"
    fake.write_text("#!/bin/sh\necho 'error: no space left on device' >&2\nexit 1\n")
    fake.chmod(0o755)
    done = []
    updates.run_upgrade(lambda p, s: None, lambda ok, err: done.append((ok, err)),
                        argv=(str(fake), "upgrade")).join(timeout=10)
    assert done and done[0][0] is False
    assert "no space left" in done[0][1]


def test_an_old_bootc_without_progress_fd_is_run_plain(tmp_path):
    fake = tmp_path / "bootc"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in *--progress-fd*) echo \"error: unexpected argument '--progress-fd'\" >&2; exit 2;; esac\n"
        "echo staged\n")
    fake.chmod(0o755)
    seen, done = [], []
    updates.run_upgrade(lambda p, s: seen.append((p, s)),
                        lambda ok, err: done.append((ok, err)),
                        argv=(str(fake), "upgrade")).join(timeout=10)
    assert done == [(True, "")]
    assert (-1, "Updating…") in seen and seen[-1] == (100, "Update ready")
