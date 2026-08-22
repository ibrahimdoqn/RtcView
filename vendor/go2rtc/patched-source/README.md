# go2rtc patched source (preserved copy)

This directory holds the **actual source file** RtcView's go2rtc fix touches —
not just the diff — so the exact change survives even if the upstream
repository, tag, or commit this is pinned to ever becomes unavailable
(deleted, force-pushed, GitHub outage, etc.).

- `pkg/core/writebuffer.go.upstream` — the file exactly as it was at go2rtc
  `v1.9.14` (commit `b5948cfb25404cc5cb37b166ecaa2dca20b11d4b`), before any
  RtcView change.
- `pkg/core/writebuffer.go` — the same file **after** applying
  `scripts/go2rtc-writebuffer-recover.patch`. This is the literal source
  that was compiled into `vendor/go2rtc/go2rtc_linux_amd64` /
  `go2rtc_linux_arm64`.

## What changed and why

`Write()`'s `w.Writer` is often an in-flight `net/http.ResponseWriter` (a
streamed MP4/WebRTC output). If the HTTP handler goroutine serving it has
already returned — client disconnected, stream reloaded/deleted — `net/http`
invalidates internal per-request state, and a stale Sender goroutine still
holding this `WriteBuffer` can panic deep inside `net/http` on the next
`Write`/`Flush`. That panic was unrecovered, which crashed go2rtc's entire
process — every camera's stream, not just the one whose consumer
disconnected. Confirmed against upstream issue
[AlexxIT/go2rtc#1261](https://github.com/AlexxIT/go2rtc/issues/1261): three
separate crash reports share the identical nil-pointer program counter,
reached via three different codec paths (H264, H265, PCM/FLAC audio) — this
is generic to any codec, not something a client-side workaround (e.g.
forcing one specific codec) could fix.

The fix (see the diff between the two files above, or
`scripts/go2rtc-writebuffer-recover.patch`) wraps the risky write in a
`recover()` that turns that panic into an ordinary per-consumer error —
contained exactly like a normal write failure already was, a few lines
below.

## Staying in sync when go2rtc's pinned version changes

Both files here are refreshed **automatically** by `scripts/build_go2rtc.sh`
every time it runs — no separate manual step. Re-pointing to a newer
upstream go2rtc release (see the longer explanation in that script's header
comment and `README.md`'s "go2rtc neden RtcView'ın kendi derlemesi" note) is
just `scripts/build_go2rtc.sh <new-tag>`; it re-clones upstream at that tag,
re-applies the patch (copying both the pre- and post-patch file here as it
goes), runs the regression test, and rebuilds `vendor/go2rtc/go2rtc_linux_*`.
Commit the result and this snapshot is guaranteed to match whatever was
actually compiled.
