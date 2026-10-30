# Could Not Dispatch — working notes

Decisions, traps and the release routine. What the plugin does for a user is in the
[README](../README.md).

## Decisions

- **One custom stream per channel.** Dispatcharr keeps the M3U profile a session holds under
  `stream_profile:<stream_id>`, per stream, not per channel. With one shared fallback stream a
  channel sent back from the card could take a provider connection without counting it: a
  provider limited to 3 connections served 4 streams (0.3.0). Dispatcharr counts every custom
  stream as a HDHomeRun tuner, so the advertised tuner count grows by one per channel.
- **The slate is a detached process** serving `http://127.0.0.1:<port>/slate.ts`, not a view
  inside Dispatcharr. Dispatcharr has no scheduler for plugins: the `restart` action is bound
  to `channel_start` and brings the process back, at most once a minute per uwsgi worker.
- **The fallback stays last and off excluded channels.** Apply and Cover new channels detach it
  from channels excluded since, and move it back to the end where a stream was added after it.
- **Sending a channel back** uses `POST /proxy/ts/change_stream/<uuid>` after 30 s on the card,
  then after 1, 2 and 5 minutes, then every 5 minutes (0.5.0; 120 s, 4, 8 and 15 minutes
  before). A failed try costs the viewer about 3 s of chain walking per refusing stream while
  the card stays on screen. A channel on the card is recognised by its URL: the slate URL
  alone or with `?channel=<uuid>` and nothing else, so the bench's `?bench=a` stays a provider
  stream.
- **A second card for a full provider** (0.6.0, issue #1, chosen by the operator on 24 October
  2026: two cards, not the three asked for). Each fallback stream points at
  `slate.ts?channel=<uuid>`, so the server knows the channel at connect time and serves the
  second encoder when every source of the chain, cards left out, belongs to an account full in
  the last 20 s (`Recovery.crowded`). The cards are recognised in the catalogue by their URL,
  since they sit on the `custom` account, which has no limit and would never read as full. A
  broken link and a passing outage cannot be told apart: Dispatcharr does not tell a plugin
  why each stream failed. A provider that fills up less than one poll (10 s) before the
  failover is not seen yet, and the channel gets the usual card.
- **A full account is judged without the channel asking for the card** (0.7.1). Each
  observation of a full account keeps the channels holding it, and `crowded(uuid)` leaves the
  asker out. On 28 October 2026 an offline channel sent back as a connection freed up held the
  third connection while it walked its four dead sources, the check saw the account full, and
  15 s later it fell back onto the card for a full provider with a connection free. A channel
  back on the card within 30 s of a `change_stream` (`Episode.sent_at`, either kind) has failed
  its sources and gets the usual card even if others filled the provider meanwhile. The first
  arrival of an offline channel on a provider full of others still gets the full card: its
  sources were never tried. The same holds for the return: a channel that filled the account
  itself is not taken for one waiting for a connection, so it waits 30 s instead of being sent
  back to its dead sources the moment it leaves them (bench of 28 October: 0 s before). `in_use`
  still counts every channel holding a connection.
- **The channels in use are written by `drawtext` from a file** (0.7.0, off by default,
  asked by the operator on 28 October 2026). The recovery loop already reads
  `/proxy/ts/status` every 2-10 s, so after each check it writes the channels on the
  profiles of accounts full in the last 20 s to `.runtime/in-use.txt`, through a `.part` and
  `os.replace`, as `drawtext` with `reload=1` needs; the encoder never restarts. The file
  carries only names, no heading: the picture says what the list is, in its own language.
  Names come from the catalogue's `effective_name`, never from `channel_name` of the status,
  which is at times the stream's name. The programme comes from
  `POST /api/epg/current-programs/`, asked only while an account is full, only for channels
  without a programme still on, with a 2 s timeout and a minute before trying again after an
  error, so a slow guide costs at most 2 s once a minute. A failure inside the caption is
  logged once and never leaves `tick`. Size, spacing and colour are fixed, set on the
  operator's 1080p card and scaled to the encoder's height; the height of the first line is a
  setting since 0.7.1, 58.5% by default.
- **A late listener starts on a video picture.** The head start begins at the last PAT before
  a packet that both carries the random-access flag and opens a video PES. ffmpeg's muxer sets
  the flag on every AAC packet too, and up to 0.4.0 an audio packet could open the head start:
  the client got up to a second of pictures without SPS/PPS, and Plezy gave up on 12 of 13
  cards on 6 October 2026. A head start is only taken from a running encoder: after an idle
  stop the buffer belongs to the previous run, whose timestamps run ahead of the new one.
- **A full provider is remembered, not observed at the moment of the fallback** (0.4.0). With
  `channel_shutdown_delay` at 0 the channel a viewer left frees its connection within a couple
  of seconds, so by the next poll the provider may have room again. The status is therefore
  read every 10 s even with nobody on the card, and a channel whose first stream's account was
  full in the last 20 s goes back as soon as there is room. The usage per profile comes from
  `/proxy/ts/status` (channels not on the card), the limits from `/api/m3u/accounts/`; a profile
  with `max_streams` 0 has no limit.
- **The catalog is read every 15 minutes** (chains, accounts), and at once, at most every 30 s,
  when a channel on the card is not in it: a channel created after the last read still gets its
  return (0.4.0).
