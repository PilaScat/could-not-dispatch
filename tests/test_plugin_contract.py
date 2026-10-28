from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_dispatcharr_plugin_could_not_dispatch"

ALLOWED_FIELD_TYPES = {"boolean", "number", "string", "text", "select", "info"}
PLUGIN_HOOK_EVENTS = {
    "channel_start",
    "channel_stop",
    "channel_reconnect",
    "channel_error",
    "channel_buffering",
    "channel_failover",
    "stream_switch",
    "recording_start",
    "recording_end",
    "epg_refresh",
    "epg_error",
    "m3u_refresh",
    "m3u_error",
    "client_connect",
    "client_disconnect",
    "login_failed",
    "epg_blocked",
    "m3u_blocked",
    "vod_start",
    "vod_stop",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    with (ROOT / "plugin.json").open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def loaded_module():
    namespace = types.ModuleType(PACKAGE_NAME)
    namespace.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    namespace.__package__ = PACKAGE_NAME
    sys.modules[PACKAGE_NAME] = namespace

    module_name = f"{PACKAGE_NAME}.plugin"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module

    for name in list(sys.modules):
        if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[name]


@pytest.fixture
def isolated_runtime(loaded_module, tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    for name in ("STATE_PATH", "JOURNAL_PATH", "LOG_PATH", "MEDIA_CACHE_DIR"):
        if hasattr(loaded_module, name):
            monkeypatch.setattr(loaded_module, name, runtime / getattr(loaded_module, name).name)
    monkeypatch.setattr(loaded_module, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(loaded_module.process, "terminate_strays", lambda *_a, **_k: [])
    if hasattr(loaded_module.process, "find_servers"):
        monkeypatch.setattr(loaded_module.process, "find_servers", lambda *_a, **_k: [])
    return runtime


def test_the_loader_import_path_finds_a_plugin_class(loaded_module):
    assert hasattr(loaded_module, "Plugin")


def test_importing_the_plugin_does_not_need_django(loaded_module):
    assert "django" not in sys.modules
    assert not any(name.startswith("apps.") for name in sys.modules)


def test_the_instance_reports_the_manifest_metadata(loaded_module, manifest):
    plugin = loaded_module.Plugin()
    assert plugin.name == manifest["name"]
    assert plugin.version == manifest["version"]
    assert plugin.description == manifest["description"]
    assert plugin.author == manifest["author"]
    assert plugin.fields == manifest["fields"]
    assert plugin.actions == manifest["actions"]


def test_every_manifest_action_has_a_handler(loaded_module, manifest, isolated_runtime):
    plugin = loaded_module.Plugin()
    for action in manifest["actions"]:
        result = plugin.run(action["id"], {}, {"settings": {}})
        assert not str(result.get("message", "")).startswith("Unknown action")


def test_an_unknown_action_is_refused_without_touching_the_database(
    loaded_module, isolated_runtime
):
    result = loaded_module.Plugin().run("nope", {}, {"settings": {}})
    assert result["status"] == "error"
    assert "Unknown action" in result["message"]


def test_manifest_fields_use_only_types_dispatcharr_can_render(manifest):
    for field in manifest["fields"]:
        assert field["type"] in ALLOWED_FIELD_TYPES, field["id"]


def test_manifest_field_ids_are_unique(manifest):
    ids = [field["id"] for field in manifest["fields"]]
    assert len(ids) == len(set(ids))


def test_the_action_buttons_look_like_one_set(manifest):
    actions = manifest["actions"]
    variants = [action.get("button_variant") for action in actions]
    assert variants.count("filled") == 1
    assert set(variants) <= {"filled", "default"}
    assert all(action.get("button_label") for action in actions)
    assert max(len(a["button_label"]) for a in actions) <= 8


def test_the_destructive_action_is_last_coloured_and_confirmed(manifest):
    last = manifest["actions"][-1]
    assert last["id"] == "remove"
    assert last["button_color"] == "red"
    assert last["confirm"]["required"] is True
    assert [a for a in manifest["actions"] if a.get("button_color")] == [last]


def test_action_labels_and_descriptions_stay_short_and_parallel(manifest):
    for action in manifest["actions"]:
        assert len(action["label"].split()) <= 3, action["id"]
        description = action["description"]
        assert description.endswith("."), action["id"]
        assert description.count(".") == 1, action["id"]
        assert len(description.split()) <= 13, action["id"]


def test_an_action_does_not_repeat_what_its_event_chip_already_says(manifest):
    for action in manifest["actions"]:
        if action.get("events"):
            assert "runs by itself" not in action["description"].lower(), action["id"]


def test_manifest_binds_only_events_that_reach_plugin_hooks(manifest):
    for action in manifest["actions"]:
        for event in action.get("events", []):
            assert event in PLUGIN_HOOK_EVENTS, event


def test_manifest_carries_what_the_registry_requires(manifest):
    for key in ("name", "version", "description", "author", "license"):
        assert manifest.get(key), key
    assert manifest["license"] == "MIT"
    assert manifest["source_type"] == "external"
    assert "{version}" in manifest["source_url"]


def test_manifest_version_matches_the_package_constant(manifest):
    from could_not_dispatch.constants import PLUGIN_VERSION

    assert manifest["version"] == PLUGIN_VERSION


def test_runtime_state_is_not_kept_in_the_plugin_settings():
    source = (ROOT / "plugin.py").read_text(encoding="utf-8")
    assert "runtime_" not in source
    assert "PluginConfig" not in source
    assert "STATE_PATH" in source


def test_the_state_file_lives_beside_the_plugin(loaded_module):
    assert loaded_module.STATE_PATH.parent == loaded_module.RUNTIME_DIR
    assert loaded_module.RUNTIME_DIR.parent == ROOT


@pytest.fixture
def covering(loaded_module, isolated_runtime, monkeypatch):
    covered: list[list[int]] = []

    def attach(name, url, channel_ids):
        covered.append(list(channel_ids))
        return loaded_module.targeting.Attachment(attached=len(channel_ids), separated=0, created=0)

    monkeypatch.setattr(loaded_module.Plugin, "_state", lambda self: {"applied": True})
    monkeypatch.setattr(loaded_module.targeting, "target_channel_ids", lambda *_: [7, 8])
    monkeypatch.setattr(loaded_module.targeting, "attach", attach)
    return covered


def test_cover_pressed_by_hand_covers_even_with_automatic_cover_off(loaded_module, covering):
    settings = {"auto_reapply": False}
    result = loaded_module.Plugin().run("reapply", {}, {"settings": settings})
    assert result["status"] == "ok"
    assert covering == [[7, 8]]


def test_an_m3u_refresh_covers_nothing_with_automatic_cover_off(loaded_module, covering):
    params = {"event": "m3u_refresh", "payload": {}}
    result = loaded_module.Plugin().run("reapply", params, {"settings": {"auto_reapply": False}})
    assert result["message"] == "Covering new channels is switched off."
    assert covering == []


def test_an_m3u_refresh_covers_new_channels_by_default(loaded_module, covering):
    params = {"event": "m3u_refresh", "payload": {}}
    loaded_module.Plugin().run("reapply", params, {"settings": {}})
    assert covering == [[7, 8]]


def test_settings_read_by_the_code_are_declared_in_the_manifest(manifest):
    declared = {field["id"] for field in manifest["fields"]}
    used = {
        "media_source",
        "media_source_full",
        "port",
        "width",
        "height",
        "fps",
        "video_kbps",
        "exclude_groups",
        "exclude_channels",
        "auto_reapply",
        "api_key",
        "show_in_use",
        "in_use_font",
        "in_use_top",
    }
    assert used <= declared


def test_the_channels_in_use_are_off_by_default(manifest):
    field = next(field for field in manifest["fields"] if field["id"] == "show_in_use")
    assert field["type"] == "boolean"
    assert field["default"] is False


def _started_arguments(loaded_module, monkeypatch, full, caption=None) -> list[str]:
    spawned: list[list[str]] = []

    def spawn(_base, arguments, *_args, **_kwargs):
        spawned.append(arguments)
        return 4242

    monkeypatch.setattr(loaded_module.process, "port_is_free", lambda *_a: True)
    monkeypatch.setattr(loaded_module.process, "spawn", spawn)
    monkeypatch.setattr(loaded_module.Plugin, "_wait_for_port", lambda self, port: True)
    card = loaded_module.media_module.Media(Path("/data/card.png"), "image", has_audio=False)
    options = loaded_module.EncodeOptions.normalized()
    loaded_module.Plugin()._start_process(card, options, 9721, "key", full, caption)
    return spawned[0]


def test_the_card_for_a_full_provider_reaches_the_server(
    loaded_module, isolated_runtime, monkeypatch
):
    full = loaded_module.media_module.Media(Path("/data/full.mp4"), "video", has_audio=True)
    arguments = _started_arguments(loaded_module, monkeypatch, full)
    assert arguments[arguments.index("--full-media") + 1] == str(Path("/data/full.mp4"))
    assert arguments[arguments.index("--full-kind") + 1] == "video"
    assert "--full-has-audio" in arguments


def test_without_a_card_for_a_full_provider_the_server_gets_none(
    loaded_module, isolated_runtime, monkeypatch
):
    arguments = _started_arguments(loaded_module, monkeypatch, None)
    assert "--full-media" not in arguments


def test_with_the_channels_in_use_off_the_server_writes_none(loaded_module, isolated_runtime):
    assert loaded_module.Plugin()._caption({}) is None
    assert loaded_module.Plugin()._caption({"show_in_use": False}) is None


def test_the_channels_in_use_reach_the_server_with_the_card_for_a_full_provider(
    loaded_module, isolated_runtime, monkeypatch
):
    caption = loaded_module.Plugin()._caption({"show_in_use": True})
    assert caption.text_file == isolated_runtime / "in-use.txt"
    assert caption.font is None
    full = loaded_module.media_module.Media(Path("/data/full.png"), "image", has_audio=False)
    arguments = _started_arguments(loaded_module, monkeypatch, full, caption)
    assert arguments[arguments.index("--in-use-file") + 1] == str(caption.text_file)
    assert "--in-use-font" not in arguments


def test_without_a_card_for_a_full_provider_the_channels_in_use_go_nowhere(
    loaded_module, isolated_runtime, monkeypatch
):
    caption = loaded_module.Plugin()._caption({"show_in_use": True})
    arguments = _started_arguments(loaded_module, monkeypatch, None, caption)
    assert "--in-use-file" not in arguments


def test_a_font_for_the_channels_in_use_is_checked_like_the_card(
    loaded_module, isolated_runtime, monkeypatch, tmp_path
):
    font, picture = tmp_path / "sky.ttf", tmp_path / "card.png"
    font.write_bytes(b"font")
    picture.write_bytes(b"png")
    real = loaded_module.media_module.validate_local_path
    monkeypatch.setattr(
        loaded_module.media_module,
        "validate_local_path",
        lambda source: real(source, allowed_roots=[tmp_path]),
    )
    plugin = loaded_module.Plugin()
    caption = plugin._caption({"show_in_use": True, "in_use_font": str(font)})
    assert caption.font == font.resolve()
    with pytest.raises(loaded_module.media_module.MediaError, match="font"):
        plugin._caption({"show_in_use": True, "in_use_font": str(picture)})
    with pytest.raises(loaded_module.media_module.MediaError, match="outside"):
        plugin._caption({"show_in_use": True, "in_use_font": str(tmp_path.parent / "x.ttf")})


def test_switching_the_channels_in_use_restarts_the_fallback(loaded_module, isolated_runtime):
    plugin = loaded_module.Plugin()
    card = loaded_module.media_module.Media(Path("/data/card.png"), "image", has_audio=False)
    options = loaded_module.EncodeOptions.normalized()
    caption = plugin._caption({"show_in_use": True})
    assert plugin._signature(card, options, 9721, True, card) != plugin._signature(
        card, options, 9721, True, card, caption
    )


def test_setting_a_card_for_a_full_provider_restarts_the_fallback(loaded_module):
    plugin = loaded_module.Plugin()
    card = loaded_module.media_module.Media(Path("/data/card.png"), "image", has_audio=False)
    full = loaded_module.media_module.Media(Path("/data/full.png"), "image", has_audio=False)
    options = loaded_module.EncodeOptions.normalized()
    assert plugin._signature(card, options, 9721, True) != plugin._signature(
        card, options, 9721, True, full
    )


def test_the_height_of_the_channels_in_use_defaults_to_the_operator_card(manifest):
    field = next(field for field in manifest["fields"] if field["id"] == "in_use_top")
    assert field["type"] == "number"
    assert field["default"] == 58.5


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, 58.5), ("", 58.5), ("40", 40.0), (12.5, 12.5), (-3, 0.0), (120, 95.0), ("nan", 58.5)],
)
def test_the_height_of_the_channels_in_use_is_read_within_its_range(
    loaded_module, isolated_runtime, given, expected
):
    caption = loaded_module.Plugin()._caption({"show_in_use": True, "in_use_top": given})
    assert caption.top_percent == expected


def test_the_height_of_the_channels_in_use_reaches_the_server_and_restarts_it(
    loaded_module, isolated_runtime, monkeypatch
):
    plugin = loaded_module.Plugin()
    caption = plugin._caption({"show_in_use": True, "in_use_top": 40})
    full = loaded_module.media_module.Media(Path("/data/full.png"), "image", has_audio=False)
    arguments = _started_arguments(loaded_module, monkeypatch, full, caption)
    assert arguments[arguments.index("--in-use-top") + 1] == "40.0"
    card = loaded_module.media_module.Media(Path("/data/card.png"), "image", has_audio=False)
    options = loaded_module.EncodeOptions.normalized()
    assert plugin._signature(card, options, 9721, True, full, caption) != plugin._signature(
        card, options, 9721, True, full, plugin._caption({"show_in_use": True})
    )
