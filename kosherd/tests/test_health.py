"""kosherctl system health: why a system service failed, without sudo (#337)."""
import json

from kosherd import access, cli, health

LISTED = ("bootloader-update.service loaded failed failed Update bootloader on boot\n"
          "nix-gc.timer              loaded failed failed Nix collector\n")
SHOWN = ("Description=Update bootloader on boot\nResult=exit-code\nExecMainStatus=1\n"
         "ExecMainExitTimestamp=Mon 2026-11-23 17:06:06 EST\n"
         "InactiveEnterTimestamp=Mon 2026-11-23 17:06:06 EST\nNRestarts=0\n")


def runner(journal_now="", journal_ever=""):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:2] == ["systemctl", "list-units"]:
            return LISTED
        if argv[:2] == ["systemctl", "show"]:
            return SHOWN
        if argv[0] == "journalctl":
            return journal_now if "-b" in argv else journal_ever
        raise AssertionError(argv)
    return run, calls


def test_each_failed_unit_comes_with_how_it_stopped_and_its_last_lines():
    run, calls = runner(journal_now="2026-11-23T17:06:06-0500 host bootupctl[811]: error: x\n")
    found = health.report(run)["failed"]
    assert [u["unit"] for u in found] == ["bootloader-update.service", "nix-gc.timer"]
    first = found[0]
    assert first["result"] == "exit-code" and first["status"] == "1"
    assert first["failed_at"] == "Mon 2026-11-23 17:06:06 EST"
    assert first["log"] == ["2026-11-23T17:06:06-0500 host bootupctl[811]: error: x"]
    journal = [c for c in calls if c[0] == "journalctl"][0]
    assert ["-u", "bootloader-update.service"] == journal[journal.index("-u"):journal.index("-u") + 2]
    assert str(health.LOG_LINES) in journal


def test_a_unit_silent_this_boot_shows_its_last_lines_from_before():
    run, _ = runner(journal_now="-- No entries --\n", journal_ever="earlier: error: y\n")
    assert health.describe("x.service", run)["log"] == ["earlier: error: y"]


def test_long_lines_and_many_units_are_cut_to_size():
    many = "".join(f"u{i}.service loaded failed failed U\n" for i in range(50))
    assert len(health.failed_units(lambda argv: many)) == health.MAX_UNITS
    run, _ = runner(journal_now="x" * 5000 + "\n")
    assert len(health.describe("x.service", run)["log"][0]) == health.MAX_LINE


def test_a_machine_with_nothing_failed_says_so():
    assert health.report(lambda argv: "") == {"failed": []}
    assert cli.format_health({"failed": []}) == "no system service has failed\n"


def test_the_report_reads_like_a_person_wrote_it():
    run, _ = runner(journal_now="bootupctl: error: something\n")
    text = cli.format_health(json.loads(json.dumps(health.report(run))))
    assert text.startswith("bootloader-update.service (Update bootloader on boot)\n"
                           "  exit-code, exit status 1, at Mon 2026-11-23 17:06:06 EST\n"
                           "  last lines it logged:\n"
                           "    bootupctl: error: something\n")


def test_only_an_administrator_may_read_it():
    assert access.ACTIONS["SystemHealth"] == access.ACTION_READ_CONFIG
