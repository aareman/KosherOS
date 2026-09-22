"""Which apps an account may have: the approved list or the whole store,
minus blocked kinds, blocked apps, and anything above the content ceiling."""

import pytest

from kosherd import appaccess
from kosherd.policy import UserPolicy

FIREFOX = {"ref": "org.mozilla.firefox", "name": "Firefox",
           "categories": ["Network", "WebBrowser"], "rating": {}}
CHESS = {"ref": "org.gnome.Chess", "name": "Chess", "categories": ["Game", "BoardGame"],
         "rating": {}}
KART = {"ref": "net.supertuxkart.SuperTuxKart", "name": "SuperTuxKart",
        "categories": ["Game"], "rating": {"violence-cartoon": "mild"}}
GORE = {"ref": "com.example.Gore", "name": "Gore", "categories": ["Game"],
        "rating": {"violence-bloodshed": "intense", "language-profanity": "moderate"}}
DATING = {"ref": "com.example.Dating", "name": "Dating", "categories": ["Network"],
          "rating": {"sex-themes": "mild"}}
TOR = {"ref": "org.torproject.torbrowser-launcher", "name": "Tor Browser Launcher",
       "categories": ["Network", "WebBrowser"], "rating": {}}
UNRATED = {"ref": "org.example.Quiet", "name": "Quiet", "categories": ["Utility"]}
INDEX = [FIREFOX, CHESS, KART, GORE, DATING, TOR, UNRATED]
APPROVED = [{"ref": "org.mozilla.firefox", "name": "Firefox",
             "categories": ["Network", "WebBrowser"]}]
APPROVED_REFS = {"org.mozilla.firefox"}


def user(**kwargs) -> UserPolicy:
    return UserPolicy(**{"uid": 1001, "username": "kid", "mode": "filtered", **kwargs})


# -- the default -------------------------------------------------------------

def test_a_new_account_gets_the_approved_list_and_an_admin_the_store():
    assert appaccess.access_of(user()) == "approved"
    assert appaccess.access_of(user(admin=True)) == "store"
    assert appaccess.access_of(user(admin=True, app_access="approved")) == "approved"


def test_approved_only_refuses_everything_off_the_list():
    assert appaccess.decide(user(), FIREFOX, APPROVED_REFS) is None
    assert appaccess.decide(user(), CHESS, APPROVED_REFS) == "not-approved"


# -- blocks apply in either mode ---------------------------------------------

@pytest.mark.parametrize("access", ["approved", "store"])
def test_a_blocked_kind_beats_an_approval(access):
    account = user(app_access=access, blocked_app_kinds=["internet"])
    assert appaccess.decide(account, FIREFOX, APPROVED_REFS) == "kind"


@pytest.mark.parametrize("access", ["approved", "store"])
def test_a_blocked_app_beats_everything(access):
    account = user(app_access=access, blocked_apps=["org.mozilla.firefox"])
    assert appaccess.decide(account, FIREFOX, APPROVED_REFS) == "blocked"


# -- the whole store -----------------------------------------------------------

def test_the_store_account_gets_what_is_below_the_ceiling():
    account = user(app_access="store")
    assert appaccess.decide(account, CHESS, APPROVED_REFS) is None
    assert appaccess.decide(account, KART, APPROVED_REFS) is None
    assert appaccess.decide(account, UNRATED, APPROVED_REFS) is None


def test_the_ceiling_refuses_violence_and_sexual_themes():
    account = user(app_access="store")
    assert appaccess.decide(account, GORE, APPROVED_REFS) == "content"
    assert appaccess.decide(account, DATING, APPROVED_REFS) == "content"


def test_an_approval_overrides_the_ceiling_and_the_circumvention_list():
    account = user(app_access="store")
    approved = APPROVED_REFS | {GORE["ref"], TOR["ref"]}
    assert appaccess.decide(account, GORE, approved) is None
    assert appaccess.decide(account, TOR, approved) is None


def test_circumvention_tools_are_refused_by_name():
    assert appaccess.decide(user(app_access="store"), TOR, APPROVED_REFS) == "circumvention"


def test_an_unknown_app_fails_closed():
    # The machine has no index entry for it: nothing to judge, so no.
    bare = {"ref": "org.example.Unknown"}
    assert appaccess.decide(user(app_access="store"), bare, APPROVED_REFS) == "unknown"
    assert appaccess.decide(user(), bare, APPROVED_REFS) == "not-approved"


def test_admins_are_judged_at_install_time_too():
    # The parent is not who the ceiling is for, but the ceiling is the
    # computer's; an admin who wants an app above it approves it by name.
    assert appaccess.decide(user(admin=True), GORE, APPROVED_REFS) == "content"
    assert appaccess.decide(user(admin=True), CHESS, APPROVED_REFS) is None


# -- the ceiling itself ----------------------------------------------------------

def test_every_ceiling_attribute_has_words_and_a_known_intensity():
    for attribute, limit in appaccess.CEILING.items():
        assert limit in appaccess.INTENSITY
        assert attribute in appaccess.ATTRIBUTE_WORDS


def test_the_ceiling_is_strict_where_a_frum_family_expects_it():
    for attribute in ("sex-nudity", "sex-themes", "language-profanity",
                      "money-gambling", "violence-bloodshed", "drugs-narcotics"):
        assert appaccess.CEILING[attribute] == "none"


def test_content_words_read_as_a_sentence():
    assert appaccess.content_words(GORE) == "graphic violence and bad language"
    assert appaccess.content_words(CHESS) == ""
    assert "graphic violence" in appaccess.explain("content", GORE)
    assert appaccess.explain("kind", CHESS).startswith("Chess is ")


def test_unlisted_attributes_are_not_judged():
    chat = {"ref": "org.example.Chat", "categories": ["Network"],
            "rating": {"social-chat": "intense", "money-purchasing": "intense"}}
    assert appaccess.content_reasons(chat) == []


# -- what the Store shows ----------------------------------------------------------

def test_visible_for_an_approved_only_account_is_the_approved_list():
    shown = appaccess.visible(user(), INDEX, APPROVED)
    assert [a["ref"] for a in shown] == ["org.mozilla.firefox"]
    assert shown[0]["kind"] == "internet" and shown[0]["approved"]


def test_visible_for_a_store_account_is_the_index_minus_refusals():
    shown = {a["ref"] for a in appaccess.visible(user(app_access="store"), INDEX, APPROVED)}
    assert shown == {FIREFOX["ref"], CHESS["ref"], KART["ref"], UNRATED["ref"]}


def test_visible_keeps_approved_apps_the_index_does_not_have():
    approved = [*APPROVED, {"ref": "org.example.Gone", "name": "Gone",
                            "categories": ["Office"]}]
    shown = {a["ref"]: a for a in appaccess.visible(user(app_access="store"), INDEX, approved)}
    assert "org.example.Gone" in shown
    assert shown["org.example.Gone"]["kind"] == "work"


def test_visible_prefers_the_index_entry_over_the_approved_entry():
    approved = [{"ref": "org.gnome.Chess", "name": "Chess (approved)", "categories": ["Game"]}]
    shown = {a["ref"]: a for a in appaccess.visible(user(), INDEX, approved)}
    assert shown["org.gnome.Chess"]["name"] == "Chess"          # from the index
    assert shown["org.gnome.Chess"]["categories"] == ["Game", "BoardGame"]


def test_visible_drops_blocked_kinds_for_a_store_account():
    shown = {a["ref"] for a in appaccess.visible(
        user(app_access="store", blocked_app_kinds=["games"]), INDEX, APPROVED)}
    assert shown == {FIREFOX["ref"], UNRATED["ref"]}
