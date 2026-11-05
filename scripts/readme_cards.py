from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from could_not_dispatch.constants import IN_USE_TOP_PERCENT
from could_not_dispatch.encoder import Caption, EncodeOptions, caption_filter, filter_value

WIDTH, HEIGHT = 1920, 1080
BACKGROUND = f"color=c=0x07080d:s={WIDTH}x{HEIGHT}:d=1"
HEADING = "font=Sans:fontsize=90:fontcolor=0x7d8086:x=(w-text_w)/2"
OFFLINE_HEADING = "Channel offline"
FULL_HEADING = "No free connections. In use:"
CHANNELS_IN_USE = (
    "Harbour TV · Evening News",
    "Northfield Sports · Cup Final",
    "Kino Classic",
)


def render(target: Path, heading: str, y: str, work: Path, lines: tuple[str, ...] = ()) -> None:
    heading_file = work / f"{target.stem}-heading.txt"
    heading_file.write_text(heading, encoding="utf-8")
    filters = ["vignette", f"drawtext=textfile={filter_value(str(heading_file))}:{HEADING}:y={y}"]
    if lines:
        lines_file = work / f"{target.stem}-lines.txt"
        lines_file.write_text("\n".join(lines), encoding="utf-8")
        options = EncodeOptions.normalized(WIDTH, HEIGHT)
        filters.append(caption_filter(Caption(text_file=lines_file), options))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", BACKGROUND,
         "-vf", ",".join(filters), "-frames:v", "1", str(target)],
        check=True,
    )


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        render(out / "card-offline.png", OFFLINE_HEADING, "(h-text_h)/2", Path(work))
        top = f"h*{IN_USE_TOP_PERCENT / 100}-text_h-60"
        render(out / "card-full.png", FULL_HEADING, top, Path(work), CHANNELS_IN_USE)


if __name__ == "__main__":
    main(sys.argv[1])
