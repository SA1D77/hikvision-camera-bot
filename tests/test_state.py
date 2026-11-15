import json

from hikcamerabot.config.state import StateStore
from tests.conftest import requires_config


def test_state_store_roundtrip(tmp_path):
    store = StateStore(tmp_path / 'state.json')
    assert store.load() == {}

    store.save({'panels': {'1': 11}, 'ir_modes': {'cam_1': 'night'}})

    assert store.load() == {'panels': {'1': 11}, 'ir_modes': {'cam_1': 'night'}}
    assert not (tmp_path / 'state.json.tmp').exists()


def test_state_store_ignores_broken_file(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{broken', encoding='utf-8')
    assert StateStore(path).load() == {}

    path.write_text(json.dumps(['not', 'a', 'dict']), encoding='utf-8')
    assert StateStore(path).load() == {}


def test_state_store_save_error_is_not_fatal(tmp_path):
    StateStore(tmp_path / 'missing_dir' / 'state.json').save({'panels': {}})


@requires_config
def test_menu_restores_only_valid_state():
    from hikcamerabot import menu

    state = {
        'panels': {'1': 11, 'oops': 5, '2': None},
        'ir_modes': {'cam_1': 'night', 'cam_2': 'unknown'},
    }
    assert menu._load_panels(state) == {1: 11}
    assert menu._load_ir_modes(state) == {'cam_1': 'night'}
    assert menu._load_panels({'panels': 'broken'}) == {}
    assert menu._load_ir_modes({}) == {}


@requires_config
async def test_menu_saves_state(tmp_path, monkeypatch):
    from hikcamerabot import menu

    store = StateStore(tmp_path / 'state.json')
    monkeypatch.setattr(menu, '_state_store', store)
    monkeypatch.setattr(menu, '_panels', {1: 11})
    monkeypatch.setattr(menu, '_ir_modes', {'cam_1': 'auto'})

    await menu._save_state()

    assert store.load() == {'panels': {'1': 11}, 'ir_modes': {'cam_1': 'auto'}}
