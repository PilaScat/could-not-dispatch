from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from could_not_dispatch.caption import InUseCaption, InUseFile, line
from could_not_dispatch.constants import IN_USE_EMPTY_TEXT
from could_not_dispatch.recovery import ApiError, InUse, Program

WEB4 = InUse(uuid="uuid-web4", name="DAZN | Web 4")
GOL = InUse(uuid="uuid-gol", name="DAZN | Diretta Gol Serie A")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Guide:
    def __init__(self, programs: dict[str, Program] | None = None) -> None:
        self.programs = programs or {}
        self.asked: list[list[str]] = []
        self.fails = False

    def __call__(self, uuids: Sequence[str]) -> dict[str, Program]:
        self.asked.append(list(uuids))
        if self.fails:
            raise ApiError("POST /api/epg/current-programs/ failed: timed out")
        return {uuid: self.programs[uuid] for uuid in uuids if uuid in self.programs}


def build(tmp_path: Path, guide: Guide | None) -> tuple[InUseCaption, Path, Clock, list[str]]:
    path, clock, lines = tmp_path / "in-use.txt", Clock(), []
    caption = InUseCaption(InUseFile(path), guide, lines.append, clock=clock)
    return caption, path, clock, lines


def test_a_line_is_the_channel_then_its_programme():
    assert line("DAZN | Web 4", Program("Genoa - Juventus", 0)) == "DAZN | Web 4 · Genoa - Juventus"
    assert line("DAZN | Web 4", None) == "DAZN | Web 4"


def test_a_programme_that_repeats_the_channel_name_is_left_out():
    assert line(GOL.name, Program("dazn |  diretta gol serie a", 0)) == GOL.name


def test_the_file_exists_before_anyone_is_listed_so_the_encoder_can_open_it(tmp_path):
    _, path, _, _ = build(tmp_path, Guide())
    assert path.read_text(encoding="utf-8") == IN_USE_EMPTY_TEXT


def test_the_same_text_is_not_written_twice(tmp_path):
    file = InUseFile(tmp_path / "in-use.txt")
    assert file.write(["DAZN | Web 4"]) is True
    assert file.write(["DAZN | Web 4"]) is False
    assert file.write([]) is True
    assert sorted(item.name for item in tmp_path.iterdir()) == ["in-use.txt"]


def test_the_guide_is_not_asked_while_nobody_holds_a_connection(tmp_path):
    guide = Guide()
    caption, path, _, _ = build(tmp_path, guide)
    caption.update([])
    assert guide.asked == []
    assert path.read_text(encoding="utf-8") == IN_USE_EMPTY_TEXT


def test_a_programme_still_on_is_not_asked_again(tmp_path):
    guide = Guide({WEB4.uuid: Program("Genoa - Juventus", 2000.0)})
    caption, path, clock, _ = build(tmp_path, guide)
    caption.update([WEB4])
    clock.now = 1999.0
    caption.update([WEB4])
    assert guide.asked == [[WEB4.uuid]]
    assert path.read_text(encoding="utf-8") == "DAZN | Web 4 · Genoa - Juventus"


def test_the_next_programme_is_asked_once_the_current_one_has_ended(tmp_path):
    guide = Guide({WEB4.uuid: Program("Genoa - Juventus", 2000.0)})
    caption, path, clock, _ = build(tmp_path, guide)
    caption.update([WEB4])
    guide.programs[WEB4.uuid] = Program("Serie A Show", 5000.0)
    clock.now = 2000.0
    caption.update([WEB4])
    assert guide.asked == [[WEB4.uuid], [WEB4.uuid]]
    assert path.read_text(encoding="utf-8") == "DAZN | Web 4 · Serie A Show"


def test_a_channel_without_a_guide_shows_its_name_and_is_asked_again_a_minute_later(tmp_path):
    guide = Guide()
    caption, path, clock, _ = build(tmp_path, guide)
    caption.update([GOL, WEB4])
    clock.now += 59
    caption.update([GOL, WEB4])
    assert guide.asked == [[GOL.uuid, WEB4.uuid]]
    clock.now += 1
    caption.update([GOL, WEB4])
    assert len(guide.asked) == 2
    assert path.read_text(encoding="utf-8") == "DAZN | Diretta Gol Serie A\nDAZN | Web 4"


def test_only_channels_without_a_known_programme_are_asked(tmp_path):
    guide = Guide({WEB4.uuid: Program("Genoa - Juventus", 9000.0)})
    caption, _, clock, _ = build(tmp_path, guide)
    caption.update([WEB4])
    clock.now += 1
    caption.update([GOL, WEB4])
    assert guide.asked == [[WEB4.uuid], [GOL.uuid]]


def test_an_unavailable_guide_leaves_the_names_and_is_tried_again_after_a_minute(tmp_path):
    guide = Guide({WEB4.uuid: Program("Genoa - Juventus", 9000.0)})
    guide.fails = True
    caption, path, clock, lines = build(tmp_path, guide)
    caption.update([WEB4])
    for _ in range(5):
        clock.now += 10
        caption.update([WEB4])
    assert len(guide.asked) == 1
    assert path.read_text(encoding="utf-8") == "DAZN | Web 4"
    clock.now += 10
    caption.update([WEB4])
    assert len(guide.asked) == 2
    assert len(lines) == 1 and "showing names only" in lines[0]
    guide.fails = False
    clock.now += 60
    caption.update([WEB4])
    assert path.read_text(encoding="utf-8") == "DAZN | Web 4 · Genoa - Juventus"


def test_without_a_guide_the_caption_still_lists_the_names(tmp_path):
    caption, path, _, _ = build(tmp_path, None)
    caption.update([WEB4])
    assert path.read_text(encoding="utf-8") == "DAZN | Web 4"
