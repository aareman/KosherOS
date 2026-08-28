"""Per-user app permissions handed to malcontent.

The wildcard-ref bug lived here: blocked apps stayed runnable because
malcontent matches refs exactly. That is pinned below.
"""

from kosherd.mct import filter_spec, specs_for
from kosherd.policy import Policy, UserPolicy

INSTALLED = [
    ("org.gnome.TwentyFortyEight", "app/org.gnome.TwentyFortyEight/x86_64/stable"),
    ("org.gnome.Chess", "app/org.gnome.Chess/x86_64/stable"),
    ("org.mozilla.firefox", "app/org.mozilla.firefox/x86_64/stable"),
]


def user(**kwargs) -> UserPolicy:
    return UserPolicy(**{"uid": 1001, "username": "kid", "mode": "whitelist", **kwargs})


# -- installation rights -----------------------------------------------------

def test_managed_users_cannot_install_at_all():
    # This is what stops `flatpak install --user` dodging the catalog.
    spec = filter_spec(user(), INSTALLED)
    assert not spec.allow_user_installation
    assert not spec.allow_system_installation


def test_admins_keep_installation_rights():
    spec = filter_spec(user(admin=True), INSTALLED)
    assert spec.allow_user_installation and spec.allow_system_installation


# -- per-user app allow-lists ------------------------------------------------

def test_no_allow_list_means_every_installed_app():
    assert filter_spec(user(apps=[]), INSTALLED).blocklist == []


def test_allow_list_blocks_exactly_the_others():
    spec = filter_spec(user(apps=["org.gnome.TwentyFortyEight"]), INSTALLED)
    assert spec.blocklist == [
        "app/org.gnome.Chess/x86_64/stable",
        "app/org.mozilla.firefox/x86_64/stable",
    ]


def test_blocklist_uses_exact_refs_not_wildcards():
    # Regression: app/<id>/*/* is silently matched by nothing, so every
    # "blocked" app stayed runnable.
    spec = filter_spec(user(apps=["org.gnome.TwentyFortyEight"]), INSTALLED)
    assert all("*" not in ref for ref in spec.blocklist)
    assert all(ref.count("/") == 3 for ref in spec.blocklist)


def test_allowing_an_app_that_is_not_installed_blocks_nothing_extra():
    spec = filter_spec(user(apps=["org.not.Installed"]), INSTALLED)
    assert len(spec.blocklist) == len(INSTALLED)


def test_admin_allow_lists_are_not_enforced():
    # Admins manage the machine; restricting their own launcher would only
    # lock them out of fixing it.
    spec = filter_spec(user(admin=True, apps=["org.gnome.Chess"]), INSTALLED)
    assert spec.blocklist == []


def test_nothing_installed_yields_no_blocklist():
    assert filter_spec(user(apps=["org.gnome.Chess"]), []).blocklist == []


# -- across the whole policy -------------------------------------------------

def test_specs_cover_every_effective_user_including_the_guest():
    policy = Policy(users=[user(uid=1000, username="abba", admin=True), user()])
    policy.guest.enabled = True
    policy.guest.uid = 1010
    specs = specs_for(policy, INSTALLED)
    assert [s.uid for s in specs] == [1000, 1001, 1010]
    # The guest is never an admin, so it can never install.
    guest_spec = specs[-1]
    assert not guest_spec.allow_user_installation


def test_disabled_guest_gets_no_filter():
    policy = Policy(users=[user()])
    assert [s.uid for s in specs_for(policy, INSTALLED)] == [1001]
