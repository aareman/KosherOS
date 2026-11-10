"""Direct stream revocation must precede both established shortcuts."""
import pytest
from kosherd import nft
from kosherd.policy import Policy, UserPolicy


def test_revision_marks_cover_unfiltered_accounts_and_container_ids():
    p = Policy(revision=8, users=[UserPolicy(uid=1001, username='kid', mode='unfiltered')])
    rules = nft.render(p, dns_uid=989, subids={1001: [(100000, 165535)]})
    output = rules.split('chain output {')[1].split('chain input {')[0]
    incoming = rules.split('chain input {')[1].split('chain local_services {')[0]
    stale = f'ct label {nft.DIRECT_LABEL} ct mark != 9'
    assert output.index(stale + ' reject') < output.index('ct state new ct mark set 9')
    assert output.index(stale) < output.index('ct state established,related accept')
    assert incoming.index(stale + ' drop') < incoming.index('ct state established,related accept')
    assert 'meta skuid { 1001, 100000-165535 } ct state new' in output
    # Local/proxied streams are not invalidated by every visual policy edit.
    assert output.index('oif "lo" accept') < output.index('ct state new ct mark set 9')


def test_revision_marks_never_wrap_and_reauthorize_old_streams():
    with pytest.raises(ValueError, match='revision'):
        nft.render(Policy(revision=2**32-1), dns_uid=989)
