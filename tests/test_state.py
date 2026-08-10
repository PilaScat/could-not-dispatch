from __future__ import annotations

from could_not_dispatch import state


def test_missing_file_reads_as_empty(tmp_path):
    assert state.load(tmp_path / "state.json") == {}


def test_a_saved_value_comes_back(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, {"pid": 42, "token": "abc"})
    assert state.load(path) == {"pid": 42, "token": "abc"}


def test_remember_keeps_what_it_was_not_told_about(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, {"pid": 42, "stream_id": 7})
    state.remember(path, {"pid": 43})
    assert state.load(path) == {"pid": 43, "stream_id": 7}


def test_remember_clears_only_the_named_keys(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, {"pid": 42, "token": "abc", "applied": True})
    state.remember(path, {}, clear=["pid", "token"])
    assert state.load(path) == {"applied": True}


def test_the_directory_is_created_on_demand(tmp_path):
    path = tmp_path / "runtime" / "state.json"
    state.save(path, {"applied": True})
    assert state.load(path) == {"applied": True}


def test_rubbish_on_disk_reads_as_empty_rather_than_exploding(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert state.load(path) == {}


def test_a_json_list_is_not_mistaken_for_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert state.load(path) == {}


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, {"pid": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
