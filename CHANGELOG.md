# Changelog

## 0.4.0 — 2026-09-14

- A channel that lands on the fallback because the provider has no free connection goes back
  to its first stream as soon as a connection frees up, instead of after two minutes. That is
  what a viewer gets when switching channels with every connection in use: the new channel
  finds no room, falls to the card, and used to stay there for two minutes after the old one
  had closed. Measured with 30 simulated viewers on a provider limited to 3 connections, every
  collective channel change left the three new channels on the card for 120 to 130 s. The
  fallback now reads the connections in use per M3U profile every ten seconds, remembers a
  full provider for 20 seconds, and while such a channel waits it checks every two seconds. A
  refusal for capacity does not count as a try; a channel that lands on the card again within
  two minutes of such a return waits like any other.
- A registry workflow opens the version bump PR on `Dispatcharr/Plugins` when a release is
  published.

## 0.3.3 — 2026-09-14

- Remove fallback says it deletes the fallback streams, plural: every channel has had its
  own since 0.3.0.
- The README links to the licence and to `docs/MEMORY.md` by full address, so they also work
  in the copy the plugin registry keeps.

## 0.3.2 — 2026-09-14

- Apply and Cover new channels now take the fallback off channels excluded since it was
  attached, freeing their streams, and move it back to the end of a channel where a stream
  was added after it. Before, a stream added later sat after the fallback, where the
  failover never reaches it, and an exclusion only held for channels not yet covered.
- The contract tests run the plugin's actions against a temporary runtime folder instead of
  the checkout. Tests, types, lint and build run in CI; `docs/MEMORY.md` holds the
  decisions, the deployment traps and the release routine.

## 0.3.1 — 2026-09-14

- Cover new channels, pressed by hand, covers channels again while Cover new channels
  automatically is off. Only the run started by an M3U refresh is skipped; before, the
  button was skipped too, because the check looked for the action's params where
  Dispatcharr never puts them.
- Checked against Dispatcharr 0.31.0: nothing the plugin relies on changed.

## 0.3.0 — 2026-09-13

- Each channel gets a fallback stream of its own instead of sharing one. Dispatcharr
  records which M3U profile a session holds under the stream, so with one shared stream a
  channel sent back from the card could take a provider connection without counting it,
  or free one another channel still held: a provider limited to 3 connections served 4
  streams at once. Apply and Cover new channels move existing channels to their own
  stream in place, keeping its position in the order.
- Channels on the card are recognised by the fallback URL, so sending them back no longer
  needs a stream id.
- The HDHomeRun tuner count Dispatcharr advertises now grows by one per covered channel,
  since Dispatcharr counts every custom stream as a tuner.
- Disabling or deleting the plugin, and Remove fallback, delete the fallback streams as
  well as detaching them.

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
