from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    AUDIO_SAMPLE_RATE,
    AUTO_MAX_HEIGHT,
    AUTO_MAX_WIDTH,
    DEFAULT_AUDIO_KBPS,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_VIDEO_KBPS,
    DEFAULT_WIDTH,
    INITIAL_BURST_KBIT,
    INITIAL_BURST_LOOKAHEAD_FRAMES,
    INITIAL_BURST_MAX_SECONDS,
    INITIAL_BURST_MIN_SECONDS,
    KIND_IMAGE,
    MUX_OVERHEAD,
    VBV_BUFFER_SECONDS,
    X264_CONSTANT_BITRATE,
)
from .media import Media


def _even(value: int, minimum: int, maximum: int) -> int:
    clamped = max(minimum, min(int(value), maximum))
    return clamped - (clamped % 2)


def fit_within(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return max_width, max_height
    if width <= max_width and height <= max_height:
        return width, height
    scale = min(max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def choose_frame(
    width: int, height: int, source_width: int, source_height: int
) -> tuple[int, int]:
    if width > 0 and height > 0:
        return width, height
    if source_width > 0 and source_height > 0:
        return fit_within(source_width, source_height, AUTO_MAX_WIDTH, AUTO_MAX_HEIGHT)
    return DEFAULT_WIDTH, DEFAULT_HEIGHT


@dataclass(frozen=True)
class EncodeOptions:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    video_kbps: int = DEFAULT_VIDEO_KBPS
    audio_kbps: int = DEFAULT_AUDIO_KBPS

    @property
    def muxrate_kbps(self) -> int:
        return int((self.video_kbps + self.audio_kbps) * MUX_OVERHEAD)

    @property
    def initial_burst_seconds(self) -> int:
        for_bytes = -(-INITIAL_BURST_KBIT // self.video_kbps)
        for_lookahead = -(-INITIAL_BURST_LOOKAHEAD_FRAMES // self.fps)
        wanted = max(for_bytes, for_lookahead)
        return max(INITIAL_BURST_MIN_SECONDS, min(wanted, INITIAL_BURST_MAX_SECONDS))

    @classmethod
    def normalized(
        cls,
        width: object = 0,
        height: object = 0,
        fps: object = DEFAULT_FPS,
        video_kbps: object = DEFAULT_VIDEO_KBPS,
        source_width: object = 0,
        source_height: object = 0,
    ) -> EncodeOptions:
        chosen_width, chosen_height = choose_frame(
            _as_int(width, 0),
            _as_int(height, 0),
            _as_int(source_width, 0),
            _as_int(source_height, 0),
        )
        return cls(
            width=_even(chosen_width, 128, 3840),
            height=_even(chosen_height, 128, 2160),
            fps=max(1, min(_as_int(fps, DEFAULT_FPS), 60)),
            video_kbps=max(64, min(_as_int(video_kbps, DEFAULT_VIDEO_KBPS), 20000)),
        )


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return fallback


def scale_filter(options: EncodeOptions) -> str:
    return (
        f"scale={options.width}:{options.height}:force_original_aspect_ratio=decrease,"
        f"pad={options.width}:{options.height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1"
    )


def build_command(media: Media, options: EncodeOptions, executable: str = "ffmpeg") -> list[str]:
    command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin"]
    pacing = [
        "-readrate",
        "1",
        "-readrate_initial_burst",
        str(options.initial_burst_seconds),
    ]

    if media.kind == KIND_IMAGE:
        command += [*pacing, "-loop", "1", "-i", str(media.path)]
    else:
        command += [
            "-fflags",
            "+genpts",
            *pacing,
            "-stream_loop",
            "-1",
            "-i",
            str(media.path),
        ]

    if media.has_audio:
        audio_map = "0:a:0"
    else:
        audio_map = "1:a:0"
        command += [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SAMPLE_RATE}",
        ]

    command += ["-map", "0:v:0", "-map", audio_map]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    command += [
        "-vf",
        scale_filter(options),
        "-r",
        str(options.fps),
        "-g",
        str(options.fps),
        "-b:v",
        f"{options.video_kbps}k",
        "-minrate",
        f"{options.video_kbps}k",
        "-maxrate",
        f"{options.video_kbps}k",
        "-bufsize",
        f"{options.video_kbps * VBV_BUFFER_SECONDS}k",
        "-x264-params",
        X264_CONSTANT_BITRATE,
    ]
    command += [
        "-c:a",
        "aac",
        "-b:a",
        f"{options.audio_kbps}k",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        "2",
    ]
    command += [
        "-f",
        "mpegts",
        "-mpegts_flags",
        "+resend_headers+initial_discontinuity",
        "-muxrate",
        f"{options.muxrate_kbps}k",
        "-muxdelay",
        "0",
        "pipe:1",
    ]
    return command
