"""Polling policy, settings persistence and target selection."""
from copy import deepcopy

import pytest

from manager.controller import Controller


class Supervisor:
    def __init__(self):
        self.saved = None

    def post(self, path, body):
        assert path == '/addons/self/options'
        self.saved = deepcopy(body['options'])


def manager(tmp_path):
    rows = [dict(id='demo', repository='justcop/example', branch='main', path='.',
                 enabled=True, update_on_start=False, health_port=0, health_path='/')]
    return Controller(tmp_path / 'data', tmp_path / 'addons',
                      dict(repositories=rows), Supervisor())


def test_default_polling_and_no_early_checks(tmp_path):
    c = manager(tmp_path)
    calls = []
    c.submit = lambda action: calls.append(action)
    assert c.snapshot()['settings']['automatic_updates'] is True
    assert c.snapshot()['settings']['check_interval'] == 60
    due = c.next_auto_check
    assert not c.automatic_tick(due - 1)
    assert c.automatic_tick(due)
    assert calls == ['automatic']
    assert not c.automatic_tick(due + 59)
    assert c.automatic_tick(due + 60)


@pytest.mark.parametrize('block', ['busy', 'recovery', 'disabled', 'no_targets'])
def test_polling_blocks(tmp_path, block):
    c = manager(tmp_path)
    c.submit = lambda action: pytest.fail('Must not start deployment')
    if block == 'busy':
        c.busy = True
    elif block == 'recovery':
        c.state['transactions']['demo'] = {}
    elif block == 'disabled':
        c.options['automatic_updates'] = False
    else:
        c.targets[0]['enabled'] = False
    assert not c.automatic_tick(c.next_auto_check)


def test_automatic_targets_independent_of_startup_flag(tmp_path):
    c = manager(tmp_path)
    disabled = deepcopy(c.targets[0])
    disabled.update(id='disabled', enabled=False)
    c.targets.append(disabled)
    batches = []
    c.deploy_batch = lambda rows: batches.append(rows)
    c._worker('automatic', None, None)
    assert [t['id'] for t in batches[0]] == ['demo']


def test_settings_persist_and_take_effect_without_restart(tmp_path):
    c = manager(tmp_path)
    c.configure(c.targets, True, False, 15)
    assert c.supervisor.saved['automatic_updates'] is False
    assert c.supervisor.saved['check_interval'] == 15
    assert not c.automatic_tick(c.next_auto_check)
    c.configure(c.targets, True, True, 120)
    assert c.snapshot()['settings']['check_interval'] == 120


@pytest.mark.parametrize('value', [True, 14, 86401, '60'])
def test_invalid_interval_not_persisted(tmp_path, value):
    c = manager(tmp_path)
    with pytest.raises(ValueError):
        c.configure(c.targets, True, True, value)
    assert c.supervisor.saved is None
