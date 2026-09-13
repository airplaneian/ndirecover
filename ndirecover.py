#!/usr/bin/env python3
"""
ndirecover - recover NDI Video Recorder .mov files that never finalized.

See README.md for background. Short version: NDI's recorder reserves a
small region at the top of the file for the QuickTime header and only
writes it on a clean stop. If the recorder crashes, fills the disk, or is
killed, that region stays blank and every tool reports "moov atom not
found" even though all of the video is present.

This rebuilds the index by locating frame headers in the media itself,
then writes a valid header. The frame data is never copied or re-encoded.

    ndirecover.py scan    broken.mov
    ndirecover.py recover broken.mov --ref good.mov
    ndirecover.py recover broken.mov --ref good.mov --write

You need a reference recording: any clip from the same recorder at the
same resolution and frame rate that stopped cleanly. Ten seconds is
plenty. Its codec parameters are copied verbatim into the rebuilt file.
"""

import argparse
import math
import os
import struct
import sys

MARKER = b"\x04\x00\x00"      # frame header bytes 1-3 (second-field offset 4)
DEFAULT_ALIGN = 4096          # NDI pads each frame to a 4 KiB boundary
QMIN, QMAX = 0x20, 0x7F       # plausible range for the quality byte
CHUNK = 64 << 20


# ------------------------------------------------------------------ atoms

def atom(kind, payload):
    return struct.pack(">I", len(payload) + 8) + kind + payload


def full(kind, version, flags, payload):
    return atom(kind, struct.pack(">B3s", version, flags.to_bytes(3, "big")) + payload)


def walk(buf, start=0, end=None):
    """Yield (kind, payload_start, atom_end) for atoms in buf[start:end]."""
    if end is None:
        end = len(buf)
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        kind = buf[pos + 4:pos + 8]
        body = pos + 8
        if size == 1:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield kind, body, pos + size
        pos += size


def find(buf, path, start=0, end=None):
    cur = (start, end if end is not None else len(buf))
    for want in path:
        hit = None
        for kind, b, e in walk(buf, cur[0], cur[1]):
            if kind == want:
                hit = (b, e)
                break
        if hit is None:
            return None
        cur = hit
    return cur


