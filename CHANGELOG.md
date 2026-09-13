# Changelog

## 0.2.1 — 2026-09-13

- The plugin description now says what 0.2.0 added: with an API key, a channel left on
  the fallback is sent back to its first stream.

## 0.2.0 — 2026-09-13

- With a Dispatcharr API key, sends a channel left on the fallback back to its first
  stream. Dispatcharr never leaves the fallback by itself, so a channel whose provider
  came back within minutes stayed on the card for hours while anyone held it.
- The first try comes after two minutes on the card, then 4, 8 and 15 minutes apart while
  the stream keeps failing; the wait starts over once the channel has held a real stream
  for 15 minutes.
- Asks Dispatcharr nothing while nobody watches the card. Without a key, nothing changes.

## 0.1.0 — 2026-08-10

First release.

- Plays a looping image or video when every real stream on a channel has failed.
- Reads the picture from a path inside the data volume or from an `http(s)` link, which is
  downloaded and cached; a failed download falls back to the cached copy.
- Attaches the fallback last in each channel's stream order, so Dispatcharr reaches it
  only after every real stream has failed.
- Excludes channels by group name, channel number, or channel name.
- Covers channels added by an M3U refresh, and restarts the fallback by itself when a
  channel starts.
- Serves one MPEG-TS encode to every viewer at once. The encoder starts on the first
  viewer and stops fifteen seconds after the last one leaves.
- Encodes at a constant bitrate with H.264 filler data, so the card fills Dispatcharr's
  one-megabyte start buffer in a fraction of a second rather than minutes, and survives
  the remux the default `ffmpeg` stream profile performs.
- Matches the picture's own resolution by default, up to 1920x1080.
- Encodes dark gradients without banding and without pulsing between clear and banded: no
  `-tune`, and a four-second VBV buffer.
- Reclaims its port from a fallback left behind by an earlier run, so Apply recovers on
  its own instead of reporting the port as taken.
