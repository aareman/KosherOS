"""Per-user app permissions handed to malcontent.

The wildcard-ref bug lived here: blocked apps stayed runnable because
malcontent matches refs exactly. That is pinned below. What an account may
run is what it may install (appaccess.decide): the approved list, or the
whole store minus blocked kinds and blocked apps.
"""

from kosherd.mct import filter_spec, specs_for
from kosherd.policy import Policy, UserPolicy

INSTALLED = [
    ("org.gnome.TwentyFortyEight", "app/org.gnome.TwentyFortyEight/x86_64/stable"),
    ("org.gnome.Chess", "app/org.gnome.Chess/x86_64/stable"),
    ("org.mozilla.firefox", "app/org.mozilla.firefox/x86_64/stable"),
]
INDEX = {
    "org.gnome.TwentyFortyEight": {"ref": "org.gnome.TwentyFortyEight",
                                   "categories": ["Game", "LogicGame"], "rating": {}},
    "org.gnome.Chess": {"ref": "org.gnome.Chess", "categories": ["Game", "BoardGame"],
                        "rating": {}},
    "org.mozilla.firefox": {"ref": "org.mozilla.firefox",
                            "categories": ["Network", "WebBrowser"], "rating": {}},
}
APPROVED = [{"ref": "org.mozilla.firefox", "name": "Firefox",
             "categories": ["Network", "WebBrowser"]},
            {"ref": "org.gnome.Chess", "name": "Chess", "categories": ["Game"]}]


def user(**kwargs) -> UserPolicy:
    return UserPolicy(**{"uid": 1001, "username": "kid", "mode": "whitelist", **kwargs})


def blocked(spec) -> list[str]:
    return [ref.split("/")[1] for ref in spec.blocklist]


# -- installation rights -----------------------------------------------------

def test_managed_users_cannot_install_at_all():
    # This is what stops `flatpak install --user` dodging kosherd.
    spec = filter_spec(user(), INSTALLED, INDEX, APPROVED)
    assert not spec.allow_user_installation
    assert not spec.allow_system_installation


def test_admins_keep_installation_rights():
    spec = filter_spec(user(admin=True), INSTALLED, INDEX, APPROVED)
    assert spec.allow_user_installation and spec.allow_system_installation


# -- approved-only accounts --------------------------------------------------

def test_the_default_account_runs_only_the_approved_list():
    spec = filter_spec(user(), INSTALLED, INDEX, APPROVED)
    assert blocked(spec) == ["org.gnome.TwentyFortyEight"]


def test_an_approved_app_the_index_does_not_know_is_still_allowed():
    # Offline machine, or an app that left Flathub: the approval stands.
    spec = filter_spec(user(), INSTALLED, None, APPROVED)
    assert blocked(spec) == ["org.gnome.TwentyFortyEight"]


def test_blocked_kinds_apply_to_approved_apps_too():
    spec = filter_spec(user(blocked_app_kinds=["games"]), INSTALLED, INDEX, APPROVED)
    assert blocked(spec) == ["org.gnome.TwentyFortyEight", "org.gnome.Chess"]


def test_a_single_blocked_app_is_blocked_whatever_else_says():
    spec = filter_spec(user(blocked_apps=["org.mozilla.firefox"]), INSTALLED, INDEX, APPROVED)
    assert "org.mozilla.firefox" in blocked(spec)


# -- accounts opened to the whole store ---------------------------------------

def test_a_store_account_runs_everything_the_index_knows():
    spec = filter_spec(user(app_access="store"), INSTALLED, INDEX, APPROVED)
    assert spec.blocklist == []


def test_a_store_account_still_loses_blocked_kinds():
    spec = filter_spec(user(app_access="store", blocked_app_kinds=["games"]),
                       INSTALLED, INDEX, APPROVED)
    assert blocked(spec) == ["org.gnome.TwentyFortyEight", "org.gnome.Chess"]


def test_a_store_account_fails_closed_on_an_unknown_unapproved_app():
    # No index yet (the daemon re-applies once it is warm), so the only
    # installed apps a store account may run are the approved ones.
    spec = filter_spec(user(app_access="store"), INSTALLED, None, APPROVED)
    assert blocked(spec) == ["org.gnome.TwentyFortyEight"]


def test_a_store_account_cannot_run_an_installed_app_above_the_ceiling():
    index = {**INDEX, "com.example.Gore": {"ref": "com.example.Gore",
                                           "categories": ["Game"],
                                           "rating": {"violence-bloodshed": "intense"}}}
    installed = [*INSTALLED, ("com.example.Gore", "app/com.example.Gore/x86_64/stable")]
    spec = filter_spec(user(app_access="store"), installed, index, APPROVED)
    assert blocked(spec) == ["com.example.Gore"]


# -- the older allow-list ------------------------------------------------------

def test_the_old_allow_list_blocks_exactly_the_others():
    spec = filter_spec(user(app_access="store", apps=["org.gnome.TwentyFortyEight"]),
                       INSTALLED, INDEX, APPROVED)
    assert spec.blocklist == [
        "app/org.gnome.Chess/x86_64/stable",
        "app/org.mozilla.firefox/x86_64/stable",
    ]


def test_blocklist_uses_exact_refs_not_wildcards():
    # Regression: app/<id>/*/* is silently matched by nothing, so every
    # "blocked" app stayed runnable.
    spec = filter_spec(user(blocked_app_kinds=["games"]), INSTALLED, INDEX, APPROVED)
    assert spec.blocklist
    assert all("*" not in ref for ref in spec.blocklist)
    assert all(ref.count("/") == 3 for ref in spec.blocklist)


def test_admin_restrictions_are_not_enforced_at_run_time():
    # Admins manage the machine; restricting their own launcher would only
    # lock them out of fixing it.
    spec = filter_spec(user(admin=True, blocked_apps=["org.gnome.Chess"]),
                       INSTALLED, INDEX, APPROVED)
    assert spec.blocklist == []


def test_nothing_installed_yields_no_blocklist():
    assert filter_spec(user(), [], INDEX, APPROVED).blocklist == []


# -- across the whole policy -------------------------------------------------

def test_specs_cover_every_effective_user_including_the_guest():
    policy = Policy(users=[user(uid=1000, username="abba", admin=True), user()])
    policy.guest.enabled = True
    policy.guest.uid = 1010
    specs = specs_for(policy, INSTALLED, INDEX, APPROVED)
    assert [s.uid for s in specs] == [1000, 1001, 1010]
    # The guest is never an admin, so it can never install; it runs the
    # approved list like any account in no group.
    guest_spec = specs[-1]
    assert not guest_spec.allow_user_installation
    assert blocked(guest_spec) == ["org.gnome.TwentyFortyEight"]


def test_disabled_guest_gets_no_filter():
    policy = Policy(users=[user()])
    assert [s.uid for s in specs_for(policy, INSTALLED, INDEX, APPROVED)] == [1001]
