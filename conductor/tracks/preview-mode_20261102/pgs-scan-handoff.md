# Handoff from pgs-scan: preview mode in both apps

From pgs-scan's track `preview-viewer_20261112`, 2026-11-12. pgs-scan and PGS Spectral now
preview through this branch, at `9c0a191`. One shared viewer drives `start_preview(region,
max_size)` from a feed thread per camera, as `docs/preview.md` §1 describes, and draws each
frame where its own `geometry.region` falls. Their suites pass against this commit on Python
3.11, 3.12 and 3.13, off-rig only: the simulated cameras, plus pgs-scan's own simulated
DSLR. pgs-scan hasn't been run on the lab rig against this branch yet.

What pgs-scan ran into, for this track to decide on before it merges:

## 1. `PreviewFrame` doesn't carry a bit depth

A capture's `Frame.metadata['bit_depth']` says how deep its values go, but a `PreviewFrame`
has nothing like it. To find full scale for the screen, the histogram and the clipping
overlay, pgs-scan copies `eight_bit()`'s rule: the digits in the format's name, and
left-aligned if a value reaches `1 << depth`. That's a guess, and the guide (§5) asks every
app to make it.

Suggestion: a `bit_depth` on `PreviewFrame` or `PreviewGeometry`, matching the capture's.

## 2. The CM3's Mono16 clips at 65520, and `bit_depth()` says 16

The CM3's `Mono16` is 12-bit data shifted up, in steps of 16, so a saturated pixel reads
65520, not 65535. `_genicam.bit_depth("Mono16")` is 16, so the depth alone puts clipping at
65535, where no pixel ever gets. pgs-scan works around this by reading the frame's step from
the low bits of its values. That works on real frames, but it's a heuristic.

Suggestion: report significant bits (12 here) as distinct from the container's 16, for
captures and previews alike. Or say where a format saturates.

## 3. `max_size` rounds down a whole decimation step

With `max_size` set to the view's size, a region slightly larger than the view arrives at
half resolution: the step is `ceil(longest / max_size)`, so 1024 px into 880 gives step 2,
which is 512 px. In pgs-scan's viewer that's `Detail 50%` at Fit, and the image is visibly
soft. Asking for the *nearest* step instead (1024 px at 116%) would cost up to about 1.3×
the data. pgs-scan hasn't changed its request; the maintainer is deciding on image quality
against data.

Question: should the library offer "at least this many pixels" (a floor alongside the cap),
or should apps work out the nearest step themselves? Apps can't, in general: the steps are
driver-specific (GenICam decimation 1–2 then host strides; the Z7 II's zoom levels).

## 4. `host_crop` is private

PGS Spectral's simulated full-spectrum DSLR (a `FileCamera`) has to preview a region now.
`cameras._preview.host_crop` does exactly that, but it's private, so pgs-scan carries a small
copy in `pgs_scan/spectral/simulation.py`.

Suggestion: export it, or a helper an app's own simulated camera can build on.

## Not for this track, for reference

- pgs-scan logs a preview failure once and retries it with backoff (100 ms doubling to 5 s).
  It never treats one as a hardware error. Spectral reopens a lost camera through its own
  reconnect.
- A capture during preview (pgs-scan's QR scan) pauses the feed, which stops preview for the
  capture and starts it again after, as §6 says. Pixel-format and throughput-limit changes
  pause it too, to be safe. Whether those need preview stopped hasn't been checked on
  hardware.
