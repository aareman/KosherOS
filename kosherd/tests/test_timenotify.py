"""The words on the screen before the time runs out, and who sees them."""

import pytest

from kosherd import timenotify as tn


def test_the_countdown_reads_in_the_second_person():
    summary, body, urgency = tn.message(15, "limit")
    assert summary == "15 minutes left today"
    assert "15 minutes" in body and "save your work" in body.lower()
    assert urgency == tn.URGENCY_NORMAL
    summary, body, urgency = tn.message(5, "limit")
    assert summary == "5 minutes left today" and urgency == tn.URGENCY_CRITICAL
    summary, _body, _u = tn.message(1, "limit")
    assert summary == "1 minute left today"


def test_the_hours_ending_read_differently_from_the_budget_running_out():
    summary, body, _u = tn.message(15, "schedule")
    assert summary == "15 minutes left" and "hours this account may be used" in body
    over, body, urgency = tn.message(0, "schedule")
    assert over == "Your time on the computer is over for now"
    assert "signed out in a minute" in body and urgency == tn.URGENCY_CRITICAL
    over, body, urgency = tn.message(0, "limit")
    assert over == "Your time for today is up" and urgency == tn.URGENCY_CRITICAL


@pytest.mark.parametrize("minutes,reason", [(15, "limit"), (5, "schedule"), (0, "limit"),
                                            (0, "schedule"), (3, "something-new")])
def test_every_warning_has_words_free_of_jargon(minutes, reason):
    summary, body, _u = tn.message(minutes, reason)
    assert summary and body
    for jargon in ("uid", "logind", "pam", "session", "kosherd"):
        assert jargon not in (summary + body).lower(), jargon


def test_only_warnings_about_this_account_are_shown():
    shown = []
    assert tn.handle(1001, 5, "limit", my_uid=1001, notify=lambda *a: shown.append(a))
    assert not tn.handle(1002, 5, "limit", my_uid=1001, notify=lambda *a: shown.append(a))
    assert shown == [tn.message(5, "limit")]


def test_the_notifier_replaces_its_last_notification_and_survives_a_failure():
    sent = []

    def send(app, replaces, icon, summary, body, urgency):
        sent.append((app, replaces, icon, summary, urgency))
        return 42

    notifier = tn.Notifier(send)
    notifier(*tn.message(15, "limit"))
    notifier(*tn.message(5, "limit"))
    assert [s[1] for s in sent] == [0, 42], "the second replaces the first"
    assert sent[0][0] == tn.APP_NAME and sent[0][2] == tn.ICON

    def broken(*args):
        raise RuntimeError("no notification daemon")

    tn.Notifier(broken)(*tn.message(5, "limit"))   # logged, not raised
