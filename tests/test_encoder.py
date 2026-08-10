from __future__ import annotations

from pathlib import Path

from could_not_dispatch.constants import KIND_IMAGE, KIND_VIDEO
from could_not_dispatch.encoder import EncodeOptions, build_command, scale_filter
from could_not_dispatch.media import Media


def _options(**overrides) -> EncodeOptions:
    return EncodeOptions(**{"width": 640, "height": 360, "fps": 5, "video_kbps": 400, **overrides})


def test_the_first_seconds_are_produced_as_fast_as_possible():
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), _options()
    )
    assert "-re" not in command
    assert command[command.index("-readrate") + 1] == "1"
    assert int(command[command.index("-readrate_initial_burst") + 1]) >= 3


def test_the_burst_covers_both_the_megabyte_and_the_encoder_lookahead():
    slow_frames = EncodeOptions.normalized(video_kbps=4000, fps=5)
    assert slow_frames.initial_burst_seconds == 12

    many_frames = EncodeOptions.normalized(video_kbps=4000, fps=25)
    assert many_frames.initial_burst_seconds == 3

    thin = EncodeOptions.normalized(video_kbps=250, fps=25)
    assert thin.initial_burst_seconds == 48

    for kbps in (250, 1000, 2000, 4000, 20000):
        for fps in (1, 5, 25, 60):
            options = EncodeOptions.normalized(video_kbps=kbps, fps=fps)
            burst = options.initial_burst_seconds
            assert burst * options.fps >= 60 or burst == 60, (kbps, fps)
            assert burst * options.video_kbps >= 12000 or burst == 60, (kbps, fps)


def test_image_loops_forever_and_gets_a_silent_audio_track():
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), _options()
    )
    assert command[:4] == ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    assert "-loop" in command and command[command.index("-loop") + 1] == "1"
    assert "-stream_loop" not in command
    assert any(part.startswith("anullsrc=") for part in command)
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "1:a:0" in command
    assert command[-1] == "pipe:1"


def test_the_encoder_keeps_its_lookahead_so_quality_stays_flat():
    for kind, path in ((KIND_IMAGE, "/data/slate.png"), (KIND_VIDEO, "/data/slate.mp4")):
        command = build_command(Media(Path(path), kind, has_audio=False), _options())
        assert "-tune" not in command, kind


def test_video_loops_with_stream_loop_and_regenerates_timestamps():
    command = build_command(
        Media(Path("/data/slate.mp4"), KIND_VIDEO, has_audio=True), _options()
    )
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert command[command.index("-fflags") + 1] == "+genpts"
    assert "-loop" not in command
    assert "0:a:0" in command
    assert not any(part.startswith("anullsrc=") for part in command)


def test_a_silent_video_still_gets_an_audio_track():
    command = build_command(
        Media(Path("/data/slate.mp4"), KIND_VIDEO, has_audio=False), _options()
    )
    assert any(part.startswith("anullsrc=") for part in command)
    assert "1:a:0" in command


def _last_value(command: list[str], flag: str) -> str:
    position = len(command) - 1 - command[::-1].index(flag)
    return command[position + 1]


def test_output_is_mpegts_with_headers_resent_for_mid_stream_joins():
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), _options()
    )
    assert _last_value(command, "-f") == "mpegts"
    flags = command[command.index("-mpegts_flags") + 1]
    assert "+resend_headers" in flags
    assert "+initial_discontinuity" in flags


def test_the_transport_stream_is_padded_to_a_constant_rate():
    options = _options(video_kbps=2000)
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), options
    )
    muxrate = command[command.index("-muxrate") + 1]
    assert muxrate == f"{options.muxrate_kbps}k"
    assert options.muxrate_kbps > options.video_kbps + options.audio_kbps


def test_the_padding_lives_in_the_video_so_it_survives_a_remux():
    options = _options(video_kbps=2000)
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), options
    )
    assert command[command.index("-x264-params") + 1] == "nal-hrd=cbr:filler=1"
    for flag in ("-b:v", "-minrate", "-maxrate"):
        assert command[command.index(flag) + 1] == "2000k", flag


def test_the_keyframe_gets_several_seconds_of_budget_so_gradients_do_not_band():
    options = _options(video_kbps=2000)
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), options
    )
    bufsize = command[command.index("-bufsize") + 1]
    assert bufsize == "8000k"
    assert int(bufsize.rstrip("k")) > options.video_kbps


def test_keyframe_interval_matches_the_frame_rate_so_joins_take_one_second():
    command = build_command(
        Media(Path("/data/slate.png"), KIND_IMAGE, has_audio=False), _options(fps=7)
    )
    assert command[command.index("-g") + 1] == "7"
    assert command[command.index("-r") + 1] == "7"


def test_scale_filter_fits_the_source_inside_the_frame_and_pads_it():
    rendered = scale_filter(_options(width=1280, height=720))
    assert "scale=1280:720:force_original_aspect_ratio=decrease" in rendered
    assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2" in rendered
    assert rendered.endswith("setsar=1")


def test_normalized_options_round_dimensions_down_to_even_numbers():
    options = EncodeOptions.normalized(width=1281, height=721)
    assert options.width == 1280
    assert options.height == 720


def test_normalized_options_clamp_out_of_range_values():
    options = EncodeOptions.normalized(width=1, height=99999, fps=0, video_kbps=-5)
    assert options.width == 128
    assert options.height == 2160
    assert options.fps == 1
    assert options.video_kbps == 64


def test_normalized_options_survive_strings_and_junk():
    options = EncodeOptions.normalized(width="1920", height="1080.0", fps="ten")
    assert (options.width, options.height) == (1920, 1080)
    assert options.fps == 5


def test_the_frame_matches_the_picture_when_no_size_is_given():
    options = EncodeOptions.normalized(source_width=1920, source_height=1080)
    assert (options.width, options.height) == (1920, 1080)


def test_a_picture_larger_than_1080p_is_fitted_keeping_its_shape():
    options = EncodeOptions.normalized(source_width=3840, source_height=2160)
    assert (options.width, options.height) == (1920, 1080)
    tall = EncodeOptions.normalized(source_width=2000, source_height=2000)
    assert (tall.width, tall.height) == (1080, 1080)


def test_a_picture_smaller_than_the_cap_is_kept_as_it_is():
    options = EncodeOptions.normalized(source_width=854, source_height=480)
    assert (options.width, options.height) == (854, 480)


def test_an_explicit_size_still_wins_over_the_picture():
    options = EncodeOptions.normalized(
        width=1280, height=720, source_width=1920, source_height=1080
    )
    assert (options.width, options.height) == (1280, 720)


def test_half_a_size_is_treated_as_no_size_rather_than_a_squashed_frame():
    options = EncodeOptions.normalized(width=1280, source_width=1920, source_height=1080)
    assert (options.width, options.height) == (1920, 1080)


def test_the_frame_falls_back_to_720p_when_nothing_is_known():
    options = EncodeOptions.normalized()
    assert (options.width, options.height) == (1280, 720)