class Reference:
    """Codec parameters and sample description lifted from a good recording."""

    def __init__(self, path):
        with open(path, "rb") as f:
            buf = f.read()
        self.path = path

        moov = find(buf, [b"moov"])
        if not moov:
            raise SystemExit(f"{path}: no moov atom. Is this a finalized recording?")

        for kind, tb, te in walk(buf, *moov):
            if kind != b"trak":
                continue
            hdlr = find(buf, [b"mdia", b"hdlr"], tb, te)
            if not hdlr or buf[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
                continue

            stsd = find(buf, [b"mdia", b"minf", b"stbl", b"stsd"], tb, te)
            if not stsd:
                continue
            s, e = stsd
            self.stsd = buf[s - 8:e]

            # visual sample entry: width at +32, height at +34
            entry = s + 8
            self.width = struct.unpack(">H", buf[entry + 32:entry + 34])[0]
            self.height = struct.unpack(">H", buf[entry + 34:entry + 36])[0]
            self.tag = buf[entry + 4:entry + 8]

            mdhd = find(buf, [b"mdia", b"mdhd"], tb, te)
            self.timescale = struct.unpack(">I", buf[mdhd[0] + 12:mdhd[0] + 16])[0]

            stts = find(buf, [b"mdia", b"minf", b"stbl", b"stts"], tb, te)
            self.delta = struct.unpack(">I", buf[stts[0] + 12:stts[0] + 16])[0]
            return

        raise SystemExit(f"{path}: no video track found")

    @property
    def fps(self):
        return self.timescale / self.delta

    def describe(self):
        return (f"{self.tag.decode('ascii', 'replace')} "
                f"{self.width}x{self.height} @ {self.fps:g}fps "
                f"({len(self.stsd)} byte stsd)")


# ------------------------------------------------------------------ scan

def scan(path, align=DEFAULT_ALIGN, progress=True):
    """Find frame header offsets.

    Frames are padded to `align`, so only aligned positions are candidates.
    That means inspecting one position per 4096 bytes rather than every
    byte, which is both far faster and a strong filter: a 3-byte marker
    hits randomly about once per 16 MiB, but landing on alignment as well
    is vanishingly rare.

    align=1 falls back to checking every position.
    """
    offsets = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        base = 0
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            if align > 1:
                first = (-base) % align
                for i in range(first, len(buf) - 4, align):
                    if buf[i + 1:i + 4] == MARKER and QMIN <= buf[i] <= QMAX:
                        offsets.append(base + i)
            else:
                i = 0
                while True:
                    j = buf.find(MARKER, i)
                    if j < 1 or j + 3 > len(buf):
                        break
                    if QMIN <= buf[j - 1] <= QMAX:
                        off = base + j - 1
                        if not offsets or off > offsets[-1]:
                            offsets.append(off)
                    i = j + 1
            base += len(buf)
            if progress:
                pct = 100.0 * base / size if size else 0
                print(f"\r  scanning {pct:5.1f}%  {len(offsets):,} frames",
                      end="", file=sys.stderr, flush=True)
    if progress:
        print(file=sys.stderr)
    return offsets


def stats(offsets, filesize):
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    if not gaps:
        return {}
    med = sorted(gaps)[len(gaps) // 2]
    return {
        "count": len(offsets),
        "min": min(gaps),
        "median": med,
        "max": max(gaps),
        "outliers": sum(1 for g in gaps if g < med * 0.25 or g > med * 4),
        "align": math.gcd(*offsets[:200]) if len(offsets) > 1 else 0,
    }


# ------------------------------------------------------------------ moov

def build_moov(ref, offsets, sizes):
    n = len(offsets)
    duration = n * ref.delta

    mvhd = full(b"mvhd", 0, 0,
                struct.pack(">IIII", 0, 0, ref.timescale, duration) +
                struct.pack(">IH", 0x00010000, 0x0100) + b"\x00" * 10 +
                struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0,
                            0, 0, 0x40000000) +
                b"\x00" * 24 + struct.pack(">I", 2))

    tkhd = full(b"tkhd", 0, 3,
                struct.pack(">IIIIII", 0, 0, 1, 0, duration, 0) + b"\x00" * 8 +
                struct.pack(">HHHH", 0, 0, 0, 0) +
                struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0,
                            0, 0, 0x40000000) +
                struct.pack(">II", ref.width << 16, ref.height << 16))

    mdhd = full(b"mdhd", 0, 0,
                struct.pack(">IIII", 0, 0, ref.timescale, duration) +
                struct.pack(">HH", 0x55C4, 0))
    hdlr = full(b"hdlr", 0, 0,
                b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    vmhd = full(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    dinf = atom(b"dinf", full(b"dref", 0, 0,
                              struct.pack(">I", 1) + full(b"url ", 0, 1, b"")))

    stts = full(b"stts", 0, 0, struct.pack(">III", 1, n, ref.delta))
    stsc = full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
    stsz = full(b"stsz", 0, 0, struct.pack(">II", 0, n) +
                b"".join(struct.pack(">I", s) for s in sizes))
    # co64 rather than stco: these files routinely exceed 4 GiB
    co64 = full(b"co64", 0, 0, struct.pack(">I", n) +
                b"".join(struct.pack(">Q", o) for o in offsets))

    stbl = atom(b"stbl", ref.stsd + stts + stsc + stsz + co64)
    minf = atom(b"minf", vmhd + dinf + stbl)
    mdia = atom(b"mdia", mdhd + hdlr + minf)
    return atom(b"moov", mvhd + atom(b"trak", tkhd + mdia))


def build_header(region, filesize):
    """ftyp + free padding + a 64-bit mdat header ending exactly at `region`,
    so the existing frame data becomes the mdat payload untouched."""
    ftyp = atom(b"ftyp", b"qt  " + struct.pack(">I", 0x200) + b"qt  ")
    mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(
        ">Q", 16 + (filesize - region))
    pad = region - len(ftyp) - len(mdat)
    if pad < 8:
        raise SystemExit(f"header region of {region} bytes is too small")
    header = ftyp + atom(b"free", b"\x00" * (pad - 8)) + mdat
    assert len(header) == region
    return header


# ------------------------------------------------------------------ cli

def cmd_scan(args):
    offsets = scan(args.movie, args.align, not args.quiet)
    size = os.path.getsize(args.movie)
    st = stats(offsets, size)
    if not st:
        raise SystemExit("no frames found. Try --align 1, or check that this "
                         "is an NDI SpeedHQ recording.")
    print(f"file       : {args.movie}")
    print(f"size       : {size:,} bytes")
    print(f"frames     : {st['count']:,}")
    print(f"first      : {offsets[0]:,}")
    print(f"frame size : min {st['min']:,}  median {st['median']:,}  "
          f"max {st['max']:,}")
    print(f"alignment  : {st['align']:,}")
    print(f"outliers   : {st['outliers']:,}")
    if args.out:
        with open(args.out, "w") as f:
            f.writelines(f"{o}\n" for o in offsets)
        print(f"wrote offsets to {args.out}")


def cmd_recover(args):
    ref = Reference(args.ref)
    offsets = scan(args.movie, args.align, not args.quiet)
    if len(offsets) < 2:
        raise SystemExit("no frames found. Try --align 1.")

    filesize = os.path.getsize(args.movie)
    sizes = [b - a for a, b in zip(offsets, offsets[1:])]
    offsets = offsets[:-1]          # final frame length is unknown, drop it
    n = len(offsets)
    region = offsets[0]

    st = stats(offsets, filesize)
    moov = build_moov(ref, offsets, sizes)
    header = build_header(region, filesize)

    print(f"reference  : {ref.path}")
    print(f"            {ref.describe()}")
    print(f"file       : {args.movie} ({filesize:,} bytes)")
    print(f"frames     : {n:,}")
    print(f"duration   : {n / ref.fps:,.1f} s ({n / ref.fps / 60:.2f} min)")
    print(f"frame size : min {st['min']:,}  median {st['median']:,}  "
          f"max {st['max']:,}")
    print(f"outliers   : {st['outliers']:,}")
    print(f"header     : {region:,} bytes at offset 0")
    print(f"moov       : {len(moov):,} bytes appended at {filesize:,}")

    if st["outliers"]:
        print("\nnote: gaps well outside the median can mean false positives "
              "or missing frames. Spot-check playback around them.")

    if not args.write:
        print("\ndry run, nothing written. Re-run with --write")
        return

    with open(args.movie, "r+b") as f:
        if args.backup:
            f.seek(0)
            with open(args.backup, "wb") as b:
                b.write(f.read(region))
            print(f"saved original header bytes to {args.backup}")
        f.seek(0)
        f.write(header)
        f.seek(0, os.SEEK_END)
        f.write(moov)
    print("done. Verify with:")
    print(f"  ffprobe -v error -show_entries format=duration "
          f"-show_entries stream=codec_name,width,height,r_frame_rate "
          f"-select_streams v -of default=noprint_wrappers=1 {args.movie}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = dict(align=dict(type=int, default=DEFAULT_ALIGN,
                             help="frame padding in bytes; 1 scans every "
                                  f"position (default {DEFAULT_ALIGN})"))

    s = sub.add_parser("scan", help="locate frames, report, write nothing")
    s.add_argument("movie")
    s.add_argument("--align", **common["align"])
    s.add_argument("--out", help="write offsets to this file")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("recover", help="rebuild the QuickTime header")
    r.add_argument("movie")
    r.add_argument("--ref", required=True,
                   help="a recording from the same source that stopped cleanly")
    r.add_argument("--align", **common["align"])
    r.add_argument("--write", action="store_true",
                   help="modify the file (default is a dry run)")
    r.add_argument("--backup", help="save the original header bytes here")
    r.add_argument("-q", "--quiet", action="store_true")
    r.set_defaults(func=cmd_recover)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