- **The card runs at 25 fps by default** (0.4.0): Jellyfin takes a transcode's frame rate from
  its first input, so a session that began on a 5 fps card stayed at 5 fps after going back to
  the provider. Saved settings keep their stored value.
- **A picture enters at 5 fps and the threads are fixed** (0.7.2, issue #4: 1 CPU limit on
  Kubernetes, card at the limit). `-loop 1` used to read, decode and scale the file at the full
  25 fps, and ffmpeg sized its threads on the cores it sees, which in a pod are the node's: 119
  threads on 16 cores. Now `-framerate 5`, `-threads 1` on the decoder, `-filter_threads 1` and
  `-threads 2` on x264. Under `docker run --cpus=1`, 1080p PNG: 0.73 → 0.37 core, 910 → 291 MB,
  first megabyte 4.50 → 1.30 s, PSNR on a quadrant 66.76 → 66.94 dB. Not 1 fps, though it saves
  0.05 core more: all 25 frames of each second then reach x264 together, the output comes in
  blocks with gaps up to 0.85 s, against 0.19 s at 5 fps, and they hit the CPU quota, 124 ms/s
  of throttling against 0. Looping a pre-encoded file with `-c copy` costs about 0 CPU, but
  needs a cache, a loop length aligned to AAC frames and a new check of the seam on every
  client, and the card with `drawtext` would still encode: left out unless this is not enough.
- **State lives in `.runtime/state.json`**, never in the plugin settings: saving the settings
  replaces the whole object.

## Traps

- Dispatcharr passes `params` to `run()` and never puts them in `context`. An action started by
  an event gets `{"event": ..., "payload": ...}`; a button gets `{}`.
- Only the events in `apps/connect/models.py:SUPPORTED_EVENTS` reach a plugin: 20 in 0.32.0.
  `tests/test_plugin_contract.py` keeps that list.
- From Dispatcharr 0.31.0 a profile URL transform that does not match fails closed. The
  `custom` account's default profile must keep `^(.*)$` → `$1`, or the slate cannot start.
- Importing a zip with `overwrite=true` replaces the plugin folder: copy `no-more-streams.png`
  (or whatever `media_source` points to) and `.runtime/` out first and back after. The new code
  only runs after `POST /api/plugins/plugins/reload/`, and the reload stops every plugin's
  processes, so press Apply afterwards.
- `/api/channels/channels/` leaves out channels hidden from the output unless asked with
  `visibility_filter=all`: up to 0.5.1 such a channel never went back to the provider, and the
  first bench of 0.6.0, on hidden channels, gave them the usual card.
- On an auto-created channel `name` is the provider's raw name (`DAZN WEB 4 FULL HD`); the
  name the operator gave it is in `override.name`, and `effective_name` resolves both.
  `/api/channels/channels/` ignores a `uuid` filter and returns the whole list.
- `/api/epg/current-programs/` asked by `channel_uuids` reads only the database: a channel
  whose guide is still being parsed, or comes from a dummy source, is simply missing.
- Dispatcharr fetches the card through the channel's stream profile, like any source: a profile
  that cannot start (a wrapper script saved with CRLF endings, or one that never exits on a
  refused source) keeps the channel off the card too.
- On Windows the `posix_only` and `proc_fs_only` tests are skipped and `mypy .` reports
  `os.WNOHANG`; `mypy --platform linux .` is clean. The CI on Linux is the reference.

## Checking the connection count

Redo this after any change to the way a channel goes back to the provider. It uses no provider
connection: an `STD` M3U account uploaded from a file (`POST /api/m3u/accounts/` with the file,
`max_streams` 1, then enable its group and refresh), whose streams point at the card with a
different query (`http://127.0.0.1:9721/slate.ts?bench=a`: no `channel`, so the usual card).
Three channels `[test stream, card]`: A takes the slot, B and C fall on the card, A leaves.
With a card set for a full provider, B and C get that one: a second ffmpeg on that file starts
when B connects.
After 120 s `/proxy/ts/status` must show one channel on that profile, and the other still on
the card; with the shared card of 0.2.1 there were two. Since 0.4.0 the other one does not
even try while the provider is full, so there is no `No profiles available with connection
capacity` in the log (25 October 2026: B back 5 s after A left, C on the card). Afterwards delete the channels, the account, its group, and the uploaded file in
`/data/uploads/m3us/`, which deleting the account leaves behind.

## Release

1. Version in `plugin.json`, `could_not_dispatch/constants.py` and `pyproject.toml`.
2. A `CHANGELOG.md` section, written before the tag.
3. `python scripts/build_zip.py`, then an annotated tag `vX.Y.Z` named `Could Not Dispatch X.Y.Z`.
4. A GitHub Release with the CHANGELOG text, the list of commits it contains, and the zip.
5. Publishing the Release starts `.github/workflows/registry-pr.yml`, which opens the PR to
   `Dispatcharr/Plugins` from the `PilaScat/Plugins` fork: the version in
   `plugins/could-not-dispatch/plugin.json`, the README next to it when it changed, and the
   CHANGELOG section as the description. It needs the `REGISTRY_PR_TOKEN` secret, a classic PAT
   of PilaScat with `public_repo` only; it can be rerun by hand with the tag. The registry
   installs the zip at `source_url`, so a README change reaches users only with a new version.
