# Cameras return what the hardware produces and never decode raw files

Cameras that produce pixel arrays (e.g. Spinnaker) return frames, and cameras that produce encoded files (e.g. DSLRs over gphoto2) return the camera files in memory, with no common `capture()` that hides the difference. Decoding raw files (e.g. with rawpy) is processing, not device control, so it stays with consumers: a consumer that wants both the original NEF and a processed result saves the bytes and decodes the same bytes from memory, with no re-read from disk.

## Considered Options

- **One `capture()` returning a result with optional array and path fields**: every caller has to check which field is set.
- **Always return an array, decoding raw files with rawpy in the driver**: adds a heavy dependency, fixes one choice of decode parameters, and loses the untouched original file unless it is also saved.
