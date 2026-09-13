# ndirecover

Recover NDI Video Recorder `.mov` files that never finalized.

If a recording was interrupted by a crash, a full disk, or a killed process, the
file will be large and apparently full of data, but every tool refuses to open
it:

```
[mov,mp4,m4a,3gp,3g2,mj2] Format mov,mp4,m4a,3gp,3g2,mj2 detected only with low score of 1
[mov,mp4,m4a,3gp,3g2,mj2] moov atom not found
```

`ndirecover` rebuilds the missing QuickTime index in place. The video data is
never copied, moved, or re-encoded. On a 29 GB recording the whole operation
takes a few seconds and writes about 1.3 MB.

## Requirements

- Python 3.8 or later, no third-party packages
- ffmpeg/ffprobe, for verification only
- **A reference recording**: any clip from the same recorder, same resolution
  and frame rate, that stopped cleanly. Ten seconds is enough.

## Usage

Check what's in the file before changing anything:

```
./ndirecover.py scan broken.mov
```

```
file       : broken.mov
size       : 31,151,095,808 bytes
frames     : 112,008
first      : 4,096
frame size : min 114,688  median 282,624  max 602,112
alignment  : 4,096
outliers   : 0
```

Then rebuild. The default is a dry run:

```
./ndirecover.py recover broken.mov --ref good.mov
./ndirecover.py recover broken.mov --ref good.mov --write --backup header.bak
```

Verify:

```
ffprobe -v error -show_entries format=duration \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -select_streams v -of default=noprint_wrappers=1 broken.mov

ffmpeg -v error -ss 900 -i broken.mov -frames:v 60 -f null - && echo clean
```

The second command decodes 60 frames from the 15 minute mark. Silence means no
errors. Worth running at a few points rather than trusting the header alone,
since a file can parse correctly and still contain damaged frames.

## What goes wrong, and why this works

NDI's recorder reserves 4096 bytes at the top of the file and writes frame data
immediately after it. The QuickTime header, which describes where every frame
begins and how long it lasts, is written into that reserved region only when
recording stops cleanly. Interrupt the recorder and the region stays blank, so
30 GB of perfectly good video is unreadable for want of an index.

Recovery means reconstructing that index. Two properties of the format make it
practical:

**SpeedHQ frames have a recognizable header.** Byte 0 is a quality value that
drifts frame to frame. Bytes 1 to 3 are a 24-bit little-endian offset to the
second field, and the value `04 00 00` is the defined "single field" case, which
every frame in a progressive recording carries. Bytes 4 onward are compressed
slice data with no fixed structure, so matching on them produces false
confidence: they may happen to agree across the first few frames and then
diverge.

**Frames are padded to 4 KiB boundaries.** This is what makes the scan reliable
and fast. A 3-byte marker hits at random roughly once per 16 MiB, which over
30 GB means hundreds of false positives, but a false positive that also lands on
a 4096-byte boundary is expected about 0.2 times across the same file. It also
means only one position in 4096 needs checking, so the scan reads the file
sequentially but inspects 16,384 positions per 64 MiB rather than 67 million.

With frame offsets known, sizes follow from the gaps between them, and a `moov`
atom can be assembled. The codec parameters come from the reference file's
sample description, copied verbatim, which is why the reference has to match the
broken recording's format.

## What the tool writes

Two small edits, neither of which touches frame data:

1. Into the 4096-byte reserved region at the top: an `ftyp` atom, `free`
   padding, and a 64-bit `mdat` header positioned so that its payload begins
   exactly where the first frame does. The existing video becomes the contents
   of a valid `mdat` without moving a byte.
2. At the end of the file: a `moov` atom containing `stsd` (from the reference),
   `stts`, `stsc`, `stsz`, and `co64`.

`co64` rather than `stco` because these recordings routinely exceed 4 GB and
32-bit chunk offsets would overflow.

The reserved region contains only the `NDI_REBUILD` marker and zero padding, so
writing there destroys nothing. `--backup` saves it anyway.

## Notes and limitations

**The last frame is dropped.** Frame sizes are derived from the distance to the
next frame, so the final frame's length is bounded only by end of file, and it
is also the frame most likely to be a partial write. One frame at 60fps is 17
milliseconds.

**Audio is not recovered.** NDI writes PCM audio in chunks interleaved between
video frames. This tool builds a video-only index. If your recording has audio
you care about, this will not get it back.

**`--align 1` exists for unusual files.** If `scan` finds nothing, the recording
may not use 4 KiB padding. Scanning every position is much slower and much
noisier, but it will find frames that alignment filtering misses. Check the
reported `alignment` value in `scan` output; if it comes back as something other
than 4096, pass that value to `--align`.

**Check the outlier count.** Gaps far from the median mean either false
positives inserting phantom frames or real gaps where frames are missing. Zero
outliers across a long recording is a good sign that the index is complete.

**This is not a general MOV repair tool.** It assumes the NDI layout: a reserved
header region, SpeedHQ video, frames padded to a fixed boundary. For ordinary
truncated MP4/MOV files where the header was written but the tail was lost, use
[untrunc](https://github.com/anthwlock/untrunc) instead.

## Diagnosing a file before you start

A quick way to tell whether you're in this situation:

```
xxd -l 16 broken.mov
```

```
00000000: 4e44 495f 5245 4255 494c 4400 0000 0000  NDI_REBUILD.....
```

`NDI_REBUILD` followed by zeros is the unfinalized case. If instead you see
`ftyp` in the first 12 bytes, the header was written and your problem is
something else.

## Verifying the tool itself

The approach was validated by building a synthetic file from known SpeedHQ
frames laid out NDI-style, recovering it, and comparing decoded output frame by
frame against the originals with `ffmpeg -f framemd5`. Every frame came back
bit-identical. Worth repeating if you modify the atom-building code.
