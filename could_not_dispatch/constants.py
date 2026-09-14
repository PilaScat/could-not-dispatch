from __future__ import annotations

from pathlib import Path

PLUGIN_NAME = "Could Not Dispatch"
PLUGIN_VERSION = "0.3.2"
PLUGIN_DESCRIPTION = (
    "Plays a looping image or video when every real stream on a channel has failed, "
    "so viewers see a message instead of a black screen. With an API key, it later sends "
    "the channel back to its first stream."
)

STREAM_NAME = "Could Not Dispatch"
SERVER_MODULE = "could_not_dispatch.server"
STREAM_PATH = "/slate.ts"
HEALTH_PATH = "/healthz"
LISTEN_HOST = "127.0.0.1"

RUN_TOKEN_ENV = "COULD_NOT_DISPATCH_RUN_TOKEN"
API_KEY_ENV = "COULD_NOT_DISPATCH_API_KEY"

DISPATCHARR_URL = "http://127.0.0.1:9191"
API_TIMEOUT_SECONDS = 10.0
RECOVERY_POLL_SECONDS = 10.0
RECOVERY_WAIT_SECONDS = (120.0, 240.0, 480.0, 900.0)
RECOVERY_SETTLED_SECONDS = 900.0
CHAINS_REFRESH_SECONDS = 900.0

ALLOWED_MEDIA_ROOTS = (Path("/data"), Path("/app/data"))
MEDIA_DOWNLOAD_TIMEOUT = (5, 30)
MEDIA_MAX_BYTES = 512 * 1024 * 1024

DOWNLOAD_HEADERS = {
    "User-Agent": f"could-not-dispatch/{PLUGIN_VERSION}",
    "Accept": "image/*,video/*,*/*;q=0.8",
}

DEFAULT_PORT = 9721
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
AUTO_MAX_WIDTH = 1920
AUTO_MAX_HEIGHT = 1080
DEFAULT_FPS = 5
DEFAULT_VIDEO_KBPS = 2000
DEFAULT_AUDIO_KBPS = 64
AUDIO_SAMPLE_RATE = 48000
MUX_OVERHEAD = 1.15
VBV_BUFFER_SECONDS = 4
INITIAL_BURST_KBIT = 12000
INITIAL_BURST_LOOKAHEAD_FRAMES = 60
INITIAL_BURST_MIN_SECONDS = 3
INITIAL_BURST_MAX_SECONDS = 60

CHUNK_SIZE = 65536
SUBSCRIBER_QUEUE_CHUNKS = 256
PRIME_BUFFER_BYTES = 4 * 1024 * 1024
PRIME_TARGET_BYTES = 1536 * 1024
IDLE_SHUTDOWN_SECONDS = 15.0
ENCODER_RESTART_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
TERMINATE_GRACE_SECONDS = 10.0
STARTUP_PROBE_SECONDS = 8.0

KIND_IMAGE = "image"
KIND_VIDEO = "video"

X264_CONSTANT_BITRATE = "nal-hrd=cbr:filler=1"

IMAGE_FORMAT_NAMES = frozenset(
    {
        "image2",
        "png_pipe",
        "jpeg_pipe",
        "mjpeg",
        "webp_pipe",
        "bmp_pipe",
        "tiff_pipe",
        "svg_pipe",
    }
)
