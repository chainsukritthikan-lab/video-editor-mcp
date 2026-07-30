#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Free Video Editor connector (MCP server) for Claude Cowork / Claude Code.

Zero pip dependencies - pure Python stdlib + local FFmpeg.
Everything runs on this PC. Nothing is uploaded. No API key. No cost.

Protocol: MCP over stdio (newline-delimited JSON-RPC 2.0).
"""

import asyncio
import base64
import io
import json
import math
import time
import os
import re
import shutil
import statistics
import subprocess
import sys
import glob as globmod
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------- encoding
# Windows default codepage (cp874 on Thai systems) mangles JSON. Force UTF-8.
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SERVER_NAME = "video-editor"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
FFMPEG_TIMEOUT = 3600  # 1 hour per operation

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")


# ---------------------------------------------------------------- ffmpeg discovery
def _find_binary(name):
    """Locate ffmpeg/ffprobe: PATH first, then common Windows install spots."""
    found = shutil.which(name)
    if found:
        return found
    exe = name + ".exe"
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", exe),
        os.path.join(r"C:\ffmpeg\bin", exe),
        os.path.join(r"C:\Program Files\ffmpeg\bin", exe),
        os.path.join(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"), exe),
        os.path.join(os.path.expandvars(r"%USERPROFILE%\scoop\shims"), exe),
        os.path.join(r"C:\ProgramData\chocolatey\bin", exe),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # winget installs land in a versioned Packages folder
    pkgroot = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(pkgroot):
        hits = globmod.glob(os.path.join(pkgroot, "*", "**", exe), recursive=True)
        if hits:
            return hits[0]
    return None


FFMPEG = _find_binary("ffmpeg")
FFPROBE = _find_binary("ffprobe")


class ToolError(Exception):
    pass


def _require_ffmpeg():
    global FFMPEG, FFPROBE
    if not FFMPEG:
        FFMPEG = _find_binary("ffmpeg")
        FFPROBE = _find_binary("ffprobe")
    if not FFMPEG:
        raise ToolError(
            "FFmpeg is not installed on this PC.\n"
            "Install it (free) with one command in PowerShell:\n"
            "    winget install --id Gyan.FFmpeg -e\n"
            "Then fully restart Claude so the new PATH is picked up."
        )


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=FFMPEG_TIMEOUT):
    """Run a binary directly (no shell). Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    dec = lambda b: b.decode("utf-8", errors="replace")
    return proc.returncode, dec(proc.stdout), dec(proc.stderr)


def ffmpeg_run(args):
    _require_ffmpeg()
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y"] + args
    try:
        code, out, err = run(cmd)
    except subprocess.TimeoutExpired:
        raise ToolError("FFmpeg timed out (over 1 hour). The file may be huge.")
    if code != 0:
        tail = "\n".join([l for l in err.strip().splitlines() if l.strip()][-12:])
        raise ToolError("FFmpeg failed:\n" + tail)
    return err


def probe(path):
    _require_ffmpeg()
    if not FFPROBE:
        raise ToolError("ffprobe not found next to ffmpeg. Reinstall FFmpeg.")
    code, out, err = run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        timeout=120,
    )
    if code != 0:
        raise ToolError("Cannot read this file:\n" + err.strip()[-500:])
    return json.loads(out)


def check_input(path, what="file"):
    if not path:
        raise ToolError("No %s path given." % what)
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isfile(path):
        raise ToolError("%s not found: %s" % (what.capitalize(), path))
    return path


def make_output(src, suffix, out=None, ext=None):
    """Pick an output path; never silently overwrite a different file."""
    if out:
        out = os.path.abspath(os.path.expandvars(os.path.expanduser(out)))
        parent = os.path.dirname(out)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        return out
    folder = os.path.join(os.path.dirname(src), "edited")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = ext or os.path.splitext(src)[1] or ".mp4"
    cand = os.path.join(folder, "%s_%s%s" % (stem, suffix, ext))
    n = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, "%s_%s_%d%s" % (stem, suffix, n, ext))
        n += 1
    return cand


def has_audio(path):
    try:
        return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))
    except Exception:
        return False


def duration_of(path):
    try:
        return float(probe(path).get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def video_duration_of(path):
    """Length of the VIDEO stream, not the container.

    The container reports the longer of the two streams, and loudness normalisation
    routinely leaves audio a little longer than picture. Using the container figure to
    position an xfade drifts the offsets, and chained across several joins the video
    chain collapses - the file still looks right until something trims to the shortest
    stream and reveals the picture ended early.
    """
    try:
        for s in probe(path).get("streams", []):
            if s.get("codec_type") == "video" and s.get("duration"):
                return float(s["duration"])
    except Exception:
        pass
    return duration_of(path)


def fps_of(path, default=30.0):
    """Frame rate of the video stream.

    Any filter that declares an output rate - zoompan is the one that bites - must be
    told the rate the source actually runs at. Hand it a constant and it relabels the
    frames instead of resampling them: same frame count, new rate, so the picture
    stretches or squeezes while the audio stays put.
    """
    try:
        for s in probe(path).get("streams", []):
            if s.get("codec_type") != "video":
                continue
            for key in ("avg_frame_rate", "r_frame_rate"):
                num, _, den = (s.get(key) or "").partition("/")
                try:
                    rate = float(num) / float(den or 1)
                except ValueError:
                    continue
                if rate > 0:
                    return rate
    except Exception:
        pass
    return default


def parse_time(value, field="time"):
    """Accept 12.5, '12.5', '00:01:30', '1:30', '90s'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().rstrip("s")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            raise ToolError("Bad %s: %r" % (field, value))
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    try:
        return float(s)
    except ValueError:
        raise ToolError("Bad %s: %r (use seconds or MM:SS)" % (field, value))


def human_size(path):
    try:
        b = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return "%.1f %s" % (b, unit)
        b /= 1024.0


def done(path, note=""):
    msg = "Done -> %s  (%s)" % (path, human_size(path))
    return msg + ("\n" + note if note else "")


def escape_filter_path(path):
    """Escape a Windows path for use inside an FFmpeg filter argument."""
    p = os.path.abspath(path).replace("\\", "/")
    p = p.replace(":", "\\:").replace("'", "\\'")
    return p


ASPECTS = {
    "9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080),
    "4:5": (1080, 1350), "4:3": (1440, 1080), "3:4": (1080, 1440),
    "21:9": (2560, 1080),
}

POSITIONS = {
    "top-left": "10:10",
    "top-right": "W-w-10:10",
    "bottom-left": "10:H-h-10",
    "bottom-right": "W-w-10:H-h-10",
    "center": "(W-w)/2:(H-h)/2",
    "top-center": "(W-w)/2:10",
    "bottom-center": "(W-w)/2:H-h-10",
}

# Downloaded model files (face detection, audio classification) live alongside the server.
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
def _enc_threads():
    """Leave the machine usable while it encodes.

    x264 takes every core it can get, which pegs the CPU and makes the desktop stutter
    for the whole render. Holding four back costs about a quarter of the encode time and
    measured no difference in SSIM.
    """
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    return str(max(2, n - 4))


_THREADS = ["-threads", _enc_threads()]

# Delivery encode: compact, because this is what gets uploaded.
VIDEO_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p"] + _THREADS
# Working encode, for files that are themselves going to be re-encoded further down the
# chain. Measured against VIDEO_ENC on a 25s 1080x1920 cut: SSIM 0.99497 vs 0.99501 -
# the same picture - in 5.3s rather than 8.9s. It pays for that with a ~40% bigger file,
# which is free for something that gets deleted, and would not be for a deliverable.
FAST_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-pix_fmt", "yuv420p"] + _THREADS
AUDIO_ENC = ["-c:a", "aac", "-b:a", "192k"]
# Proof renders trade quality for turnaround so a cut can be judged in seconds.
PROOF_ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
             "-vf", "scale=540:-2"]


# ---------------------------------------------------------------- tools
def t_video_info(a):
    path = check_input(a.get("path"), "video")
    info = probe(path)
    fmt = info.get("format", {})
    lines = ["File: %s" % path, "Size: %s" % human_size(path)]
    dur = float(fmt.get("duration", 0) or 0)
    lines.append("Duration: %.2f s  (%d:%02d)" % (dur, int(dur // 60), int(dur % 60)))
    br = fmt.get("bit_rate")
    if br:
        lines.append("Bitrate: %.0f kbps" % (int(br) / 1000.0))
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            fr = s.get("r_frame_rate", "0/1")
            try:
                num, den = fr.split("/")
                fps = float(num) / float(den) if float(den) else 0
            except Exception:
                fps = 0
            lines.append("Video: %sx%s  %.2f fps  codec=%s" %
                         (s.get("width"), s.get("height"), fps, s.get("codec_name")))
        elif s.get("codec_type") == "audio":
            lines.append("Audio: codec=%s  %s Hz  %s ch" %
                         (s.get("codec_name"), s.get("sample_rate"), s.get("channels")))
    if not any(l.startswith("Audio:") for l in lines):
        lines.append("Audio: (none - this video is silent)")
    return "\n".join(lines)


def t_list_media(a):
    folder = a.get("folder") or os.getcwd()
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        raise ToolError("Folder not found: %s" % folder)
    recursive = bool(a.get("recursive"))
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    rows = []
    for root, _dirs, files in walker:
        for f in files:
            if f.lower().endswith(VIDEO_EXTS + AUDIO_EXTS):
                p = os.path.join(root, f)
                rows.append("%s  (%s)" % (p, human_size(p)))
    if not rows:
        return "No video or audio files in %s" % folder
    rows.sort()
    return "Found %d media file(s):\n%s" % (len(rows), "\n".join(rows[:300]))


def t_trim(a):
    src = check_input(a.get("path"), "video")
    start = parse_time(a.get("start"), "start") or 0.0
    end = parse_time(a.get("end"), "end")
    dur = parse_time(a.get("duration"), "duration")
    if end is None and dur is None:
        raise ToolError("Give either 'end' or 'duration'.")
    if end is not None and end <= start:
        raise ToolError("'end' must be after 'start'.")
    out = make_output(src, "trim", a.get("output"))
    args = ["-ss", "%.3f" % start, "-i", src]
    if end is not None:
        args += ["-t", "%.3f" % (end - start)]
    else:
        args += ["-t", "%.3f" % dur]
    if a.get("fast"):
        args += ["-c", "copy"]
    else:
        args += FAST_ENC + AUDIO_ENC
    args += [out]
    ffmpeg_run(args)
    return done(out, "Cut from %.2fs, length %.2fs." % (start, (end - start) if end else dur))


def t_merge(a):
    paths = a.get("paths") or []
    if len(paths) < 2:
        raise ToolError("Give at least 2 video paths in 'paths'.")
    srcs = [check_input(p, "video") for p in paths]
    w, h = ASPECTS.get(a.get("aspect") or "", (None, None))
    if w is None:
        info = probe(srcs[0])
        vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
        w, h = int(vs.get("width", 1920)), int(vs.get("height", 1080))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps") or 30)
    keep_audio = all(has_audio(s) for s in srcs)

    args = []
    for s in srcs:
        args += ["-i", s]
    chains, refs = [], []
    for i in range(len(srcs)):
        chains.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=%d[v%d]" % (i, w, h, w, h, fps, i)
        )
        refs.append("[v%d]" % i)
        if keep_audio:
            chains.append("[%d:a]aresample=48000,asetpts=N/SR/TB[a%d]" % (i, i))
            refs.append("[a%d]" % i)
    n = len(srcs)
    if keep_audio:
        chains.append("%sconcat=n=%d:v=1:a=1[outv][outa]" % ("".join(refs), n))
    else:
        chains.append("%sconcat=n=%d:v=1:a=0[outv]" % ("".join(refs), n))
    out = make_output(srcs[0], "merged", a.get("output"), ".mp4")
    args += ["-filter_complex", ";".join(chains), "-map", "[outv]"]
    if keep_audio:
        args += ["-map", "[outa]"] + AUDIO_ENC
    args += FAST_ENC + [out]
    ffmpeg_run(args)
    note = "Joined %d clips at %dx%d." % (n, w, h)
    if not keep_audio:
        note += " One or more clips had no audio, so the result is silent."
    return done(out, note)


def t_resize(a):
    src = check_input(a.get("path"), "video")
    aspect = a.get("aspect")
    if aspect:
        if aspect not in ASPECTS:
            raise ToolError("aspect must be one of: %s" % ", ".join(ASPECTS))
        w, h = ASPECTS[aspect]
    else:
        w, h = a.get("width"), a.get("height")
        if not w or not h:
            raise ToolError("Give 'aspect' (e.g. 9:16) or both 'width' and 'height'.")
        w, h = int(w), int(h)
    w -= w % 2
    h -= h % 2
    mode = (a.get("mode") or "fill").lower()
    # Lanczos over the default bicubic: on a big upscale - phone footage blown up to
    # 1080x1920 - bicubic leaves hair and small text mushy in a way that is plainly
    # visible side by side. It costs nothing on a downscale, so it is simply the default.
    sc = "scale=%d:%d:flags=lanczos"
    if mode == "fill":
        vf = (sc + ":force_original_aspect_ratio=increase,crop=%d:%d") % (w, h, w, h)
    elif mode == "fit":
        vf = ((sc + ":force_original_aspect_ratio=decrease,"
               "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black") % (w, h, w, h))
    elif mode == "blur":
        vf = (("split[a][b];[a]" + sc + ":force_original_aspect_ratio=increase,"
               "crop=%d:%d,gblur=sigma=25[bg];"
               "[b]" + sc + ":force_original_aspect_ratio=decrease[fg];"
               "[bg][fg]overlay=(W-w)/2:(H-h)/2") % (w, h, w, h, w, h))
    else:
        raise ToolError("mode must be fill, fit, or blur.")

    # Upscaling cannot invent detail, but it does soften the edges that are already
    # there. A touch of unsharp puts that definition back; scale it to how far the
    # picture was stretched, or a 1.1x resize gets the same treatment as a 4x one.
    note = ""
    src_w = int((next((s for s in probe(src).get("streams", [])
                       if s.get("codec_type") == "video"), {}) or {}).get("width") or w)
    ratio = float(w) / max(1, src_w)
    sharpen = a.get("sharpen")
    sharpen = min(1.0, max(0.0, (ratio - 1.0) / 2.5)) if sharpen is None else float(sharpen)
    if sharpen > 0.01:
        vf += ",unsharp=5:5:%.3f:3:3:%.3f" % (0.9 * sharpen, 0.45 * sharpen)
        note = " Upscaled %.1fx, sharpened %.0f%% to restore edge definition." % (ratio, sharpen * 100)

    out = make_output(src, "%dx%d" % (w, h), a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", vf + ",setsar=1"] + FAST_ENC + ["-c:a", "copy", out])
    return done(out, "Resized to %dx%d (%s).%s" % (w, h, mode, note))


def t_crop(a):
    src = check_input(a.get("path"), "video")
    w, h = int(a["width"]), int(a["height"])
    x, y = int(a.get("x", 0)), int(a.get("y", 0))
    out = make_output(src, "crop", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", "crop=%d:%d:%d:%d" % (w, h, x, y)] +
               FAST_ENC + ["-c:a", "copy", out])
    return done(out, "Cropped %dx%d from (%d,%d)." % (w, h, x, y))


def t_speed(a):
    src = check_input(a.get("path"), "video")
    factor = float(a.get("factor") or 0)
    if factor <= 0:
        raise ToolError("'factor' must be > 0 (2 = twice as fast, 0.5 = half speed).")
    filters = ["[0:v]setpts=%.6f*PTS[v]" % (1.0 / factor)]
    maps = ["-map", "[v]"]
    if has_audio(src):
        remaining, chain, idx = factor, [], 0
        label_in = "[0:a]"
        while abs(remaining - 1.0) > 1e-6:
            step = max(0.5, min(2.0, remaining))
            chain.append("%satempo=%.6f[at%d]" % (label_in, step, idx))
            label_in = "[at%d]" % idx
            remaining /= step
            idx += 1
            if idx > 10:
                break
        if chain:
            chain[-1] = chain[-1].rsplit("[at", 1)[0] + "[a]"
            filters += chain
            maps += ["-map", "[a]"]
        else:
            maps += ["-map", "0:a"]
    out = make_output(src, "speed%gx" % factor, a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-filter_complex", ";".join(filters)] + maps +
               FAST_ENC + AUDIO_ENC + [out])
    return done(out, "Speed x%g." % factor)


def t_extract_audio(a):
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This video has no audio track.")
    fmt = (a.get("format") or "mp3").lower()
    out = make_output(src, "audio", a.get("output"), "." + fmt)
    enc = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
           "wav": ["-c:a", "pcm_s16le"],
           "m4a": ["-c:a", "aac", "-b:a", "192k"]}.get(fmt)
    if enc is None:
        raise ToolError("format must be mp3, wav, or m4a.")
    ffmpeg_run(["-i", src, "-vn"] + enc + [out])
    return done(out)


def t_mute(a):
    src = check_input(a.get("path"), "video")
    out = make_output(src, "mute", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-an", "-c:v", "copy", out])
    return done(out, "Audio removed.")


def t_add_music(a):
    src = check_input(a.get("path"), "video")
    music = check_input(a.get("music"), "music")
    vol = float(a.get("volume", 0.3))
    keep = bool(a.get("keep_original_audio", True)) and has_audio(src)
    notes = ""
    out = make_output(src, "music", a.get("output"), ".mp4")
    if keep:
        gain = "volume=%.3f" % vol
        if a.get("dynamic", True):
            # A bed at one flat level sounds like a bed. Real music mixing lifts it in
            # the gaps between lines and pulls it back under speech, so the track feels
            # like it is breathing with the edit rather than running underneath it.
            spans = merge_spans(detect_silence(src, -32, 0.45), gap=0.15)
            swell = float(a.get("swell", 2.1))
            usable = [(s, e) for s, e in spans if e - s > 0.5]
            if usable:
                # Ease in and out of each swell so the level never jumps.
                ramp = 0.35
                terms = []
                for s, e in usable[:40]:
                    terms.append("min(1\\,min((t-%.2f)/%.2f\\,(%.2f-t)/%.2f))*between(t\\,%.2f\\,%.2f)"
                                 % (s, ramp, e, ramp, s, e))
                expr = "+".join(terms)
                gain = ("volume='%.3f*(1+%.3f*max(0\\,min(1\\,%s)))':eval=frame"
                        % (vol, swell - 1.0, expr))
                notes = " Music lifts in %d gap(s) between lines and settles under speech." % len(usable)
        # normalize=0 is essential: amix otherwise divides by the input count and
        # quietly drops the whole mix by ~6 dB.
        # Loop FIRST, then apply the envelope. Applying the gain before the loop makes
        # the expression run on the bed's own short timeline and repeat with it, instead
        # of following the video's timeline where the speech gaps actually are.
        fc = ("[1:a]aloop=loop=-1:size=2e9,%s[bg];"
              "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" % gain)
    else:
        fc = "[1:a]volume=%.3f,aloop=loop=-1:size=2e9,atrim=0:%.3f[a]" % (vol, duration_of(src))
    ffmpeg_run(["-i", src, "-i", music, "-filter_complex", fc,
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-shortest"] + AUDIO_ENC + [out])
    return done(out, "Music added at volume %g%s.%s"
                % (vol, "" if keep else " (original audio replaced)", notes))


def t_subtitles(a):
    src = check_input(a.get("path"), "video")
    subs = check_input(a.get("subtitles"), "subtitle")
    size = int(a.get("font_size", 24))
    # Tahoma ships with Windows and covers Thai + English correctly.
    font = a.get("font") or "Tahoma"
    style = ("FontName=%s,FontSize=%d,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,MarginV=40"
             % (font, size))
    vf = "subtitles='%s':force_style='%s'" % (escape_filter_path(subs), style)
    out = make_output(src, "subs", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", vf] + VIDEO_ENC + ["-c:a", "copy", out])
    return done(out, "Subtitles burned in permanently.")


def t_watermark(a):
    src = check_input(a.get("path"), "video")
    img = check_input(a.get("image"), "image")
    scale = float(a.get("scale", 0.15))
    pos = POSITIONS.get(a.get("position") or "bottom-right")
    if pos is None:
        raise ToolError("position must be one of: %s" % ", ".join(POSITIONS))
    opacity = float(a.get("opacity", 1.0))
    fc = ("[1]scale=iw*%.4f:-1,format=rgba,colorchannelmixer=aa=%.3f[wm];"
          "[0][wm]overlay=%s" % (scale, opacity, pos))
    out = make_output(src, "wm", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-i", img, "-filter_complex", fc] +
               VIDEO_ENC + ["-c:a", "copy", out])
    return done(out, "Watermark placed %s." % (a.get("position") or "bottom-right"))


def t_text(a):
    src = check_input(a.get("path"), "video")
    text = (a.get("text") or "").strip()
    if not text:
        raise ToolError("'text' is empty.")
    size = int(a.get("font_size", 48))
    pos = (a.get("position") or "bottom-center")
    xy = {"top-left": ("40", "40"), "top-center": ("(w-text_w)/2", "40"),
          "top-right": ("w-text_w-40", "40"),
          "center": ("(w-text_w)/2", "(h-text_h)/2"),
          "bottom-left": ("40", "h-text_h-40"),
          "bottom-center": ("(w-text_w)/2", "h-text_h-40"),
          "bottom-right": ("w-text_w-40", "h-text_h-40")}.get(pos)
    if xy is None:
        raise ToolError("position must be one of: %s" % ", ".join(POSITIONS))

    tmp = _tmpdir()
    opts = dict(fontsize=size, fontcolor=a.get("color") or "white",
                borderw=3, bordercolor="black@0.8", x=xy[0], y=xy[1])
    start = parse_time(a.get("start"), "start")
    end = parse_time(a.get("end"), "end")
    if start is not None or end is not None:
        opts["enable"] = "'between(t\\,%.3f\\,%.3f)'" % (
            start or 0, end if end is not None else duration_of(src))
    fontfile = r"C:\Windows\Fonts\tahoma.ttf"
    if os.path.isfile(fontfile):
        # A PATH wants escape_filter_path (slashes flipped, colon escaped), not
        # esc_expr - the latter doubles backslashes. It must also be QUOTED: an
        # escaped colon in a bare value still ends the option.
        opts["fontfile"] = "'%s'" % escape_filter_path(fontfile)
    d = drawtext_of(text, tmp, **opts)

    out = make_output(src, "text", a.get("output"), ".mp4")
    # cwd is the temp folder so the textfile can be named without a path.
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src),
                        "-vf", d] + VIDEO_ENC + ["-c:a", "copy", os.path.abspath(out)],
                       cwd=tmp, capture_output=True, text=True)
    if p.returncode:
        raise ToolError("Adding text failed:\n" + (p.stderr or "").strip()[-400:])
    return done(out, "Text added: %s" % text[:60])


def t_to_gif(a):
    src = check_input(a.get("path"), "video")
    fps = int(a.get("fps", 12))
    width = int(a.get("width", 480))
    start = parse_time(a.get("start"), "start")
    dur = parse_time(a.get("duration"), "duration")
    pre = []
    if start:
        pre += ["-ss", "%.3f" % start]
    post = ["-t", "%.3f" % dur] if dur else []
    fc = ("fps=%d,scale=%d:-1:flags=lanczos,split[s0][s1];"
          "[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=bayer" % (fps, width))
    out = make_output(src, "gif", a.get("output"), ".gif")
    ffmpeg_run(pre + ["-i", src] + post + ["-filter_complex", fc, "-loop", "0", out])
    return done(out)


def t_thumbnail(a):
    src = check_input(a.get("path"), "video")
    at = parse_time(a.get("at"), "at")
    if at is None:
        at = max(0.0, duration_of(src) * 0.1)
    out = make_output(src, "thumb", a.get("output"), ".jpg")
    ffmpeg_run(["-ss", "%.3f" % at, "-i", src, "-frames:v", "1", "-q:v", "2", out])
    return done(out, "Frame grabbed at %.2fs." % at)


def t_compress(a):
    src = check_input(a.get("path"), "video")
    level = (a.get("level") or "medium").lower()
    crf = {"light": 22, "medium": 26, "strong": 30, "extreme": 34}.get(level)
    if crf is None:
        raise ToolError("level must be light, medium, strong, or extreme.")
    before = os.path.getsize(src)
    out = make_output(src, "small", a.get("output"), ".mp4")
    args = ["-i", src]
    if a.get("max_width"):
        args += ["-vf", "scale='min(%d,iw)':-2" % int(a["max_width"])]
    args += ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    ffmpeg_run(args)
    after = os.path.getsize(out)
    pct = (1 - after / float(before)) * 100 if before else 0
    return done(out, "Shrunk by %.0f%% (%s -> %s)." % (pct, human_size(src), human_size(out)))


def t_rotate(a):
    src = check_input(a.get("path"), "video")
    d = (a.get("direction") or "right").lower()
    vf = {"right": "transpose=1", "left": "transpose=2", "180": "transpose=1,transpose=1",
          "flip-horizontal": "hflip", "flip-vertical": "vflip"}.get(d)
    if vf is None:
        raise ToolError("direction must be right, left, 180, flip-horizontal, or flip-vertical.")
    out = make_output(src, "rot", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", vf, "-metadata:s:v", "rotate=0"] +
               VIDEO_ENC + ["-c:a", "copy", out])
    return done(out, "Rotated %s." % d)


def t_fade(a):
    src = check_input(a.get("path"), "video")
    dur = duration_of(src)
    fin = float(a.get("fade_in", 1.0))
    fout = float(a.get("fade_out", 1.0))
    vf, af = [], []
    if fin > 0:
        vf.append("fade=t=in:st=0:d=%.3f" % fin)
        af.append("afade=t=in:st=0:d=%.3f" % fin)
    if fout > 0 and dur > fout:
        vf.append("fade=t=out:st=%.3f:d=%.3f" % (dur - fout, fout))
        af.append("afade=t=out:st=%.3f:d=%.3f" % (dur - fout, fout))
    if not vf:
        raise ToolError("Set fade_in and/or fade_out to a positive number of seconds.")
    args = ["-i", src, "-vf", ",".join(vf)]
    if has_audio(src) and af:
        args += ["-af", ",".join(af)] + AUDIO_ENC
    else:
        args += ["-c:a", "copy"] if has_audio(src) else ["-an"]
    out = make_output(src, "fade", a.get("output"), ".mp4")
    ffmpeg_run(args + VIDEO_ENC + [out])
    return done(out, "Fade in %.1fs / out %.1fs." % (fin, fout))


def t_ffmpeg_raw(a):
    args = a.get("args")
    if not isinstance(args, list) or not args:
        raise ToolError("'args' must be a non-empty list of FFmpeg arguments (no 'ffmpeg' itself).")
    args = [str(x) for x in args]
    err = ffmpeg_run(args)
    tail = "\n".join([l for l in err.strip().splitlines() if l.strip()][-10:])
    return "FFmpeg finished.\n" + tail


# ---------------------------------------------------------------- auto-cut engine
# ---------------------------------------------------------------- analysis cache
# Shot detection, silence detection and loudness measurement dominate the wall clock -
# far more than encoding. They also depend only on the file's bytes, so results are
# cached on disk keyed by path + mtime + size. Editing is iterative: the second run
# over the same footage should not pay for the same analysis again.
def _cachedir():
    d = os.path.join(_tmpdir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(kind, path, params):
    """Identify a file by its CONTENT, not its name or timestamp.

    Intermediate segments are written to a fresh temp path on every run, so keying on
    path+mtime never hits for exactly the work that costs most (two-pass loudnorm per
    segment). Sampling the head and tail plus the size is fast and stable: re-rendering
    the same cut produces byte-identical segments, so the measurement is reused.
    """
    import hashlib
    h = hashlib.sha1()
    try:
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with io.open(path, "rb") as fh:
            h.update(fh.read(262144))
            if size > 524288:
                fh.seek(-262144, os.SEEK_END)
                h.update(fh.read(262144))
    except OSError:
        h.update(b"missing")
    h.update(("%s|%s" % (kind, repr(params))).encode("utf-8"))
    return h.hexdigest()


def cached(kind, path, params, compute):
    key = _cache_key(kind, path, params)
    f = os.path.join(_cachedir(), key + ".json")
    if os.path.isfile(f):
        try:
            return json.load(io.open(f, encoding="utf-8"))["v"]
        except (ValueError, OSError, KeyError):
            pass
    value = compute()
    try:
        with io.open(f, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": value}))
    except OSError:
        pass
    return value


def t_cache_clear(a):
    """Drop cached analysis. Rarely needed - entries key off the file's own timestamp."""
    d = _cachedir()
    n = 0
    for f in os.listdir(d):
        if f.endswith(".json"):
            try:
                os.remove(os.path.join(d, f))
                n += 1
            except OSError:
                pass
    return ("Cleared %d cached analysis result(s).\n"
            "Entries include each file's timestamp and size, so editing a video already "
            "invalidates its own cache - this is only for forcing a full re-analysis." % n)


def _probe_stderr(src, args):
    """Run an analysis-only FFmpeg pass and return its stderr."""
    _require_ffmpeg()
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", src] + args + ["-f", "null", "-"]
    try:
        code, out, err = run(cmd)
    except subprocess.TimeoutExpired:
        raise ToolError("Analysis timed out - the video is very long.")
    if code != 0:
        tail = "\n".join([l for l in err.strip().splitlines() if l.strip()][-8:])
        raise ToolError("Could not analyse this video:\n" + tail)
    return err


def detect_silence(src, db=-30, min_dur=0.5):
    def compute():
        err = _probe_stderr(src, ["-af", "silencedetect=noise=%ddB:d=%.3f" % (db, min_dur)])
        spans, start = [], None
        for line in err.splitlines():
            m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
            if m:
                start = max(0.0, float(m.group(1)))
                continue
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m and start is not None:
                spans.append((start, float(m.group(1))))
                start = None
        if start is not None:
            spans.append((start, duration_of(src)))
        return spans
    return [tuple(s) for s in cached("silence", src, (db, min_dur), compute)]


def detect_black(src, min_dur=0.5):
    err = _probe_stderr(src, ["-vf", "blackdetect=d=%.3f:pix_th=0.10" % min_dur])
    return [(float(a), float(b)) for a, b in
            re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", err)]


def detect_freeze(src, min_dur=2.0):
    err = _probe_stderr(src, ["-vf", "freezedetect=n=-60dB:d=%.3f" % min_dur])
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", err)]
    total = duration_of(src)
    spans = []
    for i, s in enumerate(starts):
        spans.append((s, ends[i] if i < len(ends) else total))
    return spans


def detect_blur(src, threshold=30.0, sample_fps=2):
    err = _probe_stderr(src, ["-vf", "fps=%d,blurdetect,metadata=print:key=lavfi.blur" % sample_fps])
    times, vals, t = [], [], None
    for line in err.splitlines():
        # pts_time is already in seconds after the fps filter - do not rescale it.
        m = re.search(r"pts_time:\s*([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"lavfi\.blur=([\d.]+)", line)
        if m and t is not None:
            times.append(t)
            vals.append(float(m.group(1)))
            t = None
    step = 1.0 / max(1, sample_fps)
    spans, run_start = [], None
    for i, v in enumerate(vals):
        if v >= threshold and run_start is None:
            run_start = times[i]
        elif v < threshold and run_start is not None:
            spans.append((run_start, times[i]))
            run_start = None
    if run_start is not None:
        spans.append((run_start, min(times[-1] + step, duration_of(src))))
    return spans


def _motion_curve(src, fps=10):
    """Frame-to-frame difference over time - a rough map of where the movement is."""
    st = _signalstats(src, sample_fps=fps)
    return st.get("YDIF") or []


def snap_to_action(src, t, window=0.35, fps=10):
    """Move a cut point to the nearest peak of movement.

    Cutting mid-movement is the oldest trick in editing: the motion carries across
    the join and the eye rides over it. Cutting on a still frame draws attention to
    the cut itself.
    """
    curve = _motion_curve(src, fps)
    if not curve:
        return t, False
    step = 1.0 / fps
    lo = max(0, int((t - window) / step))
    hi = min(len(curve) - 1, int((t + window) / step))
    if hi <= lo:
        return t, False
    best_i = max(range(lo, hi + 1), key=lambda i: curve[i])
    best_t = best_i * step
    # Only move if the peak is meaningfully busier than the original point.
    here = curve[min(len(curve) - 1, max(0, int(t / step)))]
    if curve[best_i] <= here * 1.15 or abs(best_t - t) < step:
        return t, False
    return best_t, True


def merge_spans(spans, gap=0.25):
    spans = sorted([(float(s), float(e)) for s, e in spans if e > s])
    out = []
    for s, e in spans:
        if out and s <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def invert_spans(bad, total, min_keep=0.2):
    keeps, pos = [], 0.0
    for s, e in bad:
        if s - pos >= min_keep:
            keeps.append((pos, s))
        pos = max(pos, e)
    if total - pos >= min_keep:
        keeps.append((pos, total))
    return keeps


def cut_keeps(src, keeps, out):
    """Re-assemble a video from a list of (start, end) segments to keep."""
    if not keeps:
        raise ToolError("Nothing would be left after cutting - loosen the settings.")
    if len(keeps) > 400:
        raise ToolError("Found %d separate pieces - too choppy. Raise 'min_silence' "
                        "or lower the sensitivity." % len(keeps))
    audio = has_audio(src)
    parts, refs = [], []
    for i, (s, e) in enumerate(keeps):
        parts.append("[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[v%d]" % (s, e, i))
        refs.append("[v%d]" % i)
        if audio:
            parts.append("[0:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d]" % (s, e, i))
            refs.append("[a%d]" % i)
    n = len(keeps)
    parts.append("%sconcat=n=%d:v=1:a=%d%s" % (
        "".join(refs), n, 1 if audio else 0, "[outv][outa]" if audio else "[outv]"))
    script = os.path.join(os.path.dirname(out), ".filter_%d.txt" % os.getpid())
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(parts))
    try:
        args = ["-i", src, "-filter_complex_script", script, "-map", "[outv]"]
        if audio:
            args += ["-map", "[outa]"] + AUDIO_ENC
        args += VIDEO_ENC + [out]
        ffmpeg_run(args)
    finally:
        try:
            os.remove(script)
        except OSError:
            pass


def fmt_span(s, e):
    f = lambda t: "%d:%05.2f" % (int(t // 60), t % 60)
    return "%s - %s (%.1fs)" % (f(s), f(e), e - s)


def t_find_problems(a):
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    report = {}
    if a.get("check_silence", True) and has_audio(src):
        report["Silent / dead air"] = detect_silence(
            src, int(a.get("silence_db", -30)), float(a.get("min_silence", 0.8)))
    if a.get("check_black", True):
        report["Black screen"] = detect_black(src, float(a.get("min_black", 0.5)))
    if a.get("check_freeze", True):
        report["Frozen picture"] = detect_freeze(src, float(a.get("min_freeze", 2.0)))
    if a.get("check_blur", True):
        report["Blurry / out of focus"] = detect_blur(
            src, float(a.get("blur_threshold", 30.0)))

    lines = ["Checked: %s  (%.1fs total)" % (os.path.basename(src), total), ""]
    found = 0
    for label, spans in report.items():
        spans = merge_spans(spans)
        if not spans:
            lines.append("%s: none found" % label)
            continue
        found += len(spans)
        bad_time = sum(e - s for s, e in spans)
        lines.append("%s: %d spot(s), %.1fs total" % (label, len(spans), bad_time))
        for s, e in spans[:25]:
            lines.append("    %s" % fmt_span(s, e))
        if len(spans) > 25:
            lines.append("    ... and %d more" % (len(spans) - 25))
    lines.append("")
    lines.append("Nothing was changed - this is only a report. "
                 "Use video_auto_cut to actually remove these parts."
                 if found else "This video looks clean.")
    return "\n".join(lines)


def t_auto_cut(a):
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    pad = float(a.get("padding", 0.15))
    bad, notes = [], []

    if a.get("remove_silence", True) and has_audio(src):
        spans = detect_silence(src, int(a.get("silence_db", -30)),
                               float(a.get("min_silence", 0.8)))
        bad += spans
        notes.append("silence: %d" % len(merge_spans(spans)))
    if a.get("remove_black", True):
        spans = detect_black(src, float(a.get("min_black", 0.5)))
        bad += spans
        notes.append("black: %d" % len(merge_spans(spans)))
    if a.get("remove_freeze", True):
        spans = detect_freeze(src, float(a.get("min_freeze", 2.0)))
        bad += spans
        notes.append("frozen: %d" % len(merge_spans(spans)))
    if a.get("remove_blurry", False):
        spans = detect_blur(src, float(a.get("blur_threshold", 30.0)))
        bad += spans
        notes.append("blurry: %d" % len(merge_spans(spans)))

    bad = merge_spans(bad)
    if not bad:
        return ("Nothing to cut - no silence, black frames, freezes%s found. "
                "Original left untouched." % (" or blur" if a.get("remove_blurry") else ""))
    # Shrink each bad span by the padding so cuts do not clip speech.
    padded = [(s + pad, e - pad) for s, e in bad]
    padded = [(s, e) for s, e in padded if e > s]
    keeps = invert_spans(merge_spans(padded), total)
    out = make_output(src, "autocut", a.get("output"), ".mp4")
    cut_keeps(src, keeps, out)
    new_total = duration_of(out)
    return done(out, "Removed %d bad part(s) [%s]. %.1fs -> %.1fs (saved %.1fs). "
                     "Original untouched." %
                (len(bad), ", ".join(notes), total, new_total, total - new_total))


def t_remove_silence(a):
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This video has no audio, so there is no silence to find.")
    total = duration_of(src)
    pad = float(a.get("padding", 0.15))
    spans = merge_spans(detect_silence(src, int(a.get("silence_db", -30)),
                                       float(a.get("min_silence", 0.6))))
    if not spans:
        return "No silence longer than %.2fs found. Nothing to cut." % float(a.get("min_silence", 0.6))
    padded = [(s + pad, e - pad) for s, e in spans]
    keeps = invert_spans(merge_spans([(s, e) for s, e in padded if e > s]), total)
    out = make_output(src, "tight", a.get("output"), ".mp4")
    cut_keeps(src, keeps, out)
    new_total = duration_of(out)
    return done(out, "Cut %d silent gap(s) into jump cuts. %.1fs -> %.1fs (saved %.1fs)." %
                (len(spans), total, new_total, total - new_total))


# ---------------------------------------------------------------- speech to text
_WHISPER_MODELS = {}


def _load_whisper(size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ToolError("faster-whisper is not installed. Run:\n    pip install faster-whisper")
    if size not in _WHISPER_MODELS:
        try:
            _WHISPER_MODELS[size] = WhisperModel(size, device="cpu", compute_type="int8")
        except Exception as e:
            raise ToolError(
                "Could not load the '%s' speech model: %s\n"
                "The first use downloads it (small ~500MB, medium ~1.5GB, large-v3 ~3GB) "
                "and needs an internet connection." % (size, e))
    return _WHISPER_MODELS[size]


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


# Thai vowels and tone marks that hang off the previous consonant. Breaking a line
# between a consonant and these produces orphaned marks like "ท" + "ำได้".
# U+0E31-U+0E3A: MAI HAN AKAT, SARA AA/AM and the above/below vowels.
# U+0E45-U+0E4E: LAKKHANGYAO, tone marks, THANTHAKHAT, NIKHAHIT.
# None of these may begin a line or a caption - they belong to the consonant before them.
THAI_COMBINING = frozenset(
    chr(c) for c in list(range(0x0E31, 0x0E3B)) + list(range(0x0E45, 0x0E4F))
)


# เ แ โ ใ ไ are written BEFORE the consonant they belong to, so a line must never
# end on one - "ใคร" split after "ใ" leaves a stranded vowel on the previous line.
THAI_LEADING = frozenset("เแโใไ")


def _visible_len(text):
    """How wide a line actually renders.

    Thai tone marks and upper/lower vowels stack onto the consonant before them and
    take no width of their own, so counting characters overstates a Thai line by a
    third and makes a line that fits look like it overflows.
    """
    return sum(1 for ch in (text or "") if ch not in THAI_COMBINING)


def _safe_break(text, i):
    """Nudge a break point to somewhere it will not split a cluster."""
    while i < len(text) and text[i] in THAI_COMBINING:
        i += 1
    while i > 0 and text[i - 1] in THAI_LEADING:
        i -= 1
    return i


def _thai_words(text):
    """Segment Thai into words if pythainlp is installed, else return None.

    Thai writes no spaces, so without a dictionary any line break is a guess. This
    is optional on purpose - the editor works without it, just with rougher wrapping.
    """
    try:
        from pythainlp.tokenize import word_tokenize
    except ImportError:
        return None
    try:
        return [w for w in word_tokenize(text, engine="newmm") if w]
    except Exception:
        return None


def wrap_tokens(tokens, limit):
    """Wrap on the speech recogniser's own token boundaries.

    Thai has no spaces, so wrapping by character count breaks words apart
    ("ใคร" becomes "ใค" + "ร"). Whisper's tokens follow syllables, which is a far
    better place to break than an arbitrary character position.
    """
    lines, cur = [], ""
    for tok in tokens:
        t = tok if cur else tok.lstrip()
        if cur and len(cur) + len(t) > limit:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur += t
    if cur.strip():
        lines.append(cur.rstrip())
    return "\n".join(lines)


def enforce_width(text, limit):
    """Last line of defence: no output line may exceed the limit.

    Wrapping happens in several places and a line slipping through renders off both
    edges of the frame, which is the single most visible caption failure. This runs
    on the finished text, splits only lines that are still too long, and never
    rejoins lines that were already broken deliberately.
    """
    out = []
    for line in text.split("\n"):
        if len(line) <= limit:
            out.append(line)
            continue
        words = _thai_words(line)
        pieces = wrap_tokens(words, limit).split("\n") if words else []
        if not pieces or any(len(p) > limit for p in pieces):
            pieces, i = [], 0
            while i < len(line):
                j = _safe_break(line, min(i + limit, len(line)))
                if j <= i:
                    j = min(i + limit, len(line))
                pieces.append(line[i:j])
                i = j
        out.extend(pieces)
    return "\n".join(p for p in out if p)


def _split_chunk(chunk, limit):
    """Break one space-free run into pieces that each fit the limit."""
    if len(chunk) <= limit:
        return [chunk]
    words = _thai_words(chunk)
    if words and all(len(w) <= limit for w in words):
        return wrap_tokens(words, limit).split("\n")
    pieces, i = [], 0
    while i < len(chunk):
        j = _safe_break(chunk, min(i + limit, len(chunk)))
        if j <= i:                      # safety: never fail to advance
            j = min(i + limit, len(chunk))
        pieces.append(chunk[i:j])
        i = j
    return pieces


def wrap_line(text, limit):
    """Wrap to the limit, preferring spaces but never trusting them alone.

    Whisper sprinkles spaces into Thai at phrase boundaries. Treating those spaces as
    the only break points - the way ordinary word wrapping does - leaves any chunk
    between them intact, and a Thai chunk is routinely longer than a whole caption
    line. That produced captions running off both edges of the frame. So each chunk is
    itself broken down first, then the pieces are packed into lines.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    tokens = []
    for i, chunk in enumerate(text.split(" ")):
        for j, piece in enumerate(_split_chunk(chunk, limit)):
            tokens.append((" " if (i and j == 0) else "") + piece)
    return wrap_tokens(tokens, limit)


def t_auto_subtitles(a):
    import contextlib
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This video has no sound, so there is nothing to transcribe.")
    lang = (a.get("language") or "auto").strip().lower()
    size = a.get("model") or "small"
    if size not in ("tiny", "base", "small", "medium", "large-v3"):
        raise ToolError("model must be tiny, base, small, medium or large-v3.")
    if lang not in ("auto", "en") and size.endswith(".en"):
        raise ToolError("English-only model cannot do %s. Use small, medium or large-v3." % lang)
    translate = bool(a.get("translate_to_english"))
    limit = int(a.get("max_chars_per_line", 42))

    max_cue = float(a.get("max_seconds_per_line", 3.0))
    model = _load_whisper(size)
    # faster-whisper prints progress; keep it off stdout or it corrupts the protocol.
    with contextlib.redirect_stdout(sys.stderr):
        segments, info = model.transcribe(
            src,
            language=None if lang == "auto" else lang,
            task="translate" if translate else "transcribe",
            vad_filter=True,
            beam_size=5,
            word_timestamps=True,
        )
        segments = list(segments)

    if not segments:
        raise ToolError("No speech was found in this video.")

    # Whisper returns segments up to ~10s long. Holding one caption that long is
    # unreadable, so regroup the word timings into short cues.
    pre_wrapped = False
    raw = [w for seg in segments for w in (getattr(seg, "words", None) or [])]
    # Whisper's Thai tokenizer can emit a bare consonant and its vowel mark as two
    # separate "words" ("ท" then "ำ"). Glue those back together first.
    words = []
    for w in raw:
        txt = w.word or ""
        if words and txt and txt[0] in THAI_COMBINING:
            words[-1][1] = w.end
            words[-1][2] += txt
        else:
            words.append([w.start, w.end, txt])
    words = [type("W", (), {"start": s, "end": e, "word": t})()
             for s, e, t in words if t.strip()]

    if words:
        gap_break = float(a.get("sentence_gap", 0.45))
        MICRO = 0.08          # smallest gap that is safe to cut on

        def flush(buf):
            # Keep the tokens, not just the joined string, so the line wrapping
            # downstream can break on syllable boundaries instead of characters.
            return (buf[0]["start"], buf[-1]["end"], [x["word"] for x in buf])

        cues, buf = [], []
        for w in words:
            gap = (w.start - buf[-1]["end"]) if buf else 0.0
            if buf and gap > gap_break:          # a real pause: always a clean break
                cues.append(flush(buf))
                buf, gap = [], 0.0
            buf.append({"start": w.start, "end": w.end, "word": w.word, "gap": gap})
            # Over length: split at the LAST micro-gap rather than at the current word,
            # so a break can never land inside a word like "อ๋อ" (whose parts have no
            # gap between them at all).
            if buf[-1]["end"] - buf[0]["start"] > max_cue:
                idx = next((i for i in range(len(buf) - 1, 0, -1)
                            if buf[i]["gap"] >= MICRO), None)
                if idx is None and buf[-1]["end"] - buf[0]["start"] > max_cue * 1.6:
                    idx = len(buf)               # continuous speech: take it all
                if idx:
                    cues.append(flush(buf[:idx]))
                    buf = buf[idx:]
        if buf:
            cues.append(flush(buf))
        cues = [c for c in cues if "".join(c[2]).strip()]

        # A sub-second cue flashes past unreadably; fold it into its neighbour.
        merged = []
        for s, e, toks in cues:
            if merged and (e - s) < 0.8 and (e - merged[-1][0]) <= max_cue * 1.8:
                ps, _pe, ptoks = merged[-1]
                merged[-1] = (ps, e, ptoks + toks)
            else:
                merged.append((s, e, toks))
        # A cue boundary can also land inside a word ("กิน" split as "กิ" | "น"), because
        # the micro-gaps it breaks on are between syllables, not words. When a segmenter
        # is available, re-flow the text so each whole word sits in exactly one cue.
        joined_all = "".join("".join(toks) for _s, _e, toks in merged)
        seg_words = _thai_words(joined_all) if joined_all else None
        if seg_words and len(merged) > 1:
            spans, pos = [], 0
            for _s, _e, toks in merged:
                n = len("".join(toks))
                spans.append((pos, pos + n))
                pos += n
            texts = [""] * len(merged)
            wpos = 0
            for word in seg_words:
                mid = wpos + len(word) / 2.0
                idx = len(merged) - 1
                for i, (lo, hi) in enumerate(spans):
                    if lo <= mid < hi:
                        idx = i
                        break
                texts[idx] += word
                wpos += len(word)
            merged = [(merged[i][0], merged[i][1], [texts[i]])
                      for i in range(len(merged)) if texts[i].strip()]

        # Three or four lines covers the picture and reads as a wall of text - two is
        # the practical limit. When a cue needs more, split it in TIME instead, sharing
        # the duration between the halves rather than stacking lines up the frame.
        max_lines = max(1, int(a.get("max_lines", 2)))
        final = []
        for s, e, toks in merged:
            text = wrap_line("".join(toks).strip(), limit)
            lines = [l for l in text.split("\n") if l]
            if len(lines) <= max_lines or (e - s) < 0.6:
                final.append((s, e, text))
                continue
            chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
            span = (e - s) / float(len(chunks))
            for k, chunk in enumerate(chunks):
                final.append((s + k * span, s + (k + 1) * span, "\n".join(chunk)))
        segments = [type("Cue", (), {"start": s, "end": e, "text": t})()
                    for s, e, t in final if t]
        pre_wrapped = True

    srt_path = a.get("srt_output") or os.path.splitext(
        make_output(src, "subs", None, ".srt"))[0] + ".srt"
    parent = os.path.dirname(srt_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    overflows = []
    with io.open(srt_path, "w", encoding="utf-8-sig") as fh:
        for i, seg in enumerate(segments, 1):
            # Token-wrapped cues already carry correct breaks; re-wrapping by
            # character count would undo them and split words again.
            text = seg.text.strip() if pre_wrapped else wrap_line(seg.text.strip(), limit)
            # Safety net. The known cause - space-separated chunks never being broken
            # down - is fixed in wrap_line, so this should now never fire; keep the
            # check and report it, because a silent net would hide the next such bug.
            over = [l for l in text.split("\n") if len(l) > limit]
            if over:
                overflows.append((i, max(len(l) for l in over)))
            text = enforce_width(text, limit)
            if not text:
                continue
            fh.write("%d\n%s --> %s\n%s\n\n" %
                     (i, srt_time(seg.start), srt_time(seg.end), text))

    detected = getattr(info, "language", lang)
    note = "Heard %d line(s) of %s speech." % (len(segments), detected)
    if overflows:
        note += ("\nWARNING: %d cue(s) left the wrapper too wide (longest %d chars vs a limit "
                 "of %d) and had to be re-split: cue %s. The captions written are correct, but "
                 "this should no longer happen - it means a new wrapping case has appeared. "
                 "Worth reporting."
                 % (len(overflows), max(w for _i, w in overflows), limit,
                    ", ".join(str(i) for i, _w in overflows)))
    if not a.get("burn", True):
        return "Subtitle file written -> %s\n%s" % (srt_path, note)

    burned = t_subtitles({"path": src, "subtitles": srt_path,
                          "font_size": a.get("font_size", 24),
                          "font": a.get("font"),
                          "output": a.get("output")})
    return "%s\nSubtitle file also saved -> %s\n%s" % (burned, srt_path, note)


# ---------------------------------------------------------------- effects
def _fx(name, i, w, h, fps=30.0):
    """Return an FFmpeg filter chain for an effect. i = intensity 0..1."""
    if name == "black_and_white":
        return "hue=s=0,eq=contrast=%.3f" % (1 + 0.3 * i)
    if name == "sepia":
        return ("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
    if name == "vintage":
        return "curves=vintage,vignette=PI/%.2f,noise=alls=%d:allf=t" % (5 - 2 * i, int(4 + 14 * i))
    if name == "cinematic":
        return ("curves=r='0/0.04 0.5/0.5 1/0.96':b='0/0.06 0.5/0.52 1/1',"
                "eq=contrast=%.3f:saturation=%.3f" % (1 + 0.25 * i, 1 + 0.2 * i))
    if name == "vivid":
        return "eq=saturation=%.3f:contrast=%.3f" % (1 + 0.9 * i, 1 + 0.25 * i)
    if name == "warm":
        return "colorbalance=rs=%.3f:rm=%.3f:bs=-%.3f:bm=-%.3f" % (0.2 * i, 0.15 * i, 0.15 * i, 0.1 * i)
    if name == "cold":
        return "colorbalance=bs=%.3f:bm=%.3f:rs=-%.3f:rm=-%.3f" % (0.2 * i, 0.15 * i, 0.15 * i, 0.1 * i)
    if name == "vignette":
        return "vignette=PI/%.2f" % (6 - 3 * i)
    if name == "sharpen":
        return "unsharp=5:5:%.3f:5:5:%.3f" % (1.6 * i, 0.6 * i)
    if name == "dreamy":
        return ("split[fa][fb];[fb]gblur=sigma=%.1f[fbl];"
                "[fa][fbl]blend=all_mode=screen:all_opacity=%.3f" % (8 + 14 * i, 0.25 + 0.4 * i))
    if name == "film_grain":
        return "noise=alls=%d:allf=t" % int(4 + 20 * i)
    if name == "vhs":
        return ("chromashift=cbh=%d:crh=-%d,noise=alls=%d:allf=t,eq=saturation=%.2f:contrast=1.1"
                % (int(2 + 6 * i), int(2 + 6 * i), int(6 + 18 * i), 1 + 0.4 * i))
    if name == "glitch":
        # chromashift takes no time expressions, so the jitter comes from crop instead.
        j = int(6 + 24 * i)
        return ("crop=iw-%d:ih:'%d+%d*sin(t*40)':0,scale=%d:%d,"
                "rgbashift=rh=%d:bh=-%d,noise=alls=%d:allf=t"
                % (j * 2, j, j, w, h, int(3 + 9 * i), int(3 + 9 * i), int(6 + 20 * i)))
    if name == "night_vision":
        return ("hue=s=0,colorchannelmixer=0:0:0:0:%.2f:%.2f:%.2f:0:0:0:0:0,"
                "eq=contrast=%.2f,noise=alls=%d:allf=t"
                % (0.25, 0.75, 0.15, 1 + 0.5 * i, int(6 + 14 * i)))
    if name == "blur_background":
        return "gblur=sigma=%.1f" % (2 + 18 * i)
    if name == "zoom_in":
        # fps must be the SOURCE rate - zoompan relabels frames rather than resampling,
        # so any other value rescales the clip's length against its own audio.
        return ("scale=%d:%d,zoompan=z='min(zoom+%.5f,%.3f)':d=1:"
                "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%0.6f"
                % (w * 2, h * 2, 0.0004 + 0.0008 * i, 1.1 + 0.4 * i, w, h, fps))
    if name == "shake":
        amp = int(4 + 16 * i)
        return ("crop=iw-%d:ih-%d:'%d+%d*sin(t*11)':'%d+%d*cos(t*17)',scale=%d:%d"
                % (amp * 2, amp * 2, amp, amp, amp, amp, w, h))
    if name == "mirror":
        return "crop=iw/2:ih:0:0,split[ml][mr];[mr]hflip[mrf];[ml][mrf]hstack,scale=%d:%d" % (w, h)
    raise ToolError("Unknown effect. Choose one of: %s" % ", ".join(EFFECT_NAMES))


EFFECT_NAMES = ["cinematic", "vintage", "black_and_white", "sepia", "vivid", "warm", "cold",
                "vignette", "sharpen", "dreamy", "film_grain", "vhs", "glitch", "night_vision",
                "blur_background", "zoom_in", "shake", "mirror"]


def t_effect(a):
    src = check_input(a.get("path"), "video")
    names = a.get("effect")
    if isinstance(names, str):
        names = [names]
    if not names:
        raise ToolError("Give an 'effect'. Options: %s" % ", ".join(EFFECT_NAMES))
    i = float(a.get("intensity", 0.5))
    if not 0 < i <= 1:
        raise ToolError("intensity must be between 0 and 1.")
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1920)), int(vs.get("height", 1080))
    w -= w % 2
    h -= h % 2
    chain = ",".join(_fx(n, i, w, h, fps_of(src)) for n in names)
    out = make_output(src, "fx_" + "_".join(names)[:30], a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-filter_complex", "[0:v]" + chain + "[outv]",
                "-map", "[outv]"] +
               (["-map", "0:a", "-c:a", "copy"] if has_audio(src) else []) +
               VIDEO_ENC + [out])
    return done(out, "Applied: %s (intensity %.2f)." % (", ".join(names), i))


def _loudnorm_filter(src, target, tp, denoise):
    """Build a two-pass loudnorm filter. Single-pass drifts by several dB on short
    clips, which makes levels jump between joined clips - so measure first."""
    pre = "afftdn=nf=-25," if denoise else ""
    base = "loudnorm=I=%.2f:TP=%.2f:LRA=11" % (target, tp)
    err = _probe_stderr(src, ["-af", pre + base + ":print_format=json"])
    block = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not block:
        return pre + base  # fall back to single pass rather than failing outright
    try:
        m = json.loads(block.group(0))
        measured = (":measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s"
                    ":offset=%s:linear=true:print_format=summary"
                    % (m["input_i"], m["input_tp"], m["input_lra"],
                       m["input_thresh"], m.get("target_offset", "0.0")))
    except (ValueError, KeyError):
        return pre + base
    return pre + base + measured


TRANSITIONS = ["fade", "fadeblack", "fadewhite", "dissolve", "smoothleft", "smoothright",
               "smoothup", "smoothdown", "wipeleft", "wiperight", "slideleft", "slideright",
               "circleopen", "circleclose", "radial", "pixelize", "hblur"]


def t_join_smooth(a):
    """Join clips with a transition instead of a hard cut. A dissolve also hides a
    tightened cut, so trimmed dead air stops reading as a jump."""
    paths = a.get("paths") or []
    if len(paths) < 2:
        raise ToolError("Give at least 2 clips in 'paths'.")
    srcs = [check_input(p, "video") for p in paths]
    trans = a.get("transition") or "fade"
    if trans not in TRANSITIONS:
        raise ToolError("transition must be one of: %s" % ", ".join(TRANSITIONS))
    d = float(a.get("duration", 0.5))
    # Audio may cross over a LONGER window than the picture. When it does, the next
    # shot is heard before it is seen (a J-cut) or the last one lingers over the new
    # picture (an L-cut). That overlap is what makes a cut feel invisible.
    a_cross = float(a.get("audio_crossfade", 0) or 0) or d

    # Per-junction control: a hard cut inside a scene, a dissolve when the scene changes.
    junctions = a.get("junctions")
    if junctions and len(junctions) != len(srcs) - 1:
        raise ToolError("'junctions' needs one entry per join (%d), got %d."
                        % (len(srcs) - 1, len(junctions)))
    plan = []
    for i in range(len(srcs) - 1):
        j = (junctions[i] if junctions else {}) or {}
        jt = j.get("transition") or trans
        if jt not in TRANSITIONS:
            raise ToolError("transition must be one of: %s" % ", ".join(TRANSITIONS))
        plan.append((jt, float(j.get("duration", d))))

    # xfade positions the PICTURE, so its offsets must come from video stream lengths.
    durs = [video_duration_of(s) for s in srcs]
    longest = max([p[1] for p in plan] + [a_cross]) if plan else a_cross
    for i, dur in enumerate(durs):
        if dur <= longest + 0.1:
            raise ToolError("Clip %d is only %.2fs - too short for a %.2fs transition."
                            % (i + 1, dur, longest))
    w, h = ASPECTS.get(a.get("aspect") or "", (None, None))
    if w is None:
        info = probe(srcs[0])
        vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
        w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps") or 30)
    audio = all(has_audio(s) for s in srcs)

    parts = []
    for i in range(len(srcs)):
        parts.append("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
                     "crop=%d:%d,setsar=1,fps=%d,format=yuv420p[v%d]" % (i, w, h, w, h, fps, i))
        if audio:
            parts.append("[%d:a]aresample=48000,asetpts=N/SR/TB[a%d]" % (i, i))

    # xfade offsets are absolute positions on the growing output timeline.
    cur = "[v0]"
    total = durs[0]
    frame = 1.0 / max(1, fps)
    for i in range(1, len(srcs)):
        jt, jd = plan[i - 1]
        # A hard cut is expressed as a single-frame dissolve. Mixing concat and xfade in
        # one graph makes FFmpeg fail to renegotiate the filters, and one frame of blend
        # at 24fps is not perceptible anyway.
        jd = max(jd, frame)
        out = "[vx%d]" % i
        parts.append("%s[v%d]xfade=transition=%s:duration=%.3f:offset=%.3f%s"
                     % (cur, i, jt, jd, max(0.0, total - jd), out))
        total = total + durs[i] - jd
        cur = out
    if audio:
        acur = "[a0]"
        # A J-cut brings the next shot's sound in BEFORE its picture; an L-cut lets the
        # last shot's sound run on UNDER the new picture. It is the commonest move in
        # dialogue editing and the reason cut footage stops sounding cut. Chained
        # acrossfade cannot express it - each join lands wherever the streams happen to
        # meet - so when a lead is asked for, every clip's audio is placed at an
        # absolute position on the output timeline instead and the whole lot mixed.
        leads = []
        for i in range(len(srcs) - 1):
            j = (junctions[i] if junctions else {}) or {}
            leads.append(float(j.get("audio_lead", a.get("audio_lead", 0)) or 0))

        if any(abs(x) > 0.01 for x in leads):
            v_at, at = [0.0], 0.0
            for i in range(1, len(srcs)):
                at = at + durs[i - 1] - plan[i - 1][1]
                v_at.append(at)
            mixed = []
            for i, s in enumerate(srcs):
                # positive lead = audio arrives early (J), negative = it lingers (L)
                start = v_at[i] - (leads[i - 1] if i else 0.0)
                shift = max(0.0, start)
                fade = max(0.05, a_cross)
                head = "afade=t=in:st=0:d=%.3f," % fade if i else ""
                tail = "afade=t=out:st=%.3f:d=%.3f," % (max(0.0, durs[i] - fade), fade) \
                    if i < len(srcs) - 1 else ""
                parts.append("[a%d]%s%sadelay=%d|%d[am%d]"
                             % (i, head, tail, int(shift * 1000), int(shift * 1000), i))
                mixed.append("[am%d]" % i)
            acur = "[amix]"
            parts.append("%samix=inputs=%d:duration=longest:normalize=0%s"
                         % ("".join(mixed), len(mixed), acur))
        else:
            for i in range(1, len(srcs)):
                out = "[ax%d]" % i
                # Equal-power curves; a linear crossfade dips in the middle on
                # uncorrelated material.
                parts.append("%s[a%d]acrossfade=d=%.3f:c1=qsin:c2=qsin%s"
                             % (acur, i, a_cross, out))
                acur = out

    out_path = make_output(srcs[0], "smooth", a.get("output"), ".mp4")
    script = os.path.join(os.path.dirname(out_path), ".xfade_%d.txt" % os.getpid())
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(parts))
    try:
        args = []
        for s in srcs:
            args += ["-i", s]
        args += ["-filter_complex_script", script, "-map", cur]
        if audio:
            args += ["-map", acur] + AUDIO_ENC
        args += FAST_ENC + [out_path]
        ffmpeg_run(args)
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
    kinds = {}
    for jt, jd in plan:
        k = "hard cut" if jd <= 0.05 else "%.2fs %s" % (jd, jt)
        kinds[k] = kinds.get(k, 0) + 1
    lead = ("  Audio crosses over %.2fs, longer than the picture - the next shot is heard "
            "before it is seen (J-cut)." % a_cross) if a_cross > max(
                [p[1] for p in plan] or [0]) + 0.05 else ""
    return done(out_path, "Joined %d clips: %s. Final length %.2fs.%s"
                % (len(srcs), ", ".join("%d x %s" % (n, k) for k, n in sorted(kinds.items())),
                   duration_of(out_path), "\n" + lead if lead else ""))


def t_fix_audio(a):
    """Normalise loudness to broadcast/streaming level and stop it clipping."""
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This file has no audio track to fix.")
    target = float(a.get("target_lufs", -14.0))
    tp = float(a.get("true_peak", -1.5))
    before_l, before_p = _loudness(src), _peak_db(src)
    out = make_output(src, "audiofix", a.get("output"), ".mp4")
    af = _loudnorm_filter(src, target, tp, a.get("denoise"))
    ffmpeg_run(["-i", src, "-af", af, "-c:v", "copy", "-ar", "48000"] + AUDIO_ENC + [out])
    after_l, after_p = _loudness(out), _peak_db(out)
    note = "Loudness %.1f -> %.1f LUFS (target %.1f). Peak %.1f -> %.1f dB." % (
        before_l if before_l is not None else float("nan"),
        after_l if after_l is not None else float("nan"), target,
        before_p if before_p is not None else float("nan"),
        after_p if after_p is not None else float("nan"))
    if before_p is not None and before_p >= -0.1:
        note += " The clipping is gone - the audio no longer distorts."
    return done(out, note)


def t_speed_ramp(a):
    """Ease into slow motion at a moment and ease back out.

    A hard speed change reads as a glitch; ramping in and out is what makes the moment
    feel emphasised rather than broken. Audio is dropped because time-stretching speech
    across a ramp sounds wrong at any setting.
    """
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    at = parse_time(a.get("at"), "at")
    if at is None:
        raise ToolError("Give 'at' - the moment to slow into.")
    if not 0 <= at <= total:
        raise ToolError("'at' is outside the video (%.2fs long)." % total)
    slow = float(a.get("factor", 0.4))
    if not 0.1 <= slow < 1.0:
        raise ToolError("factor must be between 0.1 and 1 (0.4 = 40% speed).")
    ramp = float(a.get("ramp", 0.5))
    hold = float(a.get("hold", 0.6))

    lo, hi = max(0.0, at - hold / 2.0), min(total, at + hold / 2.0)
    r_in, r_out = max(0.0, lo - ramp), min(total, hi + ramp)

    # Build the ramp from short slices, each at a constant speed. Varying setpts with an
    # expression multiplies each timestamp pointwise, which warps absolute time rather
    # than integrating speed - a 0.6s hold stretched a 14s clip to 21s.
    steps = max(2, int(a.get("steps", 5)))
    slices = []
    if r_in > 0.001:
        slices.append((0.0, r_in, 1.0))
    for i in range(steps):                                   # ease in
        s0 = r_in + (lo - r_in) * i / steps
        s1 = r_in + (lo - r_in) * (i + 1) / steps
        if s1 > s0:
            slices.append((s0, s1, 1.0 + (slow - 1.0) * (i + 0.5) / steps))
    if hi > lo:
        slices.append((lo, hi, slow))
    for i in range(steps):                                   # ease out
        s0 = hi + (r_out - hi) * i / steps
        s1 = hi + (r_out - hi) * (i + 1) / steps
        if s1 > s0:
            slices.append((s0, s1, slow + (1.0 - slow) * (i + 0.5) / steps))
    if total > r_out + 0.001:
        slices.append((r_out, total, 1.0))

    fps = int(a.get("fps", 30))
    parts, labels = [], []
    for i, (s0, s1, sp) in enumerate(slices):
        parts.append("[0:v]trim=start=%.4f:end=%.4f,setpts=(PTS-STARTPTS)/%.5f[r%d]"
                     % (s0, s1, sp, i))
        labels.append("[r%d]" % i)
    parts.append("%sconcat=n=%d:v=1:a=0,fps=%d[outv]" % ("".join(labels), len(labels), fps))

    out = make_output(src, "ramp", a.get("output"), ".mp4")
    script = os.path.join(_tmpdir(), "ramp_%d.txt" % os.getpid())
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(parts))
    try:
        ffmpeg_run(["-i", src, "-filter_complex_script", script, "-map", "[outv]", "-an"]
                   + VIDEO_ENC + [out])
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
    expected = total + (hi - lo) * (1.0 / slow - 1.0) + ramp * (1.0 / slow - 1.0)
    return done(out, "Ramped into %.0f%% speed around %.2fs (%.2fs ease, %.2fs hold, "
                     "%d steps each side).\n  %.2fs -> %.2fs (expected about %.1fs). "
                     "Silent, because stretched speech sounds wrong."
                % (slow * 100, at, ramp, hold, steps, total, duration_of(out), expected))


def t_smooth_slowmo(a):
    src = check_input(a.get("path"), "video")
    factor = float(a.get("factor", 0.5))
    if not 0.1 <= factor < 1:
        raise ToolError("factor must be between 0.1 and 1 (0.5 = half speed).")
    fps = int(a.get("fps", 60))
    out = make_output(src, "slowmo", a.get("output"), ".mp4")
    vf = "minterpolate=fps=%d:mi_mode=mci:mc_mode=aobmc:vsbmc=1,setpts=%.6f*PTS" % (fps, 1.0 / factor)
    ffmpeg_run(["-i", src, "-vf", vf, "-an"] + VIDEO_ENC + [out])
    return done(out, "Smooth slow motion at %gx, %d fps (silent - slow audio sounds bad)." % (factor, fps))


# ---------------------------------------------------------------- seeing the video
def _mkdirs(d):
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def _tmpdir():
    d = os.path.join(os.environ.get("TEMP", "."), "video_editor_mcp")
    os.makedirs(d, exist_ok=True)
    return d


def image_content(img_path, max_w=1400, quality=72):
    """Package an image as MCP image content so Claude can actually look at it."""
    import base64
    try:
        from PIL import Image
    except ImportError:
        raise ToolError("Pillow is not installed. Run:\n    pip install Pillow")
    im = Image.open(img_path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / float(im.width))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return {"type": "image",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "mimeType": "image/jpeg"}


def grab_frame(src, at, out, vf=None, width=None):
    args = ["-ss", "%.3f" % max(0.0, at), "-i", src]
    filters = []
    if vf:
        filters.append(vf)
    if width:
        filters.append("scale=%d:-2" % width)
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-frames:v", "1", "-q:v", "3", out]
    ffmpeg_run(args)
    return out


def _label(im, text):
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/tahomabd.ttf", max(14, im.width // 26))
    except Exception:
        font = ImageFont.load_default()
    pad = 4
    box = d.textbbox((0, 0), text, font=font)
    d.rectangle([0, 0, box[2] + pad * 2, box[3] + pad * 2], fill=(0, 0, 0))
    d.text((pad, pad), text, fill=(255, 255, 255), font=font)
    return im


def t_look_at(a):
    """Return a contact sheet as a real image, so the model can judge the footage."""
    from PIL import Image
    src = check_input(a.get("path"), "video")
    n = max(2, min(16, int(a.get("frames", 9))))
    total = duration_of(src)
    if total <= 0:
        raise ToolError("Could not read this video's length.")
    tmp = _tmpdir()
    cell_w = 420
    shots = []
    for i in range(n):
        t = total * (i + 0.5) / n
        p = os.path.join(tmp, "look_%d_%d.jpg" % (os.getpid(), i))
        grab_frame(src, t, p, width=cell_w)
        im = Image.open(p).convert("RGB")
        shots.append(_label(im, "%d:%05.2f" % (int(t // 60), t % 60)))
    cols = 3 if n > 4 else 2
    rows = (n + cols - 1) // cols
    cw, ch = shots[0].width, shots[0].height
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * 6, rows * ch + (rows + 1) * 6), (32, 32, 32))
    for i, im in enumerate(shots):
        r, c = divmod(i, cols)
        sheet.paste(im, (6 + c * (cw + 6), 6 + r * (ch + 6)))
    out = os.path.join(tmp, "sheet_%d.jpg" % os.getpid())
    sheet.save(out, quality=85)
    for p in shots:
        pass
    return [{"type": "text",
             "text": "%d frames from %s (%.1fs long), evenly spaced. "
                     "Look at these to judge the footage before choosing a style."
                     % (n, os.path.basename(src), total)},
            image_content(out)]


def t_preview_effect(a):
    """Render the effect on a real second of footage and return a before/after image."""
    from PIL import Image
    src = check_input(a.get("path"), "video")
    names = a.get("effect")
    if isinstance(names, str):
        names = [names]
    if not names:
        raise ToolError("Give an 'effect' to preview. Options: %s" % ", ".join(EFFECT_NAMES))
    i = float(a.get("intensity", 0.5))
    total = duration_of(src)
    at = parse_time(a.get("at"), "at")
    if at is None:
        at = total * 0.4
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1920)), int(vs.get("height", 1080))
    w -= w % 2
    h -= h % 2
    chain = ",".join(_fx(nm, i, w, h, fps_of(src)) for nm in names)

    tmp = _tmpdir()
    clip = os.path.join(tmp, "prev_%d.mp4" % os.getpid())
    ffmpeg_run(["-ss", "%.3f" % max(0.0, at - 0.5), "-i", src, "-t", "1.2",
                "-filter_complex", "[0:v]" + chain + "[outv]", "-map", "[outv]",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", clip])
    before_p = grab_frame(src, at, os.path.join(tmp, "before_%d.jpg" % os.getpid()), width=640)
    after_p = grab_frame(clip, 0.5, os.path.join(tmp, "after_%d.jpg" % os.getpid()), width=640)
    b = _label(Image.open(before_p).convert("RGB"), "BEFORE")
    af = _label(Image.open(after_p).convert("RGB"), "AFTER: " + "+".join(names))
    if af.height != b.height:
        af = af.resize((int(af.width * b.height / float(af.height)), b.height), Image.LANCZOS)
    combo = Image.new("RGB", (b.width + af.width + 18, max(b.height, af.height) + 12), (32, 32, 32))
    combo.paste(b, (6, 6))
    combo.paste(af, (b.width + 12, 6))
    out = os.path.join(tmp, "combo_%d.jpg" % os.getpid())
    combo.save(out, quality=85)
    try:
        os.remove(clip)
    except OSError:
        pass
    return [{"type": "text",
             "text": "Preview at %.2fs - '%s' at intensity %.2f. Nothing was saved; this is "
                     "only a test frame. Look at it and decide whether the look suits this "
                     "footage before running video_effect for real."
                     % (at, "+".join(names), i)},
            image_content(out)]


# ---------------------------------------------------------------- measuring
def _signalstats(src, sample_fps=2):
    def compute():
        err = _probe_stderr(src, ["-vf", "fps=%d,signalstats,metadata=print" % sample_fps])
        keys = {}
        for k in ("YAVG", "YMIN", "YMAX", "SATAVG", "SATMAX", "YDIF", "UAVG", "VAVG"):
            keys[k] = [float(x) for x in
                       re.findall(r"lavfi\.signalstats\.%s=([\d.\-]+)" % k, err)]
        return keys
    return cached("signalstats", src, sample_fps, compute)


def _scene_cuts(src, threshold=10.0):
    def compute():
        err = _probe_stderr(src, ["-vf", "scdet=threshold=%.1f,metadata=print:key=lavfi.scd.time"
                                  % threshold])
        return sorted(set(float(x) for x in re.findall(r"lavfi\.scd\.time=([\d.]+)", err)))
    return cached("scenecuts", src, threshold, compute)


def _loudness(src):
    def compute():
        err = _probe_stderr(src, ["-af", "ebur128"])
        # ebur128 prints a running "I: -70.0 LUFS" per frame before the final summary,
        # so take the LAST reading, never the first.
        hits = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", err)
        return float(hits[-1]) if hits else None
    return cached("loudness", src, None, compute)


def _peak_db(src):
    """True peak via volumedetect - catches clipping that loudness alone hides."""
    def compute():
        err = _probe_stderr(src, ["-af", "volumedetect"])
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", err)
        return float(m.group(1)) if m else None
    return cached("peak", src, None, compute)


def _avg(xs):
    return sum(xs) / float(len(xs)) if xs else 0.0


def measure(src):
    total = duration_of(src)
    st = _signalstats(src)
    cuts = _scene_cuts(src)
    bright = _avg(st["YAVG"])
    sat = _avg(st["SATAVG"])
    motion = _avg(st["YDIF"])
    ymin, ymax = _avg(st["YMIN"]), _avg(st["YMAX"])
    loud = _loudness(src) if has_audio(src) else None
    peak = _peak_db(src) if has_audio(src) else None
    return {
        "peak": peak,
        "duration": total,
        "brightness": bright,          # 0-255, ~110-140 is well exposed
        "contrast": ymax - ymin,       # 0-255, under ~120 is flat
        "saturation": sat,             # 0-~180, under ~40 is washed out
        "motion": motion,              # 0-255 frame-to-frame difference
        "cuts": cuts,
        "cuts_per_min": len(cuts) / (total / 60.0) if total else 0,
        "loudness": loud,              # LUFS, -14 is streaming standard
    }


def t_analyze(a):
    src = check_input(a.get("path"), "video")
    m = measure(src)
    L = ["Analysis of %s (%.1fs)" % (os.path.basename(src), m["duration"]), ""]

    b = m["brightness"]
    verdict = ("very dark" if b < 60 else "dark" if b < 95 else
               "well exposed" if b <= 165 else "bright" if b <= 200 else "overexposed")
    L.append("Brightness: %.0f/255 - %s" % (b, verdict))

    c = m["contrast"]
    L.append("Contrast:   %.0f/255 - %s" % (c, "flat, could use punch" if c < 120
                                            else "normal" if c < 200 else "strong"))
    s = m["saturation"]
    L.append("Colour:     %.0f - %s" % (s, "washed out / near grey" if s < 30
                                        else "muted" if s < 55 else "normal" if s < 110
                                        else "very saturated"))
    mo = m["motion"]
    energy = "still / static" if mo < 4 else "calm" if mo < 12 else "moderate" if mo < 25 else "fast, lots of movement"
    L.append("Movement:   %.1f - %s" % (mo, energy))
    L.append("Scene cuts: %d (%.1f per minute)" % (len(m["cuts"]), m["cuts_per_min"]))
    if m["loudness"] is not None:
        lo = m["loudness"]
        L.append("Loudness:   %.1f LUFS - %s" % (
            lo, "too quiet" if lo < -24 else "good" if lo <= -12 else "loud"))
        if m.get("peak") is not None:
            pk = m["peak"]
            L.append("Peak:       %.1f dB%s" % (
                pk, " - CLIPPING, the audio is distorting" if pk >= -0.1
                else " - close to clipping" if pk > -1.0 else ""))
    else:
        L.append("Loudness:   no audio track")

    L.append("")
    L.append("Suggested fixes:")
    sug = []
    if b < 95:
        sug.append("brighten it (video_auto_style will do this)")
    elif b > 200:
        sug.append("pull the exposure down")
    if c < 120:
        sug.append("add contrast")
    if s < 30:
        sug.append("footage is nearly grey - 'vivid' would help, or lean in with 'black_and_white'")
    elif s > 130:
        sug.append("colour is already strong - avoid 'vivid'")
    if mo > 25:
        sug.append("high energy - 'cinematic' or 'vivid' suit this; avoid 'dreamy'")
    elif mo < 4:
        sug.append("very static - 'zoom_in' adds life")
    L.append("  - " + "\n  - ".join(sug) if sug else "  - nothing obvious; the footage is balanced")
    L.append("")
    L.append("This is measurement only, nothing was changed. Use video_look_at to actually "
             "see the frames before deciding, and video_preview_effect to check a look fits.")
    return "\n".join(L)


def _correction_for(m):
    """Work out an eq correction from a measurement. Returns (eq_parts, notes)."""
    eq, notes = [], []
    b, c, s = m["brightness"], m["contrast"], m["saturation"]
    if b < 95 or b > 200:
        # eq brightness is -1..1 over the full 0-255 range.
        adj = max(-0.3, min(0.3, (128.0 - b) / 255.0))
        eq.append("brightness=%.4f" % adj)
        notes.append("%s exposure" % ("raised" if adj > 0 else "lowered"))
    if c < 120:
        k = min(1.6, 1.0 + (120 - c) / 110.0)
        eq.append("contrast=%.3f" % k)
        notes.append("contrast x%.2f" % k)
    if s < 55:
        k = min(2.0, 1.0 + (55 - s) / 45.0)
        eq.append("saturation=%.3f" % k)
        notes.append("colour x%.2f" % k)
    elif s > 130:
        eq.append("saturation=0.9")
        notes.append("colour eased back")
    return eq, notes


def t_auto_style(a):
    """Measure, correct, then re-measure and correct again until it lands."""
    src = check_input(a.get("path"), "video")
    passes = max(1, min(3, int(a.get("passes", 3))))
    look = a.get("look")
    tmp = _tmpdir()

    first = measure(src)
    current, temps, notes = src, [], []
    for step in range(passes):
        m = measure(current) if step else first
        eq, note = _correction_for(m)
        if not eq:
            break
        stage = os.path.join(tmp, "style_%d_%d.mp4" % (os.getpid(), step))
        ffmpeg_run(["-i", current, "-vf", "eq=" + ":".join(eq)] +
                   (["-c:a", "copy"] if has_audio(current) else ["-an"]) +
                   VIDEO_ENC + [stage])
        temps.append(stage)
        current = stage
        notes.append("pass %d: %s" % (step + 1, ", ".join(note)))

    if current is src and not look:
        b, c, s = first["brightness"], first["contrast"], first["saturation"]
        return ("The footage already measures well (brightness %.0f, contrast %.0f, colour %.0f), "
                "so there is nothing to correct. Pass a 'look' if you want a style anyway. "
                "Original untouched." % (b, c, s))

    out = make_output(src, "styled", a.get("output"), ".mp4")
    if look:
        info = probe(current)
        vs = next((st for st in info["streams"] if st.get("codec_type") == "video"), {})
        w, h = int(vs.get("width", 1920)), int(vs.get("height", 1080))
        chain = _fx(look, float(a.get("intensity", 0.5)), w - w % 2, h - h % 2,
                    fps_of(current))
        ffmpeg_run(["-i", current, "-filter_complex", "[0:v]" + chain + "[outv]", "-map", "[outv]"] +
                   (["-map", "0:a", "-c:a", "copy"] if has_audio(current) else []) +
                   VIDEO_ENC + [out])
        notes.append("applied the '%s' look" % look)
    else:
        shutil.copyfile(current, out)
    for t in temps:
        try:
            os.remove(t)
        except OSError:
            pass

    after = measure(out)
    return done(out, "Auto-graded (%s).\n"
                     "  brightness %.0f -> %.0f\n  contrast   %.0f -> %.0f\n  colour     %.0f -> %.0f"
                % ("; ".join(notes) if notes else "look only",
                   first["brightness"], after["brightness"],
                   first["contrast"], after["contrast"],
                   first["saturation"], after["saturation"]))


# ---------------------------------------------------------------- music matching
def _decode_pcm(path, rate=22050, max_seconds=180):
    """Decode audio to mono float array using ffmpeg + numpy."""
    import numpy as np
    _require_ffmpeg()
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
           "-t", str(max_seconds), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not proc.stdout:
        raise ToolError("Could not read audio from %s" % os.path.basename(path))
    return np.frombuffer(proc.stdout, dtype="<i2").astype("float32") / 32768.0, rate


def analyse_audio(path):
    """Return tempo (BPM) and energy for a music file, using numpy only."""
    import numpy as np
    y, sr = _decode_pcm(path)
    if y.size < sr:
        raise ToolError("%s is too short to analyse." % os.path.basename(path))
    hop = 512
    frames = y.size // hop
    env = np.abs(y[:frames * hop].reshape(frames, hop)).mean(axis=1)
    rms = float(np.sqrt((y ** 2).mean()))
    # Onset strength: positive change in energy.
    diff = np.diff(env)
    onset = np.clip(diff, 0, None)
    onset = onset - onset.mean()
    if onset.std() > 0:
        onset = onset / onset.std()
    fps = sr / float(hop)
    # Autocorrelate the onset envelope over a plausible tempo range.
    lo, hi = int(fps * 60 / 200.0), int(fps * 60 / 60.0)
    hi = min(hi, len(onset) - 1)
    best_bpm, best_score = 0.0, -1e9
    if hi > lo > 0:
        ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
        for lag in range(lo, hi):
            if ac[lag] > best_score:
                best_score, best_bpm = ac[lag], 60.0 * fps / lag
    return {"bpm": round(best_bpm), "rms": rms,
            "energy": min(1.0, rms / 0.22), "duration": duration_of(path)}


def t_music_scan(a):
    folder = a.get("folder")
    if not folder:
        raise ToolError("Give the 'folder' holding your music files.")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        raise ToolError("Folder not found: %s" % folder)
    files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
             if f.lower().endswith(AUDIO_EXTS)]
    if not files:
        raise ToolError("No music files (%s) in %s" % (", ".join(AUDIO_EXTS), folder))
    rows = ["Scanned %d track(s) in %s" % (len(files), folder), ""]
    for f in files[:60]:
        try:
            info = analyse_audio(f)
            mood = ("calm / background" if info["energy"] < 0.3 else
                    "medium" if info["energy"] < 0.6 else "energetic / punchy")
            rows.append("%-38s %3d BPM  energy %.2f  %s  (%.0fs)"
                        % (os.path.basename(f)[:38], info["bpm"], info["energy"],
                           mood, info["duration"]))
        except ToolError as e:
            rows.append("%-38s could not read (%s)" % (os.path.basename(f)[:38], e))
    return "\n".join(rows)


def t_add_music_auto(a):
    src = check_input(a.get("path"), "video")
    folder = a.get("music_folder")
    if not folder:
        raise ToolError("Give a 'music_folder' to choose from.")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        raise ToolError("Folder not found: %s" % folder)
    tracks = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
              if f.lower().endswith(AUDIO_EXTS)]
    if not tracks:
        raise ToolError("No music files in %s" % folder)

    m = measure(src)
    # Map how busy the picture is onto a 0-1 target energy.
    want = max(0.0, min(1.0, m["motion"] / 30.0 * 0.7 + min(m["cuts_per_min"], 30) / 30.0 * 0.3))
    scored = []
    for t in tracks:
        try:
            info = analyse_audio(t)
            scored.append((abs(info["energy"] - want), t, info))
        except ToolError:
            continue
    if not scored:
        raise ToolError("None of the tracks in that folder could be read.")
    scored.sort(key=lambda x: x[0])
    gap, pick, info = scored[0]

    vol = float(a.get("volume", 0.25))
    duck = bool(a.get("duck_under_speech", True)) and has_audio(src)
    dur = m["duration"]
    fade = float(a.get("fade", 1.5))

    if duck:
        fc = ("[1:a]volume=%.3f,aloop=loop=-1:size=2e9,atrim=0:%.3f,"
              "afade=t=in:st=0:d=%.2f,afade=t=out:st=%.3f:d=%.2f[bg];"
              "[0:a]asplit=2[main][sc];"
              "[bg][sc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[duck];"
              "[main][duck]amix=inputs=2:duration=first:normalize=0[aout]"
              % (vol, dur, fade, max(0.0, dur - fade), fade))
    else:
        fc = ("[1:a]volume=%.3f,aloop=loop=-1:size=2e9,atrim=0:%.3f,"
              "afade=t=in:st=0:d=%.2f,afade=t=out:st=%.3f:d=%.2f[aout]"
              % (vol, dur, fade, max(0.0, dur - fade), fade))

    out = make_output(src, "music", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-i", pick, "-filter_complex", fc,
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-shortest"] +
               AUDIO_ENC + [out])

    ranked = "\n".join("    %-34s energy %.2f  %3d BPM  (off by %.2f)"
                       % (os.path.basename(t)[:34], i["energy"], i["bpm"], g)
                       for g, t, i in scored[:5])
    return done(out,
                "Picture energy measured at %.2f (movement %.1f, %.1f cuts/min).\n"
                "Chose: %s - energy %.2f, %d BPM.\n"
                "%sFaded in and out over %.1fs.\nRanked options:\n%s"
                % (want, m["motion"], m["cuts_per_min"], os.path.basename(pick),
                   info["energy"], info["bpm"],
                   "Music ducks automatically under the speech. " if duck else "",
                   fade, ranked))


# ---------------------------------------------------------------- quality guard
def _decode_gray(src, fps=2, width=320):
    """Decode the video to a small greyscale array for pixel statistics."""
    import numpy as np
    _require_ffmpeg()
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    sw, sh = int(vs.get("width", 0)), int(vs.get("height", 0))
    if not sw or not sh:
        raise ToolError("Could not read the picture size.")
    h = max(2, int(round(width * sh / float(sw))))
    h -= h % 2
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", src,
           "-vf", "fps=%d,scale=%d:%d" % (fps, width, h), "-pix_fmt", "gray",
           "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = buf.size // (width * h)
    if n < 1:
        raise ToolError("Could not read any frames from this video.")
    return buf[:n * width * h].reshape(n, h, width), (sw, sh)


def _edge_bars(frames, thresh=18):
    """Rows/columns at the frame edge that are essentially black in every sampled frame."""
    import numpy as np
    med = np.median(frames, axis=0)
    h, w = med.shape
    top = bottom = left = right = 0
    while top < h // 3 and med[top, :].mean() < thresh:
        top += 1
    while bottom < h // 3 and med[h - 1 - bottom, :].mean() < thresh:
        bottom += 1
    while left < w // 3 and med[:, left].mean() < thresh:
        left += 1
    while right < w // 3 and med[:, w - 1 - right].mean() < thresh:
        right += 1
    return top, bottom, left, right


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = (len(sorted_vals) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def _level_spread(src):
    """Interquartile spread of short-term loudness over the active parts.

    Peak-to-peak would just measure speech-versus-pause dynamics and flag every
    normal clip. The IQR of ebur128's short-term value separates cleanly instead:
    measured ~2-4 dB on matched audio, ~7 dB when unlevelled clips are joined.
    """
    err = _probe_stderr(src, ["-af", "ebur128"])
    vals = sorted(v for v in (float(x) for x in re.findall(r"S:\s*(-?[\d.]+)", err))
                  if v > -40)
    if len(vals) < 8:
        return None
    return _pct(vals, 0.75) - _pct(vals, 0.25)


def t_check(a):
    """Inspect a finished render for the defects that are easy to ship by accident."""
    import numpy as np
    src = check_input(a.get("path"), "video")
    frames, (sw, sh) = _decode_gray(src)
    total = duration_of(src)
    problems, passed = [], []

    top, bottom, left, right = _edge_bars(frames)
    sy, sx = frames.shape[1], frames.shape[2]
    if max(top, bottom) > 1 or max(left, right) > 1:
        problems.append("Black bars: top %d, bottom %d, left %d, right %d (of %dx%d sampled). "
                        "Crop to fill, or the export has letterboxing baked in."
                        % (top * sh // sy, bottom * sh // sy, left * sw // sx,
                           right * sw // sx, sw, sh))
    else:
        passed.append("no black bars")

    dark = float((frames < 12).mean()) * 100
    blown = float((frames > 250).mean()) * 100
    if dark > 12:
        problems.append("Crushed shadows: %.1f%% of the picture is near-black. A vignette or "
                        "contrast push may have flattened detail into black." % dark)
    else:
        passed.append("shadows intact (%.1f%% near-black)" % dark)
    if blown > 6:
        problems.append("Blown highlights: %.1f%% of the picture is pure white." % blown)
    else:
        passed.append("highlights safe (%.1f%% clipped)" % blown)

    if has_audio(src):
        peak, loud = _peak_db(src), _loudness(src)
        if peak is not None and peak >= -0.1:
            problems.append("Audio is clipping at %.1f dB - it will distort. Run video_fix_audio."
                            % peak)
        else:
            passed.append("no audio clipping (peak %.1f dB)" % (peak if peak is not None else 0))
        if loud is not None:
            if loud < -17:
                problems.append("Too quiet at %.1f LUFS; social platforms expect about -14. "
                                "Adding music with amix can drop the mix ~6 dB if it "
                                "normalises by input count." % loud)
            elif loud > -9:
                problems.append("Too loud at %.1f LUFS; it will be turned down and squashed." % loud)
            else:
                passed.append("loudness on target (%.1f LUFS)" % loud)
        spread = _level_spread(src)
        if spread is not None:
            if spread > 5.0:
                problems.append("Volume jumps between sections (%.1f dB spread, expected under 5). "
                                "Level each clip with video_fix_audio before joining." % spread)
            else:
                passed.append("level consistent (%.1f dB spread)" % spread)
    else:
        problems.append("No audio track at all.")

    if total < 1.0:
        problems.append("Only %.2fs long." % total)
    dead_head = [s for s in detect_silence(src, -40, 0.5) if s[0] < 0.05] if has_audio(src) else []
    if dead_head:
        passed.append("starts on picture")

    L = ["Quality check: %s" % os.path.basename(src),
         "%dx%d, %.2fs" % (sw, sh, total), ""]
    if problems:
        L.append("PROBLEMS (%d):" % len(problems))
        L += ["  - " + p for p in problems]
        L.append("")
    L.append("Passed: " + ", ".join(passed) if passed else "Passed: nothing")
    if not problems:
        L.append("")
        L.append("Clean - safe to publish.")
    return "\n".join(L)


# ---------------------------------------------------------------- subtitles v2
# ASS colours are &HAABBGGRR - alpha first, then BLUE, GREEN, RED. Writing them as
# if they were RGB is the classic way to get the wrong colour on screen.
SUBTITLE_PRESETS = {
    "premium":  dict(font="Tahoma", size=22, outline=2, shadow=1, margin=38,
                     primary="&H00FFFFFF", outline_col="&HC0000000", bold=0),
    # ASS alpha runs 00=opaque .. FF=invisible, so the &HFF outline this shipped with
    # drew nothing at all - white text straight onto the picture.
    "bold":     dict(font="Tahoma", size=25, outline=3, shadow=1, margin=42,
                     primary="&H00FFFFFF", outline_col="&H00000000", bold=1),
    "tiktok":   dict(font="Tahoma", size=26, outline=0, shadow=0, margin=70,
                     primary="&H00FFFFFF", outline_col="&H00000000", bold=1, box=3),
    "minimal":  dict(font="Tahoma", size=19, outline=1, shadow=0, margin=30,
                     primary="&H00FFFFFF", outline_col="&H80000000", bold=0),
    "caption":  dict(font="Tahoma", size=21, outline=0, shadow=0, margin=34,
                     primary="&H00FFFFFF", outline_col="&H00000000", bold=0, box=3),
    # --- premium looks -------------------------------------------------------
    # Soft dark panel behind the words: the most legible option over busy footage.
    "panel":    dict(font="Tahoma", size=21, outline=8, shadow=0, margin=40,
                     primary="&H00FFFFFF", outline_col="&HA0140D0A", bold=1, box=3),
    # Brand navy panel, for a house style.
    "brand":    dict(font="Tahoma", size=21, outline=8, shadow=0, margin=40,
                     primary="&H00FFFFFF", outline_col="&HB04A2B0D", bold=1, box=3),
    # No panel at all: a heavy opaque outline plus a dropped shadow carries the text over
    # confetti, white shirts and blown-out windows alike. Measured against the panel look
    # on the busiest frame in the spot - this is what to reach for when a box would cover
    # too much picture.
    "clean":    dict(font="Tahoma", size=29, outline=4, shadow=2, margin=40,
                     primary="&H00FFFFFF", outline_col="&H00000000", bold=1,
                     back_col="&H60000000"),
    # Light, wide-set and understated - luxury goods rather than social.
    "elegant":  dict(font="Tahoma", size=19, outline=0, shadow=2, margin=46,
                     primary="&H00F5F5F5", outline_col="&H90000000", bold=0,
                     spacing=2.2),
    # Warm cream text with a heavy soft shadow - reads as film titling.
    "cinema":   dict(font="Tahoma", size=22, outline=1, shadow=3, margin=44,
                     primary="&H00E8F2FA", outline_col="&HD0000000", bold=0,
                     spacing=0.8),
}


# 'fits' is MEASURED, not derived: each font was rendered through libass onto a
# 1080-wide frame and the drawn pixels counted, growing the string until the ink
# reached the margins. Deriving it from font metrics gave TH Krub 24 characters when
# it actually fits 20 - and captions ran off both edges of the frame. What matters is
# how libass lays the text out, not how wide the glyphs are in isolation.
#
# 'size' keeps every face the same visual size, and was re-measured 2026-07-27 by
# rendering one caption through each font and comparing ink heights. The values it
# replaced spread the rendered height across fonts by 55% - swapping font visibly
# changed how big the caption was, which is the one thing the multiplier exists to
# stop. Re-measured, that spread is 19%. It cannot reach zero: Thai faces divide
# their em differently, so a face can match on body height and still differ overall.
#
# Where the old and new 'fits' disagreed the SMALLER survives - overflow is the
# failure that shows, so err tight.
SUBTITLE_FONTS = {
    # --- text faces: use these for captions -------------------------------------
    "Tahoma":          dict(fits=19, size=1.00, note="plain, very legible"),
    "Leelawadee UI":   dict(fits=22, size=0.97, note="modern Windows Thai UI face"),
    "TH Sarabun New":  dict(fits=32, size=1.04, note="Thai standard, formal, very compact"),
    "TH Chakra Petch": dict(fits=23, size=0.85, note="geometric, contemporary"),
    "TH Krub":         dict(fits=20, size=0.76, note="clean modern sans"),
    "TH Baijam":       dict(fits=22, size=0.85, note="rounded, friendly"),
    # The family name really does have a space in it. Registered as "TH Fahkwang"
    # it silently fell back to another face - libass never says which font it
    # could not find, so the caption just quietly came out in something else.
    "TH Fah kwang":    dict(fits=22, size=0.97, note="squarish, a little editorial"),
    "TH K2D July8":    dict(fits=22, size=0.85, note="geometric, wide counters"),
    "TH KoHo":         dict(fits=22, size=0.86, note="humanist sans, warm"),
    "TH Kodchasal":    dict(fits=29, size=1.08, note="narrow, fits a lot per line"),
    "TH Mali Grade 6": dict(fits=22, size=0.97, note="soft rounded, childlike"),
    "TH Niramit AS":   dict(fits=31, size=1.07, note="compact text face, economical"),
    # --- bundled from Google Fonts (all SIL OFL, safe on commercial work) --------
    # The pre-installed set above is the Thai National Font collection: excellent,
    # and it looks like a government document. These are what Thai social media
    # actually sets captions in, plus Latin faces for English titles on the brand
    # work. fits and size measured by rendering against Tahoma; `thai` comes from
    # the font's own cmap, NOT from looking at a render - a face without Thai
    # draws notdef boxes that any pixel test happily counts as ink, which is how
    # Kavivanar nearly got shipped as a Thai face with zero Thai glyphs in it.
    "Kanit":         dict(fits=19, size=1.01, thai=True,  kind="caption", note="the Thai social standard - bold, modern"),
    "Prompt":        dict(fits=18, size=0.99, thai=True,  kind="caption", note="geometric, clean, very current"),
    "Mitr":          dict(fits=19, size=1.00, thai=True,  kind="caption", note="rounded and friendly, warm"),
    "Athiti":        dict(fits=20, size=1.02, thai=True,  kind="caption", note="quiet sans, gets out of the way"),
    "Bai Jamjuree":  dict(fits=18, size=0.84, thai=True,  kind="caption", note="wide and sturdy, strong on video"),
    "Noto Sans Thai":dict(fits=20, size=0.97, thai=True,  kind="caption", note="neutral workhorse, never wrong"),
    "Pridi":         dict(fits=20, size=0.98, thai=True,  kind="caption", note="serif - editorial, grown-up"),
    "Taviraj":       dict(fits=18, size=0.83, thai=True,  kind="caption", note="serif with contrast, elegant"),
    "Trirong":       dict(fits=19, size=0.83, thai=True,  kind="caption", note="serif, formal and calm"),
    "Maitree":       dict(fits=18, size=0.87, thai=True,  kind="caption", note="soft serif, easy to read long"),
    "Itim":          dict(fits=19, size=0.92, thai=True,  kind="hand",    note="handwritten, casual and sweet"),
    "Sriracha":      dict(fits=19, size=0.80, thai=True,  kind="hand",    note="brush handwriting - personal"),
    "Charm":         dict(fits=22, size=0.57, thai=True,  kind="hand",    note="loose script - titles only"),
    "Chonburi":      dict(fits=15, size=0.87, thai=True,  kind="title",   note="heavy slab display - titles only"),
    "Pattaya":       dict(fits=22, size=0.89, thai=True,  kind="title",   note="condensed display - titles only"),
    # Latin only. Naming one of these in a Thai caption is the worst outcome:
    # libass falls back per glyph, so Thai words and Latin digits come out in two
    # different typefaces and it reads as a mistake rather than a choice.
    "Bebas Neue":    dict(fits=19, size=1.41, thai=False, kind="title",   note="condensed caps - punchy English titles"),
    "Anton":         dict(fits=16, size=0.96, thai=False, kind="title",   note="very heavy English display"),
    "Archivo Black": dict(fits=11, size=1.08, thai=False, kind="title",   note="blunt, wide, loud"),
    "Oswald":        dict(fits=17, size=1.02, thai=False, kind="title",   note="condensed, newsy"),
    "Montserrat":    dict(fits=12, size=1.10, thai=False, kind="caption", note="clean geometric English"),
    "Poppins":       dict(fits=12, size=0.95, thai=False, kind="caption", note="round geometric, friendly"),
    "Playfair Display": dict(fits=13, size=1.05, thai=False, kind="title", note="high-contrast serif, luxury"),
    "Lobster":       dict(fits=15, size=1.01, thai=False, kind="hand",    note="retro script - logos, titles"),
    "Kavivanar":     dict(fits=15, size=0.91, thai=False, kind="hand",    note="informal - LATIN ONLY despite the name"),
    # --- display faces: titles and end cards, too fussy for running captions -----
    "TH Charm of AU":  dict(fits=30, size=0.63, kind="title", note="display, ornamental - titles only"),
    "TH Charmonman":   dict(fits=38, size=0.94, kind="hand", note="handwriting script - titles only"),
    "TH Srisakdi":     dict(fits=45, size=0.96, kind="title", note="condensed display - titles only"),
}
# Thai character widths vary, and the measurement used one sample string, so keep
# a margin rather than sitting exactly on the limit.
CAPTION_SAFETY = 0.88


BUNDLED_FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def subtitles_arg(name):
    r"""subtitles=..., pointed at the bundled fonts as well as the system ones.

    fontsdir means the downloaded faces work without being installed - nothing is
    written to the machine, and the connector still renders the same on a PC that
    has never seen them. libass silently falls back to a default face when it
    cannot find the one named, which looks like the style was ignored rather than
    like a missing font, so this is worth getting right.

    The path must be BOTH escaped and single-quoted. Tested all three forms on a
    path that has a drive letter and a space in it:
        fontsdir=C\:/...          -> "No option name near 'Prog...'"
        fontsdir='C:/...'         -> "No option name near '/Users...'"
        fontsdir='C\:/...'        -> works
    An escaped colon on its own still ends the option, and quotes on their own do
    not protect the drive letter. This has now bitten in five separate filters.
    """
    if os.path.isdir(BUNDLED_FONTS):
        return "subtitles=%s:fontsdir='%s'" % (name, escape_filter_path(BUNDLED_FONTS))
    return "subtitles=%s" % name


_FONT_FILE_CACHE = {}


def _font_file(family):
    """The file for a family name - bundled OR installed on this machine.

    Both have to be searched. Half the registered faces are the Thai National set
    that came with Windows, and a font browser that can only draw the ones it
    downloaded is no use for choosing between them.

    Bundled wins on a name clash, since that is the copy libass will be pointed at.
    Regular weights are preferred over italics and light cuts: a family's plain
    face is what the caption will actually use.
    """
    if not _FONT_FILE_CACHE:
        try:
            from PIL import ImageFont
        except ImportError:
            _FONT_FILE_CACHE["__scanned__"] = ""
            return None
        dirs = [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "Windows", "Fonts"),
                os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")]
        rank = lambda st: (("italic" in st.lower()) * 4 +
                           any(w in st.lower() for w in ("light", "thin", "extra")) * 2 +
                           (st.lower() not in ("regular", "bold", "semibold", "medium")))
        for d in dirs:                       # system first...
            for f in (globmod.glob(os.path.join(d, "*.ttf")) +
                      globmod.glob(os.path.join(d, "*.otf"))):
                try:
                    fam, style = ImageFont.truetype(f, 20).getname()
                except Exception:
                    continue
                prev = _FONT_FILE_CACHE.get(fam)
                if prev is None or rank(style) < rank(prev[1]):
                    _FONT_FILE_CACHE[fam] = (f, style)
        for f in globmod.glob(os.path.join(BUNDLED_FONTS, "*.ttf")):
            try:                             # ...bundled last, so it wins
                _FONT_FILE_CACHE[ImageFont.truetype(f, 20).getname()[0]] = (f, "bundled")
            except Exception:
                pass
        _FONT_FILE_CACHE["__scanned__"] = ("", "")
    hit = _FONT_FILE_CACHE.get(family)
    return hit[0] if hit else None


def check_font_for(font, text):
    """Refuse a Latin-only face on Thai words, rather than let it half-render.

    libass falls back GLYPH BY GLYPH, so a Latin face asked for Thai produces the
    words in a substitute typeface and the digits in the chosen one. It looks like
    a bug in the edit, and nothing in the output says which font was missing.
    """
    meta = SUBTITLE_FONTS.get(font or "")
    if not meta or meta.get("thai") is not False:
        return
    if any(u"ก" <= c <= u"๛" for c in (text or "")):
        ok = sorted(n for n, m in SUBTITLE_FONTS.items()
                    if m.get("thai") is not False
                    and (m.get("kind") or "caption") == (meta.get("kind") or "caption"))
        raise ToolError("“%s” has no Thai glyphs - it is a Latin-only face, so Thai "
                        "words would come out in a substitute typeface while the "
                        "numbers stayed in this one.\n  For %s work in Thai, try: %s"
                        % (font, meta.get("kind", "caption"), ", ".join(ok[:6])))


def caption_width_for(font, base=16):
    """Characters per line for this font, from the measured fit."""
    f = SUBTITLE_FONTS.get(font or "Tahoma")
    if not f:
        return base
    return max(8, int(f["fits"] * CAPTION_SAFETY))


def _style_string(preset, size=None, font=None, primary=None, box_colour=None,
                  bold=None, margin=None):
    p = SUBTITLE_PRESETS.get(preset)
    if p is None:
        raise ToolError("style must be one of: %s" % ", ".join(SUBTITLE_PRESETS))
    fam = font or p["font"]
    # Keep the rendered height the same whichever face is chosen.
    scale = (SUBTITLE_FONTS.get(fam) or {}).get("size", 1.0)
    parts = ["FontName=%s" % fam,
             "FontSize=%d" % max(8, int(round((size or p["size"]) * scale))),
             "PrimaryColour=%s" % (primary or p["primary"]),
             "OutlineColour=%s" % (box_colour or p["outline_col"]),
             "BorderStyle=%d" % p.get("box", 1),
             "Outline=%d" % p["outline"],
             "Shadow=%d" % p["shadow"],
             "Bold=%d" % (p["bold"] if bold is None else int(bool(bold))),
             "MarginV=%d" % (margin or p["margin"]),
             "MarginL=24", "MarginR=24", "Alignment=2"]
    if p.get("spacing"):
        parts.append("Spacing=%.1f" % p["spacing"])
    if p.get("back_col"):
        parts.append("BackColour=%s" % p["back_col"])   # the dropped shadow's colour
    return ",".join(parts)


def _hex_to_ass(colour, alpha=0):
    """#RRGGBB -> ASS &HAABBGGRR. The byte order is reversed from HTML."""
    c = (colour or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c or ""):
        return None
    r, g, b = c[0:2], c[2:4], c[4:6]
    return "&H%02X%s%s%s" % (max(0, min(255, int(alpha))), b.upper(), g.upper(), r.upper())


def parse_srt(path):
    text = io.open(path, encoding="utf-8-sig", errors="replace").read()
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        tm = next((l for l in lines if "-->" in l), None)
        if not tm:
            continue
        body = lines[lines.index(tm) + 1:]
        cues.append((tm.strip(), " ".join(body)))
    return cues


def t_burn_subtitles(a):
    """Burn an existing subtitle file, re-wrapping it safely first.

    libass cannot wrap Thai (no spaces), so a long line silently runs off both
    edges of the frame. Re-wrapping with explicit breaks prevents that.
    """
    src = check_input(a.get("path"), "video")
    subs = check_input(a.get("subtitles"), "subtitle")
    preset = a.get("style") or "premium"
    font_name = a.get("font") or SUBTITLE_PRESETS.get(preset, {}).get("font")
    # A narrower face fits more per line, so the width follows the font unless set.
    limit = int(a.get("max_chars_per_line") or caption_width_for(font_name, 16))

    use = subs
    if subs.lower().endswith(".srt") and a.get("rewrap", True):
        cues = parse_srt(subs)
        if not cues:
            raise ToolError("No subtitles found in %s" % subs)
        fixed = os.path.join(_tmpdir(), "wrapped_%d.srt" % os.getpid())
        with io.open(fixed, "w", encoding="utf-8-sig") as fh:
            for i, (tm, body) in enumerate(cues, 1):
                fh.write("%d\n%s\n%s\n\n"
                         % (i, tm, enforce_width(wrap_line(body, limit), limit)))
        use = fixed

    style = _style_string(preset, a.get("font_size"), a.get("font"),
                          _hex_to_ass(a.get("text_color")),
                          _hex_to_ass(a.get("box_color"), a.get("box_opacity", 160)),
                          a.get("bold"), a.get("margin"))
    vf = "subtitles='%s':force_style='%s'" % (escape_filter_path(use), style)
    out = make_output(src, "subs", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", vf] + VIDEO_ENC + ["-c:a", "copy", out])
    return done(out, "Burned %d caption(s) in '%s' style, re-wrapped to %d chars per line."
                % (len(parse_srt(use)) if use.lower().endswith(".srt") else 0, preset, limit))


# ---------------------------------------------------------------- end card
def _hex_to_ff(colour):
    c = (colour or "").strip()
    if c.startswith("#"):
        c = c[1:]
    if re.fullmatch(r"[0-9a-fA-F]{6}", c or ""):
        return "0x" + c.lower()
    return colour or "black"


MOTION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion")


def _asr_frags(src, lang, size):
    """Recognised fragments as (start, end, text), with combining marks folded in.

    This is the single most expensive step in the whole server - about 12 seconds per
    10 seconds of audio, and none of it tunable: the threads are already saturated, a
    narrower beam saves a second, and the smaller models are both slower to load and
    wrong. So the win is simply never doing it twice. The cache key includes the file's
    own timestamp, so re-running an edit is free while editing the file forces a fresh
    pass.
    """
    import contextlib

    def transcribe():
        model = _load_whisper(size)
        with contextlib.redirect_stdout(sys.stderr):
            segs, info = model.transcribe(src, language=None if lang == "auto" else lang,
                                          vad_filter=True, beam_size=5, word_timestamps=True)
            segs = list(segs)
        frags = []
        for seg in segs:
            for w in (getattr(seg, "words", None) or []):
                txt = w.word or ""
                if not txt:
                    continue
                if frags and txt[0] in THAI_COMBINING:
                    frags[-1] = [frags[-1][0], w.end, frags[-1][2] + txt]
                else:
                    frags.append([w.start, w.end, txt])
        return {"frags": frags, "lang": getattr(info, "language", lang)}

    got = cached("asr", src, {"lang": lang, "size": size}, transcribe)
    return ([(f[0], f[1], f[2]) for f in got["frags"]], got.get("lang") or lang)


def _word_timings(src, lang, size, want_map=False):
    """Real words with start/end times.

    Whisper's Thai tokens are sub-syllable fragments ("ร", "าย", "ง", "าน"), which are
    useless to highlight one at a time. So the fragments give a character-to-time map,
    the text is segmented into actual words, and each word inherits the times of the
    characters it spans.
    """
    frags, detected = _asr_frags(src, lang, size)
    if not frags:
        raise ToolError("No speech found.")

    full = "".join(f[2] for f in frags)
    spans, pos = [], 0
    for s, e, txt in frags:
        spans.append((pos, pos + len(txt), s, e))
        pos += len(txt)

    def time_at(idx, end=False):
        for lo, hi, s, e in spans:
            if lo <= idx < hi:
                frac = (idx - lo) / float(max(1, hi - lo))
                return s + (e - s) * frac
        return spans[-1][3] if end else spans[0][2]

    if want_map:
        # The character-to-time map is what lets hand-written wording be locked to the
        # voice: any run of characters can be looked up, whatever the segmenter decided.
        return full, time_at

    words = _thai_words(full) or full.split(" ")
    out, cursor = [], 0
    for word in words:
        idx = full.find(word, cursor)
        if idx < 0:
            continue
        out.append({"t": word, "s": round(time_at(idx), 3),
                    "e": round(time_at(idx + len(word) - 1, True), 3)})
        cursor = idx + len(word)
    # Times must march forward or the highlight jumps backwards mid-line.
    for i in range(1, len(out)):
        if out[i]["s"] < out[i - 1]["e"]:
            out[i]["s"] = out[i - 1]["e"]
        if out[i]["e"] <= out[i]["s"]:
            out[i]["e"] = out[i]["s"] + 0.08
    return out, detected


def _joined_speech_map(srcs, offsets, lang, size):
    """One character-to-time map across pieces that are about to be joined.

    Cues are written against the finished cut, so the map they are looked up in has
    to run on the finished cut's clock. The joined audio does not exist yet, so each
    piece is recognised on its own and its times are shifted by where that piece
    lands on the timeline.

    Worth the recognition pass: spreading a cue by character count instead put the
    word highlight an average of 0.31s and a worst of 0.75s off the voice on the
    MiiMuuD ad - 43 of its 45 words more than a frame out. Recognition is cached, so
    the cost falls on the first build of a set of pieces and no later one.
    """
    fulls, maps, bases, at = [], [], [], 0
    for s in srcs:
        try:
            full_i, time_i = _word_timings(s, lang, size, want_map=True)
        except ToolError:
            full_i, time_i = "", None       # a silent shot is perfectly normal
        fulls.append(full_i)
        maps.append(time_i)
        bases.append(at)
        at += len(full_i)

    if not any(fulls):
        return None

    def time_at(idx, end=False):
        for i in range(len(fulls) - 1, -1, -1):
            if fulls[i] and idx >= bases[i]:
                return offsets[i] + maps[i](idx - bases[i], end)
        first = next(i for i, f in enumerate(fulls) if f)
        return offsets[first] + maps[first](0, end)

    return "".join(fulls), time_at


def _room_tone_source(src, min_len=0.45):
    """Find the quietest sustained stretch - that is what the room actually sounds like."""
    total = duration_of(src)
    quiet = merge_spans(detect_silence(src, -34, min_len), gap=0.1)
    usable = [(s, e) for s, e in quiet if e - s >= min_len and s > 0.15 and e < total - 0.15]
    if not usable:
        return None
    return max(usable, key=lambda sp: sp[1] - sp[0])


def t_polish(a):
    """The details that separate an edited video from a produced one.

    Each of these is invisible on its own and obvious by its absence: continuous room
    tone, a grain layer, restrained colour, a breath of movement, and voice EQ.
    """
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    steps, tmp = [], _tmpdir()

    # ---- video ---------------------------------------------------------------
    vf = []
    grain = float(a.get("grain", 0.55))
    if grain > 0:
        # Grain also disguises a resolution mismatch: shots from different sources stop
        # looking like different sources once they share the same texture.
        vf.append("noise=alls=%d:allf=t+u" % max(1, int(round(grain * 12))))
        steps.append("film grain at %.2f" % grain)

    product = (a.get("product_colour") or "").strip().lower()
    if product:
        # Pull the whole frame back, then push saturation into the product's own colour
        # band. The product reads as vivid while the frame looks graded rather than
        # turned up - which is why it registers as expensive instead of loud.
        band = {"blue": "blues", "cyan": "cyans", "red": "reds", "green": "greens",
                "yellow": "yellows", "magenta": "magentas"}.get(product)
        if not band:
            raise ToolError("product_colour must be one of: %s"
                            % "blue, cyan, red, green, yellow, magenta")
        pull = float(a.get("desaturate", 0.86))
        lift = float(a.get("product_lift", 0.14))
        vf.append("eq=saturation=%.3f" % pull)
        # selectivecolor takes cyan/magenta/yellow/black shifts for the chosen band.
        shift = {"blues": "%.3f 0 -%.3f 0", "cyans": "%.3f 0 -%.3f 0",
                 "reds": "-%.3f %.3f 0 0", "greens": "-%.3f 0 %.3f 0",
                 "yellows": "0 -%.3f %.3f 0", "magentas": "0 %.3f -%.3f 0"}[band]
        vf.append("selectivecolor=%s=%s" % (band, shift % (lift, lift)))
        steps.append("saturation pulled to %.0f%%, %s pushed back up" % (pull * 100, product))

    push = float(a.get("push_in", 0.018))
    if push > 0:
        # A frame that breathes reads as film; a locked-off frame reads as a still.
        z = 1.0 + push
        # zoompan RELABELS frames at whatever rate it is given rather than resampling
        # them, so a constant here stretches the picture against the audio on any source
        # that is not already at that rate.
        pfps = fps_of(src)
        vf.append("scale=%d:%d" % (w * 2, h * 2))
        vf.append("zoompan=z='min(1+%0.6f*on/%d,%0.5f)':d=1:x='iw/2-(iw/zoom/2)':"
                  "y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%0.6f"
                  % (push, max(1, int(total * pfps)), z, w, h, pfps))
        steps.append("%.1f%% push-in across the whole clip" % (push * 100))

    # ---- audio ---------------------------------------------------------------
    af = []
    if a.get("voice_eq", True) and has_audio(src):
        # Clear the rumble, add a little presence. This is most of what makes a voice
        # sound produced rather than merely recorded.
        af.append("highpass=f=80")
        af.append("equalizer=f=3000:t=q:w=1.2:g=2.2")
        af.append("equalizer=f=250:t=q:w=1.0:g=-1.5")
        steps.append("voice EQ: 80 Hz high-pass, +2.2 dB presence, -1.5 dB mud")

    out = make_output(src, "polish", a.get("output"), ".mp4")
    stage = os.path.join(tmp, "polish_%d.mp4" % os.getpid())
    args = ["-i", src]
    if vf:
        args += ["-vf", ",".join(vf)]
    if af:
        args += ["-af", ",".join(af)]
    args += (["-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if has_audio(src) else ["-an"])
    args += FAST_ENC + [stage]
    ffmpeg_run(args)

    # ---- room tone -----------------------------------------------------------
    tone_note = ""
    if a.get("room_tone", True) and has_audio(src):
        # A finished cut usually has music running under it, leaving no true silence to
        # sample. The raw footage does - so allow pointing at the original clip.
        tone_src = a.get("room_tone_from")
        tone_src = check_input(tone_src, "room tone source") if tone_src else src
        span = _room_tone_source(tone_src)
        if span:
            s, e = span
            bed = os.path.join(tmp, "tone_%d.wav" % os.getpid())
            ffmpeg_run(["-ss", "%.3f" % s, "-i", tone_src, "-t", "%.3f" % (e - s),
                        "-vn", "-af", "afade=t=in:st=0:d=0.08,afade=t=out:st=%.3f:d=0.08"
                        % max(0.0, (e - s) - 0.08), "-ac", "2", "-ar", "48000", bed])
            lvl = float(a.get("room_tone_level", 0.5))
            ffmpeg_run(["-i", stage, "-stream_loop", "-1", "-i", bed,
                        "-filter_complex",
                        "[1:a]volume=%.3f,atrim=0:%.3f[tone];"
                        "[0:a][tone]amix=inputs=2:duration=first:normalize=0[a]" % (lvl, total),
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", out])
            tone_note = ("\n  Room tone taken from %.2f-%.2fs of %s (its quietest stretch) and "
                         "laid underneath continuously - the silence between lines is gone."
                         % (s, e, os.path.basename(tone_src)))
            steps.append("continuous room tone")
        else:
            shutil.move(stage, out)
            tone_note = ("\n  No quiet stretch long enough to sample in %s, so no room tone was "
                         "added. A finished cut with music under it has no true silence left - "
                         "pass 'room_tone_from' pointing at the raw footage instead."
                         % os.path.basename(tone_src))
    else:
        shutil.move(stage, out)
    if os.path.isfile(stage):
        try:
            os.remove(stage)
        except OSError:
            pass

    report = t_check({"path": out})
    return done(out, "Polished: %s.%s\n\n%s"
                % ("; ".join(steps) if steps else "nothing enabled", tone_note, report))


def t_kinetic_captions(a):
    """Captions where each word lights up as it is spoken."""
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This video has no audio to caption.")
    if not os.path.isdir(os.path.join(MOTION_DIR, "node_modules")):
        raise ToolError("Remotion is not set up. In %s run:\n    npm install" % MOTION_DIR)
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise ToolError("Node.js is required for kinetic captions but was not found.")

    lang = (a.get("language") or "auto").strip().lower()
    given = a.get("cues")
    if given:
        # Hand-written cues: the caller has already decided the wording and where the
        # lines break, which is the one judgement automatic captioning cannot make.
        # Still transcribe, but only to look up when each word is actually said.
        words, detected = [], lang
        timer = _word_timings(src, lang, a.get("model") or "large-v3", want_map=True)
    else:
        words, detected = _word_timings(src, lang, a.get("model") or "large-v3")

    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps") or 24)
    total = duration_of(src)

    font = a.get("font") or "TH Krub"
    per_line = int(a.get("max_chars_per_line") or caption_width_for(font, 16))
    max_lines = max(1, int(a.get("max_lines", 2)))
    gap = float(a.get("sentence_gap", 0.45))

    if given:
        payload = _cues_from_text(given, total, timer)
        if not payload:
            raise ToolError("None of the given cues had any text.")
        # 0 words: let the renderer count them off the payload it is about to draw.
        return _render_kinetic(a, src, payload, w, h, fps, total, font, npx,
                               0, "hand-written")

    # Group words into cues on pauses and length, then wrap each cue into lines.
    cues, cur = [], []
    for wd in words:
        if cur:
            long_gap = wd["s"] - cur[-1]["e"] > gap
            too_long = wd["e"] - cur[0]["s"] > float(a.get("max_seconds_per_line", 2.6))
            too_wide = sum(len(x["t"]) for x in cur) + len(wd["t"]) > per_line * max_lines
            if long_gap or too_long or too_wide:
                cues.append(cur)
                cur = []
        cur.append(wd)
    if cur:
        cues.append(cur)

    payload = []
    for group in cues:
        lines, line, width_used = [], [], 0
        for wd in group:
            if line and width_used + len(wd["t"]) > per_line:
                lines.append(line)
                line, width_used = [], 0
            line.append(wd)
            width_used += len(wd["t"])
        if line:
            lines.append(line)
        lines = lines[:max_lines]
        flat = [x for ln in lines for x in ln]
        if not flat:
            continue
        payload.append({"s": flat[0]["s"],
                        "e": min(total, flat[-1]["e"] + 0.35),
                        "lines": lines})

    if not payload:
        raise ToolError("Nothing to caption.")
    return _render_kinetic(a, src, payload, w, h, fps, total, font, npx,
                           len(words), detected)


def _cues_from_text(given, total, timer=None):
    """Turn hand-written cues into per-word timings.

    Where the written wording matches what was heard, each word is looked up in the
    recogniser's character-to-time map and lands exactly on the voice. Spreading the
    cue's span by character count instead drifts by a couple of tenths wherever the
    speaker pauses mid-cue, which is visible as the highlight running ahead. Words
    that cannot be found - a different spelling of a colloquial particle, say - fall
    back to sharing out whatever time is left between their located neighbours.
    """
    full, time_at = timer if timer else (None, None)
    cursor = 0
    payload = []
    for cue in given:
        s, e = parse_time(cue.get("start"), "start"), parse_time(cue.get("end"), "end")
        text = (cue.get("text") or "").strip("\n")
        if s is None or e is None or e <= s or not text.strip():
            continue
        rows = [_thai_words(ln) or ln.split(" ") for ln in text.split("\n") if ln.strip()]
        rows = [[w for w in r if w] for r in rows]
        rows = [r for r in rows if r]
        if not rows:
            continue
        flat = [w for r in rows for w in r]

        found = [None] * len(flat)
        if full:
            for i, word in enumerate(flat):
                probe_at = full.find(word.strip(), cursor)
                if probe_at >= 0:
                    hit = (time_at(probe_at), time_at(probe_at + len(word.strip()) - 1, True))
                    if float(s) - 0.5 <= hit[0] <= float(e) + 0.5:   # sanity: same cue
                        found[i] = hit
                        cursor = probe_at + len(word.strip())

        # Fill the gaps: anything unmatched shares the time between its located
        # neighbours, weighted by how many characters it has to get through.
        times, i = [None] * len(flat), 0
        for i, hit in enumerate(found):
            times[i] = hit
        lo_t = float(s)
        i = 0
        while i < len(flat):
            if times[i]:
                lo_t = times[i][1]
                i += 1
                continue
            j = i
            while j < len(flat) and not times[j]:
                j += 1
            hi_t = times[j][0] if j < len(flat) else float(e)
            weights = [max(1, len(flat[k])) for k in range(i, j)]
            step = (hi_t - lo_t) / float(sum(weights))
            at = lo_t
            for k in range(i, j):
                dur = weights[k - i] * step
                times[k] = (at, at + dur)
                at += dur
            i = j

        # Hold each word lit until the next one starts. Real speech leaves gaps between
        # words, and honouring them literally makes the highlight blink out between
        # every word. The ONSET is what the eye tracks, so keep those exact and simply
        # close the gaps behind them.
        for k in range(len(times) - 1):
            times[k] = (times[k][0], max(times[k][1], times[k + 1][0]))
        times[-1] = (times[-1][0], max(times[-1][1], float(e)))

        lines, k = [], 0
        for r in rows:
            line = []
            for word in r:
                a_t, b_t = times[k]
                line.append({"t": word, "s": round(a_t, 3), "e": round(max(b_t, a_t + 0.05), 3)})
                k += 1
            lines.append(line)
        payload.append({"s": float(s), "e": float(e), "lines": lines})

    # Hold each cue on screen a moment past the last word so it can be read - but never
    # into the next one. The component takes the FIRST cue whose window contains the
    # frame, so an overlap would keep showing the old caption over the new line.
    hold = 0.3
    for i, cue in enumerate(payload):
        limit = payload[i + 1]["s"] if i + 1 < len(payload) else total
        cue["e"] = min(cue["e"] + hold, limit, total)
    return payload


def _ass_colour(hex_colour, default="&H00FFFFFF"):
    """#RRGGBB -> ASS &H00BBGGRR (byte order reversed, 00 alpha = opaque)."""
    c = (hex_colour or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c or ""):
        return default
    return "&H00%s%s%s" % (c[4:6].upper(), c[2:4].upper(), c[0:2].upper())


def _kinetic_ass_text(a, payload, w, h, font):
    """The word-by-word captions as ASS text, so both the caption tool and the
    single-pass build can draw them without duplicating the styling."""
    return _render_kinetic_ass(a, None, payload, w, h, 0, font, None, text_only=True)


def _render_kinetic_ass(a, src, payload, w, h, total, font, out, text_only=False):
    """The same word-by-word highlight, drawn by libass instead of a browser.

    Remotion renders every frame through Chromium and encodes it to VP8 with alpha -
    152 seconds for a 25 second cut, then another 22 to composite it. libass draws the
    identical colour-and-scale change in one ffmpeg pass. What it cannot do is the soft
    glow around the live word, so this is offered rather than imposed.
    """
    entrance = bool(a.get("title_motion", True))
    size = int(round(h * 0.030 * float(a.get("font_scale", 1.0))))
    outline = max(1, int(round(size * float(a.get("outline", 0.075)) * 0.55)))
    accent = _ass_colour(a.get("accent"), "&H00FFC87E")
    primary = _ass_colour(a.get("text_color"), "&H00FFFFFF")
    margin_v = int(round(h * float(a.get("margin_bottom", 0.14))))

    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % w, "PlayResY: %d" % h,
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: K,%s,%d,%s,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,%d,2,2,"
        "60,60,%d,1" % (font, size, primary, outline, margin_v),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def ts(t):
        t = max(0.0, float(t))
        h_, rem = divmod(t, 3600)
        m_, s_ = divmod(rem, 60)
        return "%d:%02d:%05.2f" % (int(h_), int(m_), s_)

    glow = float(a.get("glow", 1.0))

    events = []
    for cue in payload:
        flat = [wd for ln in cue["lines"] for wd in ln]
        for i, live in enumerate(flat):
            start = live["s"] if i else cue["s"]
            end = flat[i + 1]["s"] if i + 1 < len(flat) else cue["e"]
            if end <= start:
                continue

            def line_for(live_tags, other_tags):
                """The whole cue, so libass lays it out identically every time."""
                parts, k = [], 0
                for li, ln in enumerate(cue["lines"]):
                    if li:
                        parts.append("\\N")
                    for wd in ln:
                        parts.append("{%s}%s{\\r}"
                                     % (live_tags if k == i else other_tags, wd["t"]))
                        k += 1
                return "".join(parts)

            if glow > 0.01:
                # The halo: the SAME cue, every word but the live one made fully
                # transparent, the live one blown out in the accent colour and blurred.
                # Rendering the whole line rather than the word alone is what keeps the
                # halo registered with the text - libass centres each line it is given,
                # so a lone word would sit in the middle of the frame.
                events.append(
                    "Dialogue: 0,%s,%s,K,,0,0,0,,%s"
                    % (ts(start), ts(end),
                       line_for("\\c%s\\3c%s\\bord%d\\blur%d\\fscx109\\fscy109\\alpha&H30&"
                                % (accent, accent, max(2, int(size * 0.10)),
                                   max(2, int(size * 0.13 * glow))),
                                "\\alpha&HFF&\\3a&HFF&\\4a&HFF&")))
            # the text itself, over the halo.
            # Text that simply appears is the flattest thing on the screen. A
            # fade plus a short scale settles it in instead, and in ASS it is
            # free: the browser title renderer costs ten seconds of startup
            # before it draws a single frame, for something libass adds in the
            # same pass as the caption it is already drawing.
            if entrance and abs(start - payload[0]["s"]) < 0.001:
                events.append("Dialogue: 1,%s,%s,K,,0,0,0,,"
                              "{\\fad(320,260)\\fscx108\\fscy108"
                              "\\t(0,300,\\fscx100\\fscy100)}%s"
                              % (ts(start), ts(end),
                                 line_for("\\c%s" % accent, "")))
            else:
                events.append("Dialogue: 1,%s,%s,K,,0,0,0,,%s"
                              % (ts(start), ts(end),
                                 line_for("\\c%s\\fscx109\\fscy109" % accent, "")))

    if text_only:
        return "\n".join(head + events) + "\n"

    tmp = _tmpdir()
    ass = os.path.join(tmp, "kin_%d.ass" % os.getpid())
    with io.open(ass, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(head + events) + "\n")

    # libass chokes on drive letters and backslashes inside a filter argument, so hand
    # it a bare filename from the file's own directory.
    cwd, name = os.path.dirname(ass), os.path.basename(ass)
    ff = ["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src),
          "-vf", subtitles_arg(name), "-c:a", "copy"] + VIDEO_ENC + \
         ["-movflags", "+faststart", os.path.abspath(out)]
    p = subprocess.run(ff, cwd=cwd, capture_output=True, text=True)
    try:
        os.remove(ass)
    except OSError:
        pass
    if p.returncode != 0:
        raise ToolError("Caption burn failed:\n" + (p.stderr or "").strip()[-400:])
    return len(events)


def _render_kinetic(a, src, payload, w, h, fps, total, font, npx, n_words, detected):
    n_words = n_words or sum(len(ln) for c in payload for ln in c["lines"])
    preview = " / ".join("".join(x["t"] for x in ln) for ln in payload[0]["lines"])

    if (a.get("engine") or "remotion").lower() == "fast":
        out = make_output(src, "kinetic", a.get("output"), ".mp4")
        n = _render_kinetic_ass(a, src, payload, w, h, total, font, out)
        return done(out, "Kinetic captions: %d word(s) in %s across %d cue(s), each lighting "
                         "up as it is spoken, with a halo on the live word.\n  Drawn by "
                         "libass in %d step(s) - no browser render, so roughly four times "
                         "quicker than 'remotion' for the same look.\n  First cue: %s"
                    % (n_words, detected, len(payload), n, preview))

    frames = max(fps, int(round(total * fps)))
    props = {"cues": payload,
             "accent": a.get("accent") or "#6cabe2",
             "textColor": a.get("text_color") or "#ffffff",
             "panel": a.get("panel") or "rgba(14,20,26,0.62)",
             "fontFamily": '"%s", "Leelawadee UI", Tahoma, sans-serif' % font,
             "fontScale": float(a.get("font_scale", 1.0)),
             "marginBottom": float(a.get("margin_bottom", 0.14)),
             "outline": float(a.get("outline", 0.075)),
             # The composition sizes itself from these via calculateMetadata.
             "durationInFrames": frames, "fps": fps, "width": w, "height": h}

    tmp = _tmpdir()
    props_file = os.path.join(tmp, "kprops_%d.json" % os.getpid())
    with io.open(props_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(props, ensure_ascii=False))

    # Render only the stretches that actually HAVE a caption. Measured on this
    # machine, a Remotion invocation costs 10.3s fixed plus 0.242s per frame, so a
    # gap is worth skipping once it is longer than the startup it would cost to
    # skip it - about 43 frames. On a typical ad captions cover around half the
    # running time, which took the render from 191s to 133s without touching
    # quality, and does LESS total work rather than simply using more of the CPU.
    startup_frames = 43
    runs = []
    for cue in sorted(payload, key=lambda c: c["s"]):
        lo = max(0, int(math.floor(cue["s"] * fps)) - 1)
        hi = min(frames - 1, int(math.ceil(cue["e"] * fps)) + 1)
        if runs and lo - runs[-1][1] <= startup_frames:
            runs[-1][1] = max(runs[-1][1], hi)
        else:
            runs.append([lo, hi])

    segments = []
    for i, (lo, hi) in enumerate(runs):
        seg = os.path.join(tmp, "kin_%d_%d.webm" % (os.getpid(), i))
        cmd = [npx, "remotion", "render", "KineticCaptions", seg,
               "--codec=vp8", "--pixel-format=yuva420p",
               "--props=%s" % props_file, "--frames=%d-%d" % (lo, hi), "--log=error"]
        try:
            p = subprocess.run(cmd, cwd=MOTION_DIR, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=3600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ToolError("The caption render timed out.")
        if p.returncode != 0 or not os.path.isfile(seg):
            tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
            raise ToolError("Remotion failed:\n" + "\n".join(tail))
        segments.append((seg, lo / float(fps)))

    out = make_output(src, "kinetic", a.get("output"), ".mp4")
    # libvpx by name: VP8 alpha lives in a separate WebM layer the default decoder
    # drops. itsoffset puts each rendered stretch back at the time it belongs to.
    args, chain, last = ["-i", src], [], "[0:v]"
    for i, (seg, at) in enumerate(segments, 1):
        args += ["-c:v", "libvpx", "-itsoffset", "%.3f" % at, "-i", seg]
        label = "[v%d]" % i if i < len(segments) else "[outv]"
        chain.append("%s[%d:v]overlay=0:0:eof_action=pass%s" % (last, i, label))
        last = label
    ffmpeg_run(args + ["-filter_complex", ";".join(chain), "-map", "[outv]",
                       "-map", "0:a?", "-c:a", "copy"] + VIDEO_ENC +
               ["-movflags", "+faststart", out])
    for f in [props_file] + [s for s, _t in segments]:
        try:
            os.remove(f)
        except OSError:
            pass
    return done(out, "Kinetic captions: %d word(s) in %s across %d cue(s), each lighting up "
                     "as it is spoken.\n  First cue: %s"
                % (n_words, detected, len(payload), preview))


def t_motion_title(a):
    """Animated title overlaid on the video, rendered with Remotion.

    drawtext can only place static text. This renders real motion - words springing in
    one after another - to a transparent WebM, then composites it over the footage.
    """
    src = check_input(a.get("path"), "video")
    title = (a.get("title") or "").strip()
    if not title:
        raise ToolError("Give a 'title' to animate.")
    if not os.path.isdir(os.path.join(MOTION_DIR, "node_modules")):
        raise ToolError("Remotion is not set up. In %s run:\n    npm install" % MOTION_DIR)
    node = shutil.which("node")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not node or not npx:
        raise ToolError("Node.js is required for animated titles but was not found.")

    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps") or 24)
    at = parse_time(a.get("at"), "at") or 0.0
    dur = float(a.get("duration", 3.0))
    frames = max(fps, int(round(dur * fps)))

    props = {"title": title,
             "subtitle": (a.get("subtitle") or "").strip(),
             "accent": a.get("accent") or "#6cabe2",
             "textColor": a.get("text_color") or "#ffffff",
             "stagger": int(a.get("stagger", 3)),
             # The composition sizes itself from these via calculateMetadata.
             "durationInFrames": frames, "fps": fps, "width": w, "height": h}
    tmp = _tmpdir()
    props_file = os.path.join(tmp, "props_%d.json" % os.getpid())
    with io.open(props_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(props, ensure_ascii=False))
    overlay = os.path.join(tmp, "title_%d.webm" % os.getpid())

    cmd = [npx, "remotion", "render", "AnimatedTitle", overlay,
           "--codec=vp8", "--pixel-format=yuva420p",
           "--props=%s" % props_file, "--log=error"]
    try:
        p = subprocess.run(cmd, cwd=MOTION_DIR, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError("The animation render timed out.")
    if p.returncode != 0 or not os.path.isfile(overlay):
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
        raise ToolError("Remotion failed:\n" + "\n".join(tail))

    out = make_output(src, "title", a.get("output"), ".mp4")
    # libvpx must be named explicitly: VP8 keeps alpha in a separate WebM layer and the
    # default decoder drops it, which silently gives an opaque black rectangle.
    ffmpeg_run(["-i", src, "-c:v", "libvpx", "-i", overlay,
                "-filter_complex",
                "[1:v]setpts=PTS-STARTPTS+%.3f/TB[ov];[0:v][ov]overlay=0:0:eof_action=pass" % at,
                "-map", "0:a?", "-c:a", "copy"] + VIDEO_ENC + [out])
    try:
        os.remove(overlay)
        os.remove(props_file)
    except OSError:
        pass
    return done(out, "Animated title over %.2fs-%.2fs. Words spring in %d frame(s) apart, "
                     "then the whole thing eases out."
                % (at, at + dur, props["stagger"]))


def t_end_card(a):
    """Append a branded end card, dissolving into it."""
    src = check_input(a.get("path"), "video")
    title = (a.get("title") or "").strip()
    subtitle = (a.get("subtitle") or "").strip()
    logo = a.get("logo")
    if logo:
        logo = check_input(logo, "logo")
    if not (title or subtitle or logo):
        raise ToolError("Give a 'title', 'subtitle' and/or 'logo' for the card.")

    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    dur = float(a.get("duration", 2.5))
    bg = _hex_to_ff(a.get("background") or "#0d1b2a")
    fg = a.get("text_color") or "white"
    fps = int(a.get("fps") or 24)

    font = r"C:\Windows\Fonts\tahomabd.ttf"
    card_tmp = _tmpdir()

    chain = ["[0:v]format=yuv420p[bg]"]
    last = "[bg]"
    if logo:
        chain.append("[1:v]scale=%d:-1[lg]" % int(w * float(a.get("logo_scale", 0.42))))
        chain.append("%s[lg]overlay=(W-w)/2:(H-h)/2-%d[withlogo]" % (last, int(h * 0.06)))
        last = "[withlogo]"

    def fit(text, size):
        """Shrink until the line fits. A Thai glyph advances roughly 0.58 x the size,
        and Thai has no spaces to wrap on, so an over-long line runs off both edges."""
        est = len(text) * 0.58 * size
        limit = w * 0.86
        return int(size * limit / est) if est > limit else size

    def draw(text, size, y, label_in, label_out, delay):
        # Read from a file rather than escaping inline: a title of "50% OFF" rendered as
        # nothing at all, because drawtext read the % as the start of an expansion.
        opts = {"fontsize": fit(text, size), "fontcolor": fg,
                "x": "(w-text_w)/2", "y": y,
                "alpha": "'min(1\\,max(0\\,(t-%.2f)/0.4))'" % delay}
        if os.path.isfile(font):
            opts["fontfile"] = "'%s'" % escape_filter_path(font)
        return "%s%s%s" % (label_in, drawtext_of(text, card_tmp, **opts), label_out)

    ty = "(h-text_h)/2+%d" % int(h * 0.10) if logo else "(h-text_h)/2-%d" % int(h * 0.02)
    if title:
        chain.append(draw(title, int(h * 0.055), ty, last, "[t1]", 0.25))
        last = "[t1]"
    if subtitle:
        sy = "(h-text_h)/2+%d" % int(h * (0.18 if logo else 0.07))
        chain.append(draw(subtitle, int(h * 0.030), sy, last, "[t2]", 0.55))
        last = "[t2]"
    chain.append("%sfade=t=in:st=0:d=0.4[outv]" % last)

    card = os.path.join(card_tmp, "card_%d.mp4" % os.getpid())
    args = ["-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=%d:d=%.2f" % (bg, w, h, fps, dur)]
    if logo:
        args += ["-i", os.path.abspath(logo)]
    args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-filter_complex", ";".join(chain), "-map", "[outv]",
             "-map", "%d:a" % (2 if logo else 1), "-t", "%.2f" % dur]
    args += VIDEO_ENC + AUDIO_ENC + [card]
    # Run from the temp folder: drawtext reads its text from bare-named files there.
    p = subprocess.run(["ffmpeg", "-y", "-v", "error"] + args, cwd=card_tmp,
                       capture_output=True, text=True)
    if p.returncode:
        raise ToolError("Building the end card failed:\n" + (p.stderr or "").strip()[-400:])

    out = make_output(src, "endcard", a.get("output"), ".mp4")
    try:
        t_join_smooth({"paths": [src, card], "transition": a.get("transition") or "fade",
                       "duration": float(a.get("transition_duration", 0.5)),
                       "fps": fps, "output": out})
    finally:
        try:
            os.remove(card)
        except OSError:
            pass
    return done(out, "Added a %.1fs end card and dissolved into it." % dur)


# ---------------------------------------------------------------- one-command edit
EDIT_STYLES = {
    "cinematic": "curves=r='0/0.04 0.5/0.5 1/0.96':b='0/0.06 0.5/0.52 1/1',"
                 "eq=contrast=1.125:saturation=1.12,vignette=PI/9,unsharp=5:5:0.5:5:5:0.2",
    "clean":     "eq=contrast=1.05:saturation=1.08,unsharp=5:5:0.4:5:5:0.2",
    "warm":      "colorbalance=rs=0.06:rm=0.04:bs=-0.05,eq=contrast=1.08:saturation=1.15",
    "punchy":    "eq=contrast=1.2:saturation=1.45,unsharp=5:5:0.7:5:5:0.3",
    "none":      "",
}


def t_auto_edit(a):
    """Cut raw clips into a finished piece: shots, dead air, dissolves, grade, captions."""
    if a.get("proof") and not a.get("_reentry"):
        # The final encode is only a slice of the work - the trims, levelling and join
        # dominate. Drop the encoder quality for all of them, not just the last step.
        global VIDEO_ENC
        saved = VIDEO_ENC
        VIDEO_ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                     "-pix_fmt", "yuv420p"]
        try:
            inner = dict(a)
            inner["_reentry"] = True
            return t_auto_edit(inner)
        finally:
            VIDEO_ENC = saved

    preset_name = a.get("preset")
    if preset_name:
        presets = _load_presets()
        if preset_name not in presets:
            raise ToolError("No preset called '%s'. Saved: %s"
                            % (preset_name, ", ".join(sorted(presets)) or "none"))
        merged = dict(presets[preset_name])
        merged.update({k: v for k, v in a.items() if v is not None})  # explicit wins
        a = merged

    paths = a.get("paths") or []
    if not paths:
        raise ToolError("Give one or more clips in 'paths'.")
    srcs = [check_input(p, "video") for p in paths]
    style = a.get("style") or "cinematic"
    if style not in EDIT_STYLES:
        raise ToolError("style must be one of: %s" % ", ".join(EDIT_STYLES))
    trans = a.get("transition") or "fade"
    tdur = float(a.get("transition_duration", 0.45))
    min_shot = float(a.get("min_shot", 0.9))
    pad = float(a.get("padding", 0.12))
    tmp = _tmpdir()
    log = []

    # 1. Split every clip on its own shot boundaries, then trim dead air off each shot.
    pieces = []
    for ci, src in enumerate(srcs):
        total = duration_of(src)
        cuts = _scene_cuts(src, float(a.get("shot_sensitivity", 8.0)))
        bounds = [0.0] + [c for c in cuts if 0.2 < c < total - 0.2] + [total]
        quiet = merge_spans(detect_silence(src, -34, 0.45)) if has_audio(src) else []
        log.append("%s: %d shot(s)" % (os.path.basename(src), len(bounds) - 1))
        for i in range(len(bounds) - 1):
            s, e = bounds[i], bounds[i + 1]
            # Pull the edges in past any silence that sits at the head or tail of the shot.
            for qs, qe in quiet:
                if qs <= s + 0.05 and qe > s:
                    s = min(qe - pad, e)
                if qe >= e - 0.05 and qs < e:
                    e = max(qs + pad, s)
            if e - s < min_shot:
                continue
            pieces.append((ci, s, e))

    # Balance the shots to each other. Grading them all identically still leaves one
    # clip cooler or darker than the next, and that mismatch is what reads as amateur.
    if a.get("match_colour", True) and len({p[0] for p in pieces}) > 1:
        cstats = {}
        for ci in {p[0] for p in pieces}:
            cstats[ci] = _colour_of(srcs[ci])
        vals = list(cstats.values())
        target = tuple(sorted(v[i] for v in vals)[len(vals) // 2] for i in range(3))
        colour_fix = {}
        for ci, st in cstats.items():
            chain, _d = _match_filter(st, target, float(a.get("match_strength", 1.0)))
            if chain:
                colour_fix[ci] = chain
        if colour_fix:
            log.append("colour matched %d of %d source clip(s) to a common look"
                       % (len(colour_fix), len(cstats)))
    else:
        colour_fix = {}

    # Tighten the air after each line, harder as the piece builds - then let the payoff
    # breathe. Uniform shot lengths read as flat; an edit should gather pace and land.
    if a.get("accelerate", True) and len(pieces) > 2:
        n_p = len(pieces)
        head_pad = float(a.get("open_pad", 0.55))    # room to establish
        tight_pad = float(a.get("tight_pad", 0.14))  # by the climax
        hold_pad = float(a.get("hold_pad", 0.75))    # the payoff holds
        tightened = []
        for i, (ci, s, e) in enumerate(pieces):
            if not has_audio(srcs[ci]):
                tightened.append((ci, s, e))
                continue
            quiet = merge_spans(detect_silence(srcs[ci], -34, 0.25))
            # How much silence to leave after the last word in this shot.
            frac = i / float(n_p - 1)
            pad = head_pad + (tight_pad - head_pad) * frac
            if i == n_p - 1:
                pad = hold_pad
            ne = e
            for qs, qe in quiet:
                if qs < e <= qe + 0.05 and qs > s:      # the shot ends in silence
                    ne = min(e, qs + pad)
                    break
            if ne - s >= min_shot:
                tightened.append((ci, s, ne))
            else:
                tightened.append((ci, s, e))
        saved = sum(e - s for _c, s, e in pieces) - sum(e - s for _c, s, e in tightened)
        pieces = tightened
        if saved > 0.05:
            log.append("pacing: trimmed %.2fs of trailing air, tightening toward the end"
                       % saved)

    # Nudge each cut onto a moment of movement, so the motion carries across the join.
    if a.get("cut_on_action", True) and len(pieces) > 1:
        moved = 0
        snapped = []
        for k, (ci, s, e) in enumerate(pieces):
            ns, ne = s, e
            if k > 0:
                cand, did = snap_to_action(srcs[ci], s)
                if did and ne - cand >= min_shot:
                    ns, moved = cand, moved + 1
            if k < len(pieces) - 1:
                cand, did = snap_to_action(srcs[ci], e)
                if did and cand - ns >= min_shot:
                    ne, moved = cand, moved + 1
            snapped.append((ci, ns, ne))
        pieces = snapped
        if moved:
            log.append("%d cut(s) moved onto a movement peak" % moved)

    if not pieces:
        raise ToolError("Nothing usable was found - every shot was shorter than %.2fs "
                        "or silent. Lower 'min_shot'." % min_shot)

    # --- what is actually being SAID ------------------------------------------
    # Everything above places cuts on pictures: scene changes, silence, movement. None
    # of it knows a sentence is in progress, so a cut lands mid-word and the line is
    # severed. Recognition is cached per file, so asking costs nothing after the first
    # pass, and it is the only way to tell an alternate take from a new beat.
    speech = {}
    if a.get("respect_speech", True):
        lang = (a.get("language") or "auto").strip().lower()
        model = a.get("subtitle_model") or "large-v3"
        for ci, src in enumerate(srcs):
            if not has_audio(src):
                continue
            try:
                speech[ci] = _word_timings(src, lang, model)[0]
            except (ToolError, Exception):
                pass

    if speech:
        def out_of_word(ci, t, lo, hi):
            """Push a cut to the nearer edge of any word it lands inside."""
            for w in speech.get(ci, ()):
                if w["s"] + 0.03 < t < w["e"] - 0.03:
                    cand = w["e"] if (t - w["s"]) > (w["e"] - t) else w["s"]
                    return cand if lo <= cand <= hi else t
            return t

        fixed, moved = [], 0
        for ci, s, e in pieces:
            ns = out_of_word(ci, s, s - 0.6, e - min_shot)
            ne = out_of_word(ci, e, ns + min_shot, e + 0.6)
            moved += (abs(ns - s) > 0.01) + (abs(ne - e) > 0.01)
            fixed.append((ci, ns, ne))
        pieces = fixed
        if moved:
            log.append("%d cut(s) moved off a word so no line is cut in half" % moved)

    # --- alternate takes ------------------------------------------------------
    # Four clips of the same scene repeat the same line four times. Comparing what is
    # SPOKEN inside each shot is what separates a genuine second beat from the same
    # performance shot again - the pictures are near-identical either way.
    repeats = {}
    if speech and a.get("drop_repeats", True) and len(pieces) > 1:
        import difflib

        def said(ci, s, e):
            return "".join(w["t"] for w in speech.get(ci, ())
                           if s <= (w["s"] + w["e"]) / 2.0 <= e).strip()

        spoken = [said(ci, s, e) for ci, s, e in pieces]
        for i in range(len(pieces)):
            if len(spoken[i]) < 6 or i in repeats:
                continue
            for j in range(i + 1, len(pieces)):
                if len(spoken[j]) < 6 or j in repeats:
                    continue
                if difflib.SequenceMatcher(None, spoken[i], spoken[j]).ratio() >= 0.80:
                    repeats[j] = i + 1        # keep the first, note what it repeats

    # Alternate takes of the same scene often repeat a line. Nothing here can tell
    # that from a deliberate repeat, so expose the shot list and let it be pruned.
    # --- fit a running time ---------------------------------------------------
    # The planner keeps everything it judges usable, which for four ten-second clips is
    # 35 seconds of ad. Nothing above has any notion of how long the thing should BE, so
    # the pruning fell to whoever read the plan. Given a target, drop the weakest shots
    # until it fits: silence goes before dialogue, and of two silent shots the one that
    # looks most like the shot before it goes first, because that is the one a viewer
    # will not miss.
    over = {}
    target = a.get("target_duration")
    if target and len(pieces) > 2:
        target = float(target)
        total_kept = sum(e - s for _c, s, e in pieces)
        if total_kept > target + 0.4:
            sig = {}
            for n, (ci, s, e) in enumerate(pieces):
                try:
                    g = _frame_gray(srcs[ci], s + (e - s) / 2.0, width=64)
                    sig[n] = None if g is None else g.astype("float32") / 255.0
                except Exception:
                    sig[n] = None

            def looks_like_previous(n):
                import numpy as np
                a_, b_ = sig.get(n), sig.get(n - 1)
                if a_ is None or b_ is None or a_.shape != b_.shape:
                    return 0.0
                return float(max(0.0, 1.0 - np.abs(a_ - b_).mean() * 4.0))

            def has_speech(n):
                ci, s, e = pieces[n]
                return any(s <= (w["s"] + w["e"]) / 2.0 <= e for w in speech.get(ci, ()))

            def movement(n):
                """How much the picture changes across the shot.

                Scoring on dialogue alone threw away a product being picked up and a
                character dancing while colleagues stared - the reveal and the joke,
                both silent, both the reason the ad works. Movement and novelty are
                what those two have instead of a line.
                """
                import numpy as np
                ci, s, e = pieces[n]
                try:
                    a_ = _frame_gray(srcs[ci], s + (e - s) * 0.25, width=64)
                    b_ = _frame_gray(srcs[ci], s + (e - s) * 0.75, width=64)
                    if a_ is None or b_ is None or a_.shape != b_.shape:
                        return 0.0
                    d = np.abs(a_.astype("float32") - b_.astype("float32")).mean() / 255.0
                    return float(min(1.0, d * 6.0))
                except Exception:
                    return 0.0

            def novelty(n):
                """Unlike BOTH neighbours - a shot carrying something not already seen."""
                before = looks_like_previous(n)
                after = looks_like_previous(n + 1) if n + 1 < len(pieces) else 0.0
                return 1.0 - max(before, after)

            protect = set()
            for tok in (a.get("protect_shots") or []):
                try:
                    protect.add(int(tok) - 1)
                except (TypeError, ValueError):
                    raise ToolError("protect_shots takes shot numbers, e.g. [3, 12].")
            # The opening and the closing shot are never candidates. An advert ends on
            # the product, and a held product shot is deliberately STILL - scoring it on
            # movement dropped exactly the frame the whole thing exists to show.
            protect.add(0)
            protect.add(len(pieces) - 1)

            # lower score = dropped sooner. Dialogue still leads. Novelty - being unlike
            # BOTH neighbours - is the sounder second signal: movement flatters a shaky
            # shot and punishes a composed one.
            scored = []
            for n in range(len(pieces)):
                ci, s, e = pieces[n]
                if n in protect:
                    continue                      # never offered up for trimming
                score = (2.0 if has_speech(n) else 0.0) + 1.2 * novelty(n)
                scored.append((score, e - s, n))
            scored.sort()                       # weakest first, shortest as tiebreak
            running = total_kept
            while scored:
                if running <= target + 0.4 or len(pieces) - len(over) <= 3:
                    break
                excess = running - target
                # Of the weak shots, take one that roughly FITS what has to go. Taking
                # the weakest regardless removed a 7.8s beat to shed 3s of excess, and
                # the beat it removed carried a line of dialogue.
                fits = [c for c in scored if c[1] <= excess + 1.5]
                score, span, n = (fits or scored)[0]
                scored.remove((score, span, n))
                over[n] = ("silent" if score < 2 else "has dialogue") + \
                          (", close to a neighbour" if novelty(n) < 0.5 else "")
                running -= span
            if over:
                log.append("target %.1fs: dropped %d shot(s) to fit, %.1fs -> %.1fs"
                           % (target, len(over), total_kept, running))

    drop = set()
    for tok in (a.get("drop_shots") or []):
        try:
            drop.add(int(tok))
        except (TypeError, ValueError):
            raise ToolError("drop_shots takes shot numbers, e.g. [3, 4]. Got %r." % tok)
    for j in repeats:
        drop.add(j + 1)
    for n in over:
        drop.add(n + 1)
    if repeats:
        log.append("dropped %d shot(s) that repeat an earlier line: %s"
                   % (len(repeats), ", ".join("%d repeats %d" % (j + 1, repeats[j])
                                              for j in sorted(repeats))))
    if a.get("plan_only") or drop:
        rows = []
        for n, (ci, s, e) in enumerate(pieces, 1):
            why = ""
            if n - 1 in repeats:
                why = "  <- same line as shot %d" % repeats[n - 1]
            elif n - 1 in over:
                why = "  <- cut for length (%s)" % over[n - 1]
            rows.append("%s%2d. %-34s %6.2f - %6.2f  (%.2fs)%s"
                        % ("DROP " if n in drop else "     ", n,
                           os.path.basename(srcs[ci])[:34], s, e, e - s, why))
        plan = ("Shot plan for %d clip(s):\n%s\n\nTotal kept: %.2fs across %d shot(s)."
                % (len(srcs), "\n".join(rows),
                   sum(e - s for n, (_c, s, e) in enumerate(pieces, 1) if n not in drop),
                   len([1 for n in range(1, len(pieces) + 1) if n not in drop])))
        if a.get("plan_only"):
            text = (plan + "\n\nNothing was rendered. Re-run without 'plan_only', passing "
                           "drop_shots=[n, ...] for any shot you do not want - useful when two "
                           "clips are alternate takes and repeat the same line.")
            # A list of timecodes is hard to judge. Show the shots.
            try:
                from PIL import Image
                shots = []
                for n, (ci, s, e) in enumerate(pieces, 1):
                    p = os.path.join(tmp, "plan_%d_%d.jpg" % (os.getpid(), n))
                    grab_frame(srcs[ci], s + min(0.4, (e - s) / 3.0), p, width=300)
                    im = Image.open(p).convert("RGB")
                    shots.append(_label(im, "%d%s  %.1fs" % (n, "  DROP" if n in drop else "",
                                                             e - s)))
                if shots:
                    cols = min(5, len(shots))
                    rows_n = (len(shots) + cols - 1) // cols
                    cw, ch = shots[0].width, shots[0].height
                    sheet = Image.new("RGB", (cols * cw + (cols + 1) * 6,
                                              rows_n * ch + (rows_n + 1) * 6), (24, 30, 38))
                    for i, im in enumerate(shots):
                        r_, c_ = divmod(i, cols)
                        sheet.paste(im, (6 + c_ * (cw + 6), 6 + r_ * (ch + 6)))
                    sheet_path = os.path.join(tmp, "plansheet_%d.jpg" % os.getpid())
                    sheet.save(sheet_path, quality=86)
                    return [{"type": "text", "text": text},
                            image_content(sheet_path, max_w=1400)]
            except Exception:
                pass          # a picture is a bonus; never fail the plan over it
            return text
        log.append("dropped shot(s): %s" % ", ".join(str(d) for d in sorted(drop)))
        pieces = [p for n, p in enumerate(pieces, 1) if n not in drop]
        if not pieces:
            raise ToolError("Every shot was dropped.")

    target = a.get("target_seconds")
    if target:
        target = float(target)
        kept, acc = [], 0.0
        for p in pieces:
            if acc >= target:
                break
            kept.append(p)
            acc += p[2] - p[1]
        dropped = len(pieces) - len(kept)
        if dropped:
            log.append("kept the first %d shot(s) to reach ~%.0fs, dropped %d"
                       % (len(kept), target, dropped))
        pieces = kept

    # 2. Cut each piece out, then level it so the joins do not jump in volume.
    seg_files = []
    for n, (ci, s, e) in enumerate(pieces):
        raw = os.path.join(tmp, "ae_%d_%d.mp4" % (os.getpid(), n))
        t_trim({"path": srcs[ci], "start": s, "end": e, "output": raw})
        # Balance this clip toward the others before anything else touches it.
        if colour_fix.get(ci):
            fixed = os.path.join(tmp, "ae_%d_%d_c.mp4" % (os.getpid(), n))
            try:
                ffmpeg_run(["-i", raw, "-vf", colour_fix[ci]] +
                           (["-c:a", "copy"] if has_audio(raw) else ["-an"]) +
                           VIDEO_ENC + [fixed])
                os.remove(raw)
                raw = fixed
            except ToolError:
                pass
        if has_audio(raw):
            lvl = os.path.join(tmp, "ae_%d_%d_l.mp4" % (os.getpid(), n))
            try:
                t_fix_audio({"path": raw, "output": lvl})
                seg_files.append(lvl)
                continue
            except ToolError:
                pass
        seg_files.append(raw)
    log.append("%d segment(s) cut and levelled" % len(seg_files))

    # 3. Join. How each junction is treated is an editorial decision, not a setting.
    joined = os.path.join(tmp, "ae_join_%d.mp4" % os.getpid())
    if len(seg_files) == 1:
        shutil.copyfile(seg_files[0], joined)
    else:
        pacing = (a.get("pacing") or "editorial").lower()
        join_args = {"paths": seg_files, "transition": trans, "duration": tdur,
                     "aspect": a.get("aspect"), "fps": int(a.get("fps") or 24),
                     "output": joined}
        if pacing == "editorial":
            # Cutting inside a continuous scene should be a HARD cut - a dissolve there
            # reads as amateur. Reserve the dissolve for an actual change of scene,
            # which here means the material came from a different source clip.
            js = []
            for i in range(len(pieces) - 1):
                same_scene = pieces[i][0] == pieces[i + 1][0]
                js.append({"transition": trans,
                           "duration": 0.0 if same_scene else tdur})
            join_args["junctions"] = js
            # Audio crosses over a longer window than the picture, so each join is
            # heard slightly before it is seen.
            join_args["audio_crossfade"] = float(a.get("audio_lead", 0.32))
            hard = sum(1 for j in js if j["duration"] <= 0.05)
            log.append("joined: %d hard cut(s) inside scenes, %d dissolve(s) between them, "
                       "audio leading by %.2fs" % (hard, len(js) - hard,
                                                   join_args["audio_crossfade"]))
        else:
            log.append("joined with %.2fs '%s' transitions" % (tdur, trans))
        t_join_smooth(join_args)

    # 4. Captions before grading would get graded too, so transcribe from the joined cut.
    srt = None
    lang = a.get("subtitles")
    if isinstance(lang, str) and lang.strip().lower() in ("none", "off", "no", ""):
        lang = None                     # lets a run opt out of a preset's captions
    if lang is False:
        lang = None
    if lang:
        try:
            cap_font = a.get("font") or SUBTITLE_PRESETS.get(
                a.get("subtitle_style") or "premium", {}).get("font")
            res = t_auto_subtitles({"path": joined, "language": lang,
                                    "model": a.get("subtitle_model") or "large-v3",
                                    "burn": False,
                                    "max_chars_per_line": int(a.get("max_chars_per_line")
                                                              or caption_width_for(cap_font, 16)),
                                    "max_seconds_per_line": 2.4,
                                    "srt_output": os.path.join(tmp, "ae_%d.srt" % os.getpid())})
            srt = res.split("-> ", 1)[1].splitlines()[0].strip()
            log.append("subtitles written (%s)" % lang)
        except ToolError as e:
            log.append("subtitles skipped: %s" % str(e).splitlines()[0])

    # 5. Grade, burn captions, fade top and tail - one pass, so no extra generation loss.
    dur = duration_of(joined)
    chain = [c for c in [EDIT_STYLES[style]] if c]
    base_chain = list(chain)            # same grade and fades, minus the captions
    if srt and os.path.isfile(srt):
        # video_auto_subtitles already wrapped these on syllable boundaries; re-wrapping
        # by character count here would split Thai words back apart.
        wrapped = srt
        chain.append("subtitles='%s':force_style='%s'"
                     % (escape_filter_path(wrapped),
                        _style_string(a.get("subtitle_style") or "premium",
                                      font=a.get("font"))))
    fi, fo = float(a.get("fade_in", 0.5)), float(a.get("fade_out", 0.6))
    for c in ([("fade=t=in:st=0:d=%.2f" % fi) if fi > 0 else None,
               ("fade=t=out:st=%.2f:d=%.2f" % (dur - fo, fo)) if (fo > 0 and dur > fo) else None]):
        if c:
            chain.append(c)
            base_chain.append(c)

    proof = bool(a.get("proof"))
    out = make_output(srcs[0], "proof" if proof else "autoedit", a.get("output"), ".mp4")
    # Burned-in captions cannot be removed later, so keep a caption-free master too.
    # Reworking the wording then needs only a re-burn, not a whole re-edit.
    clean_master = None
    if srt and os.path.isfile(srt) and not proof and a.get("keep_clean_master", True):
        clean_master = os.path.splitext(out)[0] + "_nosubs.mp4"
    if srt and os.path.isfile(srt):
        # Keep the captions beside the video so they can be corrected and re-burned.
        keep = os.path.splitext(out)[0] + ".srt"
        try:
            shutil.copyfile(wrapped, keep)
            log.append("captions saved to %s" % os.path.basename(keep))
        except (OSError, NameError):
            pass
    args = ["-i", joined]
    if chain:
        args += ["-vf", ",".join(chain)]
    if has_audio(joined):
        af = []
        if fi > 0:
            af.append("afade=t=in:st=0:d=%.2f" % min(fi, 0.4))
        if fo > 0 and dur > fo:
            af.append("afade=t=out:st=%.2f:d=%.2f" % (dur - fo, fo))
        if af:
            args += ["-af", ",".join(af)]
        args += AUDIO_ENC + ["-ar", "48000"]
    if proof:
        # PROOF_ENC carries its own -vf, so fold the filter chain into it.
        enc = list(PROOF_ENC)
        if chain:
            args = [x for x in args if x != ",".join(chain)]
            args = [x for x in args if x != "-vf"]
            enc[enc.index("-vf") + 1] = ",".join(chain) + ",scale=540:-2"
        args += enc + [out]
    else:
        args += VIDEO_ENC + ["-movflags", "+faststart", out]
    ffmpeg_run(args)

    if clean_master:
        # Identical grade and fades, captions omitted - rendered from the same source
        # so it stays in sync with the captioned cut frame for frame.
        cargs = ["-i", joined]
        if base_chain:
            cargs += ["-vf", ",".join(base_chain)]
        if has_audio(joined):
            af = []
            if fi > 0:
                af.append("afade=t=in:st=0:d=%.2f" % min(fi, 0.4))
            if fo > 0 and dur > fo:
                af.append("afade=t=out:st=%.2f:d=%.2f" % (dur - fo, fo))
            if af:
                cargs += ["-af", ",".join(af)]
            cargs += AUDIO_ENC + ["-ar", "48000"]
        cargs += VIDEO_ENC + ["-movflags", "+faststart", clean_master]
        try:
            ffmpeg_run(cargs)
            log.append("caption-free master: %s" % os.path.basename(clean_master))
        except ToolError:
            clean_master = None

    for f in seg_files:
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.remove(joined)
    except OSError:
        pass

    # 6-8. Sound design, end card, then levelling - applied identically to the captioned
    # cut and to the caption-free master so the two stay interchangeable.
    sfx_name = a.get("sfx")
    mood = a.get("music")

    def finish(path, note):
        steps = []
        if sfx_name:
            try:
                stage = os.path.join(tmp, "ae_sfx_%d.mp4" % os.getpid())
                t_add_sfx({"path": path,
                           "on_transitions": (sfx_name if isinstance(sfx_name, str) else "whoosh"),
                           "gain": float(a.get("sfx_gain", 0.95)), "output": stage})
                shutil.move(stage, path)
                steps.append("sound effects on the transitions")
            except ToolError as e:
                steps.append("sfx skipped: %s" % str(e).splitlines()[0])

        if mood:
            try:
                bed = os.path.join(tmp, "ae_bed_%d.mp3" % os.getpid())
                t_music_generate({"mood": mood, "duration": duration_of(path) + 0.5,
                                  "output": bed})
                staged = os.path.join(tmp, "ae_mus_%d.mp4" % os.getpid())
                t_add_music({"path": path, "music": bed,
                             "volume": float(a.get("music_level", 0.22)),
                             "keep_original_audio": True, "output": staged})
                shutil.move(staged, path)
                try:
                    os.remove(bed)
                except OSError:
                    pass
                steps.append("'%s' music bed underneath" % mood)
            except ToolError as e:
                steps.append("music skipped: %s" % str(e).splitlines()[0])

        # End card BEFORE levelling: appending it re-encodes the audio and can push
        # peaks back up, so levelling first would leave the result clipping.
        added = False
        if a.get("end_title") or a.get("end_subtitle") or a.get("end_logo"):
            try:
                staged = os.path.join(tmp, "ae_end_%d.mp4" % os.getpid())
                t_end_card({"path": path, "title": a.get("end_title"),
                            "subtitle": a.get("end_subtitle"), "logo": a.get("end_logo"),
                            "background": a.get("end_background") or "#0d2b4a",
                            "text_color": a.get("end_text_color"),
                            "duration": float(a.get("end_duration", 2.5)),
                            "output": staged})
                shutil.move(staged, path)
                added = True
                steps.append("end card appended")
            except ToolError as e:
                steps.append("end card skipped: %s" % str(e).splitlines()[0])

        if (sfx_name or mood or added) and has_audio(path):
            try:
                final = os.path.join(tmp, "ae_lvl_%d.mp4" % os.getpid())
                t_fix_audio({"path": path, "output": final})
                shutil.move(final, path)
                steps.append("levelled last, after every audio step")
            except ToolError as e:
                steps.append("final levelling skipped: %s" % str(e).splitlines()[0])
        if note:
            log.extend(steps)

    finish(out, True)
    if clean_master and os.path.isfile(clean_master):
        finish(clean_master, False)

    report = t_check({"path": out})
    return done(out, "Auto-edited %d clip(s) -> %.2fs.%s\n  %s\n\n%s"
                % (len(srcs), duration_of(out),
                   ("\n  preset: %s" % preset_name) if preset_name else "",
                   "\n  ".join(log), report))


# ---------------------------------------------------------------- sound design
# Synthesised rather than sampled: no library to download, no licence to worry about.
# Each entry is (expression, seconds, shaping filters). Commas inside a function call
# must be escaped as \, or aevalsrc reads them as its own option separator.
_E = "\\,"          # aevalsrc treats a bare comma as its own option separator


def _saw(f, n=9):
    """Sawtooth from summed harmonics - brass and horns live in the upper partials."""
    return "+".join("sin(2*PI*%.3f*t)/%d" % (f * k, k) for k in range(1, n + 1))


SFX_LIBRARY = {
    # --- building blocks -------------------------------------------------
    "impact":   ("0.85*sin(2*PI*(150*exp(-7*t))*t)*exp(-4.5*t)", 1.40, "highpass=f=25"),
    "sub_drop": ("0.9*sin(2*PI*(90*exp(-2.2*t))*t)*exp(-1.6*t)", 2.00, "highpass=f=20"),
    "whoosh":   ("0.9*(random(0)*2-1)*exp(-((t-0.38)*(t-0.38))/0.016)", 0.78,
                 "bandpass=f=700:width_type=o:w=1.4,lowpass=f=3000"),
    "swoosh":   ("0.9*(random(0)*2-1)*exp(-((t-0.22)*(t-0.22))/0.006)", 0.46,
                 "bandpass=f=1400:width_type=o:w=1.3,lowpass=f=5000"),
    "riser":    ("0.85*(random(0)*2-1)*pow(t/1.6%s2.6)" % _E, 1.60,
                 "bandpass=f=1600:width_type=o:w=2.0"),
    "pop":      ("0.7*sin(2*PI*760*t)*exp(-42*t)", 0.22, "highpass=f=180"),
    "click":    ("0.6*(random(0)*2-1)*exp(-160*t)", 0.10, "highpass=f=900"),
    "sparkle":  ("0.22*(sin(2*PI*3140*t)*exp(-9*t)+sin(2*PI*4710*t)*exp(-12*t)"
                 "+sin(2*PI*6280*t)*exp(-15*t))", 1.10, "highpass=f=1500"),
    "thud":     ("0.8*sin(2*PI*70*t)*exp(-11*t)", 0.50, "lowpass=f=220"),
    # --- the ones editors actually reach for ------------------------------
    "vine_boom": ("0.95*sin(2*PI*(62*exp(-3.2*t))*t)*exp(-3.0*t)", 1.60, "lowpass=f=320"),
    "braam":     ("0.10*((%s)+(%s)+(%s))*min(t/0.35%s1)*exp(-1.1*t)"
                  % (_saw(55), _saw(55.4), _saw(54.6), _E), 2.60, "lowpass=f=2200"),
    "bass_808":  ("0.9*sin(2*PI*(45+95*exp(-22*t))*t)*exp(-1.9*t)", 1.80, "lowpass=f=260"),
    "reverse_cymbal": ("0.8*(random(0)*2-1)*pow(t/1.8%s3.0)" % _E, 1.80,
                       "highpass=f=1200,bandpass=f=5000:width_type=o:w=2.4"),
    "tape_stop": ("0.8*sin(2*PI*(430*exp(-5.5*t))*t)*exp(-1.6*t)", 0.90, "lowpass=f=2600"),
    "record_scratch": ("0.7*(sin(2*PI*(320+240*sin(2*PI*7*t))*t))*exp(-3.2*t)", 0.75,
                       "bandpass=f=1100:width_type=o:w=2.2"),
    "glitch":    ("0.8*(random(0)*2-1)*mod(floor(t*26)%s2)" % _E, 0.60,
                  "bandpass=f=2400:width_type=o:w=2.6"),
    "ding":      ("0.30*(sin(2*PI*1046*t)*exp(-3.4*t)+0.6*sin(2*PI*2887*t)*exp(-5.0*t)"
                  "+0.35*sin(2*PI*5648*t)*exp(-7.0*t))", 2.20, "highpass=f=500"),
    "coin":      ("0.5*(sin(2*PI*1050*t)*exp(-26*t)+sin(2*PI*1580*t)*exp(-9*t))", 0.70,
                  "highpass=f=600"),
    "heartbeat": ("0.9*(sin(2*PI*58*t)*exp(-13*t)+0.8*sin(2*PI*54*(t-0.34))"
                  "*exp(-13*(t-0.34))*gt(t%s0.34))" % _E, 1.10, "lowpass=f=180"),
    "error_buzz": ("0.5*sin(2*PI*110*t)*mod(floor(t*22)%s2)*exp(-1.4*t)" % _E, 0.90,
                   "lowpass=f=1400"),
    "shutter":   ("0.85*(random(0)*2-1)*(exp(-190*t)+0.7*exp(-150*(t-0.055))*gt(t%s0.055))" % _E,
                  0.22, "highpass=f=1100"),
    "typewriter": ("0.7*(random(0)*2-1)*exp(-240*t)", 0.07,
                   "bandpass=f=2200:width_type=o:w=1.6"),
    "airhorn":   ("0.12*(%s)*min(t/0.06%s1)*min((1.3-t)/0.25%s1)" % (_saw(233, 12), _E, _E),
                  1.30, "bandpass=f=1500:width_type=o:w=3"),
    "suspense_sting": ("0.16*(sin(2*PI*(220+90*t)*t)+sin(2*PI*(311+127*t)*t))*pow(t/1.8%s2.0)" % _E,
                       1.80, "highpass=f=140"),
}

# How editors actually stack them: (sound, offset from the hit point, relative gain).
# A whoosh leads INTO the cut and the impact lands ON it.
SFX_COMBOS = {
    "transition":  [("whoosh", -0.45, 0.9), ("impact", 0.0, 0.7)],
    "hard_cut":    [("swoosh", -0.25, 0.9), ("thud", 0.0, 0.8)],
    "reveal":      [("riser", -1.45, 0.8), ("impact", 0.0, 0.95), ("sparkle", 0.05, 0.5)],
    "drop":        [("reverse_cymbal", -1.70, 0.8), ("bass_808", 0.0, 1.0)],
    "punchline":   [("vine_boom", 0.0, 1.0)],
    "emphasis":    [("braam", 0.0, 0.9)],
    "suspense":    [("suspense_sting", -1.60, 0.85), ("sub_drop", 0.0, 0.8)],
    "glitch_cut":  [("glitch", -0.25, 0.9), ("tape_stop", 0.0, 0.7)],
}


# What separates a synth beep from a produced effect is not the waveform - it is
# LAYERS (sub + body + transient), STEREO width, and a decay TAIL in a space.
# Each recipe adds extra layers on top of the base expression, plus room and width.
#   layers: (expression, gain) rendered alongside the base
#   room:   (delays_ms, decays) for the reverb tail
#   width:  0 = mono, 1 = fully decorrelated stereo
SFX_RECIPES = {
    "impact": dict(
        layers=[("0.55*sin(2*PI*(58*exp(-4*t))*t)*exp(-2.6*t)", 1.0),       # sub weight
                ("0.35*(random(0)*2-1)*exp(-70*t)", 0.8)],                  # transient crack
        room=("55|110|190|300", "0.45|0.30|0.18|0.10"), width=0.35),
    "vine_boom": dict(
        layers=[("0.4*sin(2*PI*(40*exp(-2.5*t))*t)*exp(-2.2*t)", 1.0)],
        room=("70|140|240", "0.40|0.25|0.14"), width=0.25),
    "braam": dict(
        layers=[("0.25*sin(2*PI*(110*exp(-0.5*t))*t)*min(t/0.4\\,1)*exp(-1.0*t)", 0.9)],
        room=("90|180|320|520", "0.50|0.36|0.24|0.14"), width=0.75),
    "bass_808": dict(
        layers=[("0.3*(random(0)*2-1)*exp(-90*t)", 0.6)],                   # click attack
        room=("40|85", "0.25|0.14"), width=0.2),
    "whoosh": dict(
        layers=[("0.5*(random(0)*2-1)*exp(-((t-0.42)*(t-0.42))/0.030)", 0.8)],
        room=("60|120|210", "0.40|0.26|0.15"), width=0.95),
    "swoosh": dict(
        layers=[], room=("45|95|160", "0.35|0.22|0.12"), width=0.95),
    "riser": dict(
        layers=[("0.35*sin(2*PI*(180+900*pow(t/1.6\\,2))*t)*pow(t/1.6\\,2.4)", 0.8)],
        room=("70|150|260", "0.42|0.28|0.16"), width=0.85),
    "reverse_cymbal": dict(
        layers=[], room=("50|110", "0.30|0.18"), width=0.9),
    "sparkle": dict(
        layers=[("0.12*sin(2*PI*7850*t)*exp(-18*t)", 0.7)],
        room=("80|170|300|470", "0.45|0.32|0.20|0.12"), width=0.8),
    "thud": dict(
        layers=[("0.25*(random(0)*2-1)*exp(-95*t)", 0.5)],
        room=("45|95", "0.28|0.15"), width=0.25),
    "ding": dict(
        layers=[], room=("110|230|400|650", "0.50|0.36|0.24|0.15"), width=0.6),
    "sub_drop": dict(layers=[], room=("60|130", "0.30|0.16"), width=0.2),
    "glitch": dict(layers=[], room=("25|55", "0.22|0.12"), width=0.9),
    "shutter": dict(layers=[], room=("30|65", "0.25|0.12"), width=0.5),
    "airhorn": dict(layers=[], room=("90|190|330", "0.45|0.30|0.18"), width=0.7),
    "suspense_sting": dict(layers=[], room=("120|250|430", "0.50|0.34|0.20"), width=0.8),
}


def render_sfx(name, dest, gain=1.0):
    """Render one effect: layers, stereo width, a room tail, then peak-levelled."""
    if name not in SFX_LIBRARY and os.path.isfile(name):
        # A real recording from the user's own folder. Level it the same way as the
        # synthesised set, so a gain of 0.8 means the same thing whichever it is.
        peak = _peak_db(name)
        lift = 0.0 if peak is None else max(-6.0, min(24.0, -1.0 - peak))
        ffmpeg_run(["-i", name, "-af", "volume=%.2fdB,volume=%.3f,alimiter=limit=0.92"
                    % (lift, gain), "-ac", "2", "-ar", "48000", dest])
        return duration_of(dest)
    if name not in SFX_LIBRARY:
        raise ToolError("Unknown sound. Choose from: %s" % ", ".join(sorted(SFX_LIBRARY)))
    expr, dur, extra = SFX_LIBRARY[name]
    rec = SFX_RECIPES.get(name, {})
    layers = rec.get("layers") or []
    width = float(rec.get("width", 0.0))
    room = rec.get("room")
    tmp = _tmpdir()
    tail = 0.55 if room else 0.0
    full = dur + tail

    # Mix the layers down to one mono source first.
    inputs, mixes = [], []
    allexpr = [(expr, 1.0)] + [(e, g) for e, g in layers]
    for j, (e, g) in enumerate(allexpr):
        inputs += ["-f", "lavfi", "-i", "aevalsrc=%s:d=%.3f:s=48000" % (e, full)]
        mixes.append("[%d:a]volume=%.3f[l%d]" % (j, g, j))
    chain = list(mixes)
    chain.append("%samix=inputs=%d:duration=longest:normalize=0[mix]"
                 % ("".join("[l%d]" % j for j in range(len(allexpr))), len(allexpr)))
    chain.append("[mix]%s,afade=t=out:st=%.3f:d=0.08[out]"
                 % (extra if extra else "anull", max(0.0, full - 0.08)))
    mono = os.path.join(tmp, "sfxmono_%d_%s.wav" % (os.getpid(), name))
    script = os.path.join(tmp, "sfxs_%d_%s.txt" % (os.getpid(), name))
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(chain))
    ffmpeg_run(inputs + ["-filter_complex_script", script, "-map", "[out]",
                         "-ac", "1", "-ar", "48000", mono])
    try:
        os.remove(script)
    except OSError:
        pass

    # Width comes from the REVERB ONLY; the dry hit stays centred and identical in both
    # channels. Delaying the dry signal for width (the Haas trick) comb-filters against
    # itself, and phone speakers are mono - measured up to 12 dB of cancellation, which
    # made the quietest effects disappear entirely.
    raw = os.path.join(tmp, "sfxraw_%d_%s.wav" % (os.getpid(), name))
    if room and width > 0:
        delays, decays = room
        spread = 1.0 + 0.45 * width
        alt = "|".join("%d" % max(1, int(float(d) * spread)) for d in delays.split("|"))
        graph = ("[0:a]asplit=3[d][w1][w2];"
                 "[d]asplit=2[dl][dr];[dl][dr]amerge=inputs=2[dry];"
                 "[w1]aecho=0.85:0.88:%s:%s[wl];"
                 "[w2]aecho=0.85:0.88:%s:%s[wr];"
                 "[wl][wr]amerge=inputs=2[wet];"
                 "[dry][wet]amix=inputs=2:weights=1 %.2f:normalize=0,"
                 "aformat=channel_layouts=stereo[o]"
                 % (delays, decays, alt, decays, 0.35 + 0.45 * width))
    elif room:
        delays, decays = room
        graph = ("[0:a]aecho=0.85:0.88:%s:%s,asplit=2[a][b];"
                 "[a][b]amerge=inputs=2,aformat=channel_layouts=stereo[o]"
                 % (delays, decays))
    else:
        graph = "[0:a]asplit=2[a][b];[a][b]amerge=inputs=2,aformat=channel_layouts=stereo[o]"
    ffmpeg_run(["-i", mono, "-filter_complex", graph, "-map", "[o]", "-ar", "48000", raw])
    sides = [mono]

    peak = _peak_db(raw)
    lift = 0.0 if peak is None else max(-6.0, min(24.0, -1.0 - peak))
    ffmpeg_run(["-i", raw, "-af", "volume=%.2fdB,volume=%.3f,alimiter=limit=0.92"
                % (lift, gain), "-ac", "2", "-ar", "48000", dest])
    for f in set(sides + [raw]):
        try:
            os.remove(f)
        except OSError:
            pass
    return full


def t_sfx_library(a):
    """Write the whole sound-effect set to a folder so it can be auditioned."""
    folder = a.get("folder")
    if not folder:
        raise ToolError("Give a 'folder' to write the sounds into.")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    os.makedirs(folder, exist_ok=True)
    rows = []
    for name in sorted(SFX_LIBRARY):
        dest = os.path.join(folder, name + ".wav")
        dur = render_sfx(name, dest)
        rows.append("  %-10s %.2fs  %s" % (name, dur, dest))
    return ("Generated %d sound effect(s) into %s\n%s\n\n"
            "These are synthesised on this PC - no download, nothing to licence."
            % (len(SFX_LIBRARY), folder, "\n".join(rows)))


# ---------------------------------------------------------------- brand presets
PRESET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")
PRESET_KEYS = ["style", "subtitles", "subtitle_style", "subtitle_model", "max_chars_per_line",
               "sfx", "sfx_gain", "music", "music_level", "transition",
               "transition_duration", "aspect", "fps", "fade_in", "fade_out",
               "end_title", "end_subtitle", "end_background", "end_text_color",
               "end_duration", "end_logo"]


def _load_presets():
    if not os.path.isfile(PRESET_FILE):
        return {}
    try:
        return json.load(io.open(PRESET_FILE, encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_presets(d):
    with io.open(PRESET_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(d, ensure_ascii=False, indent=2))


MEDIA_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
              ".mp4", ".mov", ".mkv", ".webm",
              ".png", ".jpg", ".jpeg", ".webp",
              ".ttf", ".otf", ".srt", ".ass")


def t_download_asset(a):
    """Fetch a media file from a URL you chose.

    Deliberately only fetches media - never an archive or anything executable - and
    never picks the file itself. Which asset (and therefore which licence) is your
    call, because an automated grab cannot tell CC0 from CC-BY-NC in a search result.
    """
    import urllib.request
    import urllib.error

    url = (a.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("Give an http(s) 'url'.")
    folder = a.get("folder")
    if not folder:
        raise ToolError("Give a 'folder' to save into, e.g. your sfx or music folder.")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    os.makedirs(folder, exist_ok=True)

    name = a.get("filename") or os.path.basename(url.split("?")[0].split("#")[0])
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name).strip() or "asset"
    ext = os.path.splitext(name)[1].lower()
    if ext not in MEDIA_EXTS:
        raise ToolError("Only media files are fetched (%s). Got '%s'. Archives and anything "
                        "executable are refused on purpose." % (", ".join(MEDIA_EXTS), ext or "no extension"))

    limit = int(float(a.get("max_mb", 120)) * 1024 * 1024)
    dest = os.path.join(folder, name)
    n = 2
    while os.path.exists(dest):
        stem, e = os.path.splitext(name)
        dest = os.path.join(folder, "%s_%d%s" % (stem, n, e))
        n += 1

    req = urllib.request.Request(url, headers={"User-Agent": "video-editor-mcp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > limit:
                raise ToolError("That file is %.1f MB, over the %.0f MB limit."
                                % (int(declared) / 1e6, limit / 1e6))
            got = 0
            with io.open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        fh.close()
                        os.remove(dest)
                        raise ToolError("Download exceeded the %.0f MB limit; stopped."
                                        % (limit / 1e6))
                    fh.write(chunk)
    except urllib.error.HTTPError as e:
        raise ToolError("The server refused it (HTTP %s). Many sites block direct "
                        "downloads - use a direct file link." % e.code)
    except urllib.error.URLError as e:
        raise ToolError("Could not reach it: %s" % e.reason)

    if not ctype.startswith(("audio/", "video/", "image/", "font/", "text/", "application/octet-stream")):
        os.remove(dest)
        raise ToolError("That URL returned '%s', not a media file - probably a web page "
                        "rather than a direct file link." % (ctype or "unknown"))

    note = ""
    try:
        if ext in AUDIO_EXTS + (".mp4", ".mov", ".mkv", ".webm"):
            info = analyse_audio(dest) if ext in AUDIO_EXTS else None
            if info:
                note = "\n  %d BPM, energy %.2f, %.1fs" % (info["bpm"], info["energy"],
                                                           info["duration"])
    except ToolError:
        pass
    return ("Downloaded -> %s  (%s, %s)%s\n\n"
            "Check the licence on the page you took this from before using it in a "
            "commercial video - that part is not something I can verify for you."
            % (dest, human_size(dest), ctype or "unknown type", note))


def t_brand_preset(a):
    """Save, list or delete a house style so every video comes out consistent."""
    action = (a.get("action") or "list").lower()
    presets = _load_presets()

    if action == "list":
        if not presets:
            return ("No presets yet. Save one with action='save', and every setting you pass "
                    "- grade, captions, sound, end card - is remembered under that name.")
        rows = []
        for name, cfg in sorted(presets.items()):
            bits = ", ".join("%s=%s" % (k, cfg[k]) for k in PRESET_KEYS if k in cfg)
            rows.append("  %-16s %s" % (name, bits or "(empty)"))
        return "Saved presets:\n%s" % "\n".join(rows)

    name = a.get("name")
    if not name:
        raise ToolError("Give a preset 'name'.")

    if action == "delete":
        if name not in presets:
            raise ToolError("No preset called '%s'." % name)
        del presets[name]
        _save_presets(presets)
        return "Deleted preset '%s'." % name

    if action == "save":
        cfg = {k: a[k] for k in PRESET_KEYS if a.get(k) is not None}
        if not cfg:
            raise ToolError("Nothing to save - pass the settings you want remembered.")
        presets[name] = cfg
        _save_presets(presets)
        return ("Saved '%s':\n%s\n\nUse it with video_auto_edit(preset='%s'). Anything you "
                "pass alongside overrides the preset for that one run."
                % (name, "\n".join("  %-22s %s" % (k, v) for k, v in sorted(cfg.items())), name))

    raise ToolError("action must be save, list or delete.")


# ---------------------------------------------------------------- cover frame
def t_transcript(a):
    """Word-level transcript with timings, for rewriting captions by meaning.

    Splitting captions on sense rather than on pauses needs someone who reads the
    language; this hands over the raw material to do that with.
    """
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This file has no audio to transcribe.")
    lang = (a.get("language") or "auto").strip().lower()
    size = a.get("model") or "large-v3"
    frags, detected = _asr_frags(src, lang, size)
    if not frags:
        raise ToolError("No speech found.")
    words = [[s, e, t] for s, e, t in frags]

    full = "".join(t for _s, _e, t in words).strip()
    lines = ["Transcript of %s (%s)" % (os.path.basename(src), detected), ""]
    lines.append("Full text:")
    lines.append("  " + full)
    lines.append("")
    if a.get("words", True):
        lines.append("Word timings - use these to write caption cues that break on meaning:")
        for s, e, t in words:
            lines.append("  %6.2f %6.2f  %s" % (s, e, t))
        lines.append("")
    gaps = [(words[i][1], words[i + 1][0]) for i in range(len(words) - 1)
            if words[i + 1][0] - words[i][1] > 0.35]
    if gaps:
        lines.append("Pauses over 0.35s (natural caption boundaries):")
        for s, e in gaps[:20]:
            lines.append("  %6.2f - %6.2f  (%.2fs)" % (s, e, e - s))
    return "\n".join(lines)


def t_video_cover(a):
    """Pick the strongest still from a video to use as the post thumbnail."""
    import numpy as np
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    skip = float(a.get("skip_edges", 0.6))
    frames, (sw, sh) = _decode_gray(src, fps=4, width=320)
    n = frames.shape[0]
    cuts = set(int(round(c * 4)) for c in _scene_cuts(src, 3.0))

    best_i, best_score = None, -1e9
    for i in range(n):
        t = i / 4.0
        if t < skip or t > total - skip:
            continue
        if any(abs(i - c) <= 2 for c in cuts):
            continue                        # mid-dissolve frames are soft and muddled
        f = frames[i].astype("float32")
        # Sharpness: how much fine detail survives. Blurred frames score low.
        gx = np.abs(np.diff(f, axis=1)).mean()
        gy = np.abs(np.diff(f, axis=0)).mean()
        sharp = float(gx + gy)
        # Prefer something happening in the middle of frame, where a subject sits.
        h, w = f.shape
        centre = f[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
        contrast = float(centre.std())
        bright = float(f.mean())
        penalty = abs(bright - 128) / 128.0 * 40.0      # very dark or blown frames
        score = sharp * 2.2 + contrast - penalty
        if score > best_score:
            best_score, best_i = score, i

    if best_i is None:
        raise ToolError("Could not find a usable frame.")
    at = best_i / 4.0
    out = a.get("output") or os.path.splitext(make_output(src, "cover", None, ".jpg"))[0] + ".jpg"
    out = os.path.abspath(os.path.expandvars(os.path.expanduser(out)))
    parent = os.path.dirname(out)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    grab_frame(src, at, out)

    result = [{"type": "text",
               "text": "Cover frame from %.2fs -> %s\nChosen for sharpness and central "
                       "contrast, skipping dissolves and the first/last %.1fs.\n"
                       "Look at it - if it is not the right moment, ask for a specific time."
                       % (at, out, skip)}]
    try:
        result.append(image_content(out, max_w=700))
    except ToolError:
        pass
    return result


def _colour_of(src):
    """Average luma and the two chroma axes. U is blue-vs-yellow, V is red-vs-cyan."""
    st = _signalstats(src, sample_fps=2)
    return (_avg(st.get("YAVG") or []), _avg(st.get("UAVG") or []),
            _avg(st.get("VAVG") or []))


def _match_filter(cur, target, strength=1.0):
    """Filter chain that pulls one shot's colour toward a reference.

    Chroma is centred on 128: a shot below the reference on U is short of blue, below
    on V is short of red. colorbalance moves the midtones without crushing the ends.
    """
    dy, du, dv = (target[0] - cur[0], target[1] - cur[1], target[2] - cur[2])
    # Below this the shots already match, and "correcting" them only introduces error.
    # Measured on clips from one shoot: differences of ~1 are noise, not mismatch.
    if abs(dy) < 2.0 and abs(du) < 1.5 and abs(dv) < 1.5:
        return "", (dy, du, dv)
    parts = []
    if abs(dy) > 1.5:
        parts.append("eq=brightness=%.4f" % max(-0.25, min(0.25, dy / 255.0 * strength)))
    rb = max(-0.5, min(0.5, dv / 60.0 * strength))     # red axis
    bb = max(-0.5, min(0.5, du / 60.0 * strength))     # blue axis
    if abs(rb) > 0.008 or abs(bb) > 0.008:
        parts.append("colorbalance=rm=%.4f:bm=%.4f:rs=%.4f:bs=%.4f"
                     % (rb, bb, rb * 0.6, bb * 0.6))
    return ",".join(parts), (dy, du, dv)


def t_match_colour(a):
    """Balance several clips to a common look so they cut together cleanly."""
    paths = a.get("paths") or []
    if len(paths) < 2:
        raise ToolError("Give at least 2 clips in 'paths'.")
    srcs = [check_input(p, "video") for p in paths]
    stats = [_colour_of(s) for s in srcs]

    ref = a.get("reference")
    if ref is not None:
        idx = int(ref) - 1
        if not 0 <= idx < len(srcs):
            raise ToolError("reference must be between 1 and %d." % len(srcs))
        target = stats[idx]
        how = "clip %d" % (idx + 1)
    else:
        # The median shot is a safer anchor than the mean - one very warm or very dark
        # shot would drag an average and pull every other clip toward its mistake.
        target = tuple(sorted(v[i] for v in stats)[len(stats) // 2] for i in range(3))
        how = "the median of all clips"

    strength = float(a.get("strength", 1.0))
    folder = a.get("folder")
    if folder:
        folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    else:
        folder = os.path.join(os.path.dirname(srcs[0]), "matched")
    os.makedirs(folder, exist_ok=True)

    rows, made = [], []
    for i, src in enumerate(srcs):
        chain, (dy, du, dv) = _match_filter(stats[i], target, strength)
        out = os.path.join(folder, os.path.splitext(os.path.basename(src))[0] + "_m.mp4")
        if chain:
            ffmpeg_run(["-i", src, "-vf", chain] +
                       (["-c:a", "copy"] if has_audio(src) else ["-an"]) +
                       VIDEO_ENC + [out])
        else:
            shutil.copyfile(src, out)
        after = _colour_of(out)
        rows.append("  %-34s Y %+5.1f  U %+5.1f  V %+5.1f  ->  Y %+5.1f  U %+5.1f  V %+5.1f"
                    % (os.path.basename(src)[:34], dy, du, dv,
                       target[0] - after[0], target[1] - after[1], target[2] - after[2]))
        made.append(out)

    return ("Matched %d clip(s) to %s (Y %.1f, U %.1f, V %.1f):\n%s\n\n"
            "Columns are the distance from the reference before and after. Written to %s"
            % (len(srcs), how, target[0], target[1], target[2], "\n".join(rows), folder))


FACE_MODEL = os.path.join(MODEL_DIR, "blaze_face_short_range.tflite")
_FACE_DET = [None]


def _face_detector():
    if _FACE_DET[0] is not None:
        return _FACE_DET[0]
    if not os.path.isfile(FACE_MODEL):
        raise ToolError("The face model is missing. Expected:\n    %s" % FACE_MODEL)
    try:
        import contextlib
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        with contextlib.redirect_stdout(sys.stderr):
            opts = vision.FaceDetectorOptions(
                base_options=mpp.BaseOptions(model_asset_path=FACE_MODEL),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=0.4)
            _FACE_DET[0] = vision.FaceDetector.create_from_options(opts)
    except ImportError:
        raise ToolError("mediapipe is not installed. Run:\n    pip install mediapipe")
    return _FACE_DET[0]


def track_faces(src, sample_fps=4):
    """Where is the subject over time? Returns [(t, x, y, size)] in 0-1 coordinates."""
    import contextlib
    import cv2
    import mediapipe as mp
    det = _face_detector()
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise ToolError("Could not open %s" % os.path.basename(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1
    step = max(1, int(round(fps / float(sample_fps))))
    track, i = [], 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % step == 0:
                img = mp.Image(image_format=mp.ImageFormat.SRGB,
                               data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                with contextlib.redirect_stdout(sys.stderr):
                    res = det.detect(img)
                if res.detections:
                    b = max(res.detections,
                            key=lambda d: d.bounding_box.width).bounding_box
                    track.append((i / fps,
                                  (b.origin_x + b.width / 2.0) / w,
                                  (b.origin_y + b.height / 2.0) / h,
                                  b.width / float(w)))
            i += 1
    finally:
        cap.release()
    return track, (w, h, i / max(fps, 1))


def _smooth_path(points, window=5):
    """Average the track. Raw detections jitter frame to frame, and a crop that
    follows the jitter looks like camera shake rather than a considered move."""
    if len(points) < 3:
        return points
    out = []
    for i in range(len(points)):
        lo, hi = max(0, i - window // 2), min(len(points), i + window // 2 + 1)
        chunk = points[lo:hi]
        out.append(sum(chunk) / float(len(chunk)))
    return out


def _keyframe_expr(times, values, lo, hi):
    """Piecewise-linear ffmpeg expression through the keyframes.

    Each term is flat before its interval, ramps across it, then holds - so summing
    them reproduces straight-line interpolation without a nested if() chain.
    """
    if not times:
        return "%.2f" % ((lo + hi) / 2.0)
    terms = ["%.2f" % max(lo, min(hi, values[0]))]
    for i in range(len(times) - 1):
        dt = times[i + 1] - times[i]
        if dt <= 0.001:
            continue
        dv = max(lo, min(hi, values[i + 1])) - max(lo, min(hi, values[i]))
        if abs(dv) < 0.5:
            continue
        terms.append("%.2f*clip((t-%.3f)/%.3f\\,0\\,1)" % (dv, times[i], dt))
    return "clip(%s\\,%.2f\\,%.2f)" % ("+".join(terms), lo, hi)


def t_reframe(a):
    """Reframe to another shape while keeping the subject in the picture."""
    src = check_input(a.get("path"), "video")
    shape = a.get("aspect") or "1:1"
    if shape not in ASPECTS:
        raise ToolError("aspect must be one of: %s" % ", ".join(ASPECTS))
    tw, th = ASPECTS[shape]
    tw -= tw % 2
    th -= th % 2

    track, (w, h, total) = track_faces(src, int(a.get("sample_fps", 4)))
    if not track:
        raise ToolError("No face was found, so there is nothing to follow. Use "
                        "video_export_pack for a centre crop or blurred surround instead.")

    # Largest crop of the target shape that fits inside the source.
    scale = min(w / float(tw), h / float(th))
    cw, ch = int(tw * scale) & ~1, int(th * scale) & ~1
    cw, ch = min(cw, w), min(ch, h)

    times = [p[0] for p in track]
    xs = _smooth_path([p[1] * w - cw / 2.0 for p in track], int(a.get("smooth", 7)))
    ys = _smooth_path([p[2] * h - ch / 2.0 for p in track], int(a.get("smooth", 7)))
    # Bias upward: heads sit above centre, so a face-centred crop cuts the forehead.
    lift = ch * float(a.get("headroom", 0.08))
    ys = [y - lift for y in ys]

    room_x, room_y = max(0, w - cw), max(0, h - ch)
    ex = _keyframe_expr(times, xs, 0, room_x)
    ey = _keyframe_expr(times, ys, 0, room_y)
    # Report the axis that actually has somewhere to go. Cropping a vertical clip to a
    # square leaves no horizontal room at all, so a horizontal figure would read as
    # "it did nothing" when the whole move is vertical.
    def travel(vals, room):
        if room <= 0:
            return None
        span = max(0.0, min(max(vals), room) - max(min(vals), 0.0))
        return span / float(room)
    tx, ty = travel(xs, room_x), travel(ys, room_y)
    if tx is None and ty is None:
        moved = "the shapes already match, so the crop holds still"
    elif tx is None:
        moved = "the crop travels %.0f%% of its vertical range" % (ty * 100)
    elif ty is None:
        moved = "the crop travels %.0f%% of its horizontal range" % (tx * 100)
    else:
        moved = ("the crop travels %.0f%% horizontally and %.0f%% vertically"
                 % (tx * 100, ty * 100))

    out = make_output(src, "reframe_" + shape.replace(":", "x"), a.get("output"), ".mp4")
    vf = "crop=%d:%d:x='%s':y='%s',scale=%d:%d,setsar=1" % (cw, ch, ex, ey, tw, th)
    ffmpeg_run(["-i", src, "-vf", vf] +
               (["-c:a", "copy"] if has_audio(src) else ["-an"]) +
               VIDEO_ENC + ["-movflags", "+faststart", out])
    return done(out, "Reframed %dx%d -> %s (%dx%d), following the face across %d tracked "
                     "point(s).\n  Crop window %dx%d, %s."
                % (w, h, shape, tw, th, len(track), cw, ch, moved))


def t_export_pack(a):
    """One finished edit out to every platform shape, plus a short hook cut."""
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    wanted = a.get("formats") or ["9:16", "1:1", "16:9"]
    for f in wanted:
        if f not in ASPECTS:
            raise ToolError("format must be one of: %s" % ", ".join(ASPECTS))
    folder = a.get("folder")
    if folder:
        folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    else:
        folder = os.path.join(os.path.dirname(src), "export")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]

    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    sw, sh = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    src_ratio = sw / float(sh)

    made, notes = [], []
    for f in wanted:
        w, h = ASPECTS[f]
        w -= w % 2
        h -= h % 2
        target = w / float(h)
        # Cropping into a much wider shape throws away most of the frame, so pad
        # against a blurred copy instead of butchering the composition.
        if abs(target - src_ratio) < 0.02:
            vf = "scale=%d:%d" % (w, h)
            how = "native"
        elif target < src_ratio * 1.5:
            vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d"
                  % (w, h, w, h))
            how = "cropped to fill"
        else:
            vf = ("split[bg][fg];[bg]scale=%d:%d:force_original_aspect_ratio=increase,"
                  "crop=%d:%d,gblur=sigma=28[b];"
                  "[fg]scale=%d:%d:force_original_aspect_ratio=decrease[f];"
                  "[b][f]overlay=(W-w)/2:(H-h)/2" % (w, h, w, h, w, h))
            how = "blurred surround"
        out = os.path.join(folder, "%s_%s.mp4" % (stem, f.replace(":", "x")))
        ffmpeg_run(["-i", src, "-filter_complex", "[0:v]" + vf + ",setsar=1[v]",
                    "-map", "[v]"] +
                   (["-map", "0:a", "-c:a", "copy"] if has_audio(src) else []) +
                   VIDEO_ENC + ["-movflags", "+faststart", out])
        made.append(out)
        notes.append("  %-6s %4dx%-4d  %-18s %s" % (f, w, h, how, os.path.basename(out)))

    hook = a.get("hook_seconds")
    if hook:
        hook = float(hook)
        if hook >= total:
            raise ToolError("hook_seconds (%.1f) must be shorter than the video (%.1f)."
                            % (hook, total))
        # Pick the liveliest window rather than assuming the opening is the best bit.
        st = _signalstats(src, sample_fps=2)
        motion = st.get("YDIF") or []
        best_at, best_score = 0.0, -1.0
        if motion:
            step = 0.5
            span = max(1, int(hook / step))
            for i in range(0, max(1, len(motion) - span)):
                score = sum(motion[i:i + span]) / float(span)
                if score > best_score:
                    best_score, best_at = score, i * step
        best_at = max(0.0, min(best_at, total - hook))
        hook_out = os.path.join(folder, "%s_hook%ds.mp4" % (stem, int(hook)))
        t_trim({"path": src, "start": best_at, "duration": hook, "output": hook_out})
        made.append(hook_out)
        notes.append("  hook   %.1fs from %.2fs (busiest stretch)  %s"
                     % (hook, best_at, os.path.basename(hook_out)))

    sizes = sum(os.path.getsize(m) for m in made) / 1e6
    return ("Exported %d file(s) to %s\n%s\n\nTotal %.1f MB. The original is untouched."
            % (len(made), folder, "\n".join(notes), sizes))


def t_audio_scope(a):
    """Show the mix as a picture, and say where music or effects bury the voice.

    Levels alone cannot answer 'does the whoosh sit right' - masking is about which
    band is loudest at a given moment. This renders a waveform and a spectrogram to
    look at, and measures the speech band against everything competing with it.
    """
    import numpy as np
    from PIL import Image
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This file has no audio to inspect.")
    total = duration_of(src)
    tmp = _tmpdir()
    W = 1200

    wave = os.path.join(tmp, "wave_%d.png" % os.getpid())
    spec = os.path.join(tmp, "spec_%d.png" % os.getpid())
    ffmpeg_run(["-i", src, "-filter_complex",
                "[0:a]showwavespic=s=%dx240:colors=0x5aa9e6" % W, "-frames:v", "1", wave])
    ffmpeg_run(["-i", src, "-lavfi",
                "[0:a]showspectrumpic=s=%dx420:legend=1:scale=log:color=intensity" % W,
                "-frames:v", "1", spec])

    # Decode once and compare the speech band against what competes with it.
    sr = 22050
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", src,
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    y = np.frombuffer(p.stdout, dtype="<i2").astype("float32") / 32768.0
    win = int(sr * 0.25)
    rows = []
    for i in range(0, max(1, y.size - win), win):
        seg = y[i:i + win]
        if seg.size < win // 2:
            break
        sp = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        f = np.fft.rfftfreq(seg.size, 1.0 / sr)
        speech = float(sp[(f >= 300) & (f < 3400)].sum())
        low = float(sp[(f >= 40) & (f < 300)].sum())        # music bass, booms
        high = float(sp[(f >= 3400) & (f < 9000)].sum())    # whooshes, air
        rows.append((i / float(sr), speech, low, high))

    speech_vals = sorted(r[1] for r in rows)
    active = _pct(speech_vals, 0.75)          # a "loud speech" reference level
    masked = [r for r in rows if r[1] > active * 0.45 and (r[2] + r[3]) > r[1] * 1.15]
    quiet = [r for r in rows if r[1] < active * 0.08]

    # Stack the two plots into one image with a label strip.
    wi, si = Image.open(wave).convert("RGB"), Image.open(spec).convert("RGB")
    sheet = Image.new("RGB", (W, wi.height + si.height + 54), (17, 24, 31))
    sheet.paste(_label(wi, "waveform - %.1fs" % total), (0, 26))
    sheet.paste(_label(si, "spectrogram - bright = loud"), (0, wi.height + 40))
    out_img = os.path.join(tmp, "scope_%d.jpg" % os.getpid())
    sheet.save(out_img, quality=88)

    L = ["Mix inspection: %s (%.1fs)" % (os.path.basename(src), total), ""]
    loud = _loudness(src)
    peak = _peak_db(src)
    L.append("Loudness %.1f LUFS, peak %.1f dB" % (loud if loud is not None else 0,
                                                   peak if peak is not None else 0))
    L.append("")
    if masked:
        L.append("Voice is being covered at %d moment(s) - music or effects are louder than "
                 "the speech band there:" % len(masked))
        for t, s, lo, hi in masked[:10]:
            src_of = "low end (music/boom)" if lo > hi else "top end (whoosh/air)"
            L.append("    %5.2fs  %s is %.0f%% above the voice" % (t, src_of,
                                                                   (lo + hi) / max(s, 1e-9) * 100 - 100))
        if len(masked) > 10:
            L.append("    ... and %d more" % (len(masked) - 10))
        L.append("  Lower music_level or sfx gain, or duck harder at these points.")
    else:
        L.append("Nothing is burying the voice - the speech band stays on top throughout.")
    L.append("")
    L.append("%d of %d quarter-second windows are near-silent%s"
             % (len(quiet), len(rows),
                " - a bed would fill them" if len(quiet) > len(rows) * 0.15 else ""))
    L.append("")
    L.append("Look at the spectrogram: speech is the banded texture around 300-3400 Hz, a "
             "whoosh is a bright vertical smear, music is the steady low bands.")
    return [{"type": "text", "text": "\n".join(L)}, image_content(out_img, max_w=1200, quality=88)]


# ---------------------------------------------------------------- machine listening
YAMNET = os.path.join(MODEL_DIR, "yamnet.tflite")
_AUDIO_CLF = [None]


def _classifier():
    """YAMNet, loaded once. Absent model just means this feature is unavailable."""
    if _AUDIO_CLF[0] is not None:
        return _AUDIO_CLF[0]
    if not os.path.isfile(YAMNET):
        raise ToolError("The audio classifier model is missing. Expected:\n    %s\n"
                        "Download yamnet.tflite from Google's MediaPipe model host." % YAMNET)
    try:
        import contextlib
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import audio as mp_audio
        with contextlib.redirect_stdout(sys.stderr):
            opts = mp_audio.AudioClassifierOptions(
                base_options=mpp.BaseOptions(model_asset_path=YAMNET),
                running_mode=mp_audio.RunningMode.AUDIO_CLIPS,
                max_results=5)
            _AUDIO_CLF[0] = mp_audio.AudioClassifier.create_from_options(opts)
    except ImportError:
        raise ToolError("mediapipe is not installed. Run:\n    pip install mediapipe")
    return _AUDIO_CLF[0]


def listen_to(path, seconds=12.0):
    """What does this file actually sound like? Returns [(label, confidence), ...]."""
    import contextlib
    import numpy as np
    from mediapipe.tasks.python.components import containers
    clf = _classifier()
    _require_ffmpeg()
    sr = 16000                       # YAMNet's expected rate
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                        "-t", "%.2f" % seconds, "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    y = np.frombuffer(p.stdout, dtype="<f4").copy()
    if y.size < sr // 2:
        if y.size == 0:
            raise ToolError("No audio in %s" % os.path.basename(path))
        y = np.pad(y, (0, sr // 2 - y.size))
    clip = containers.AudioData.create_from_array(y, sr)
    with contextlib.redirect_stdout(sys.stderr):
        res = clf.classify(clip)
    best = {}
    for r in res:
        for c in r.classifications[0].categories[:4]:
            best[c.category_name] = max(best.get(c.category_name, 0.0), float(c.score))
    return sorted(best.items(), key=lambda kv: -kv[1])[:5]


def t_video_shape(a):
    """The whole film as one picture: pacing, brightness, motion, sound, cuts.

    Written because of a real failure too. I judge a film from a grid of stills,
    which shows me every moment and none of the shape - so a montage that flickered
    light-dark-light across twenty-one photos looked perfectly fine in stills and
    was only caught later by measuring. Stills cannot show change over time, and
    change over time is what pacing IS.

    This turns the time axis into something readable: four lanes on one image, all
    on the same clock. A brightness lane that zig-zags is a montage that flickers.
    A motion lane that flatlines is a dead stretch. A sound lane that does not dip
    where the picture has a voice is music that has not been ducked. None of that
    is visible in a contact sheet.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    if total < 1.0:
        raise ToolError("Too short to have a shape.")
    rate = 4.0                      # samples per second - enough to see a 0.45s dissolve
    tw, th = 64, 36

    p = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", src,
                        "-vf", "fps=%g,scale=%d:%d,format=gray" % (rate, tw, th),
                        "-f", "rawvideo", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=FFMPEG_TIMEOUT,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    nf = buf.size // (tw * th)
    if nf < 4:
        raise ToolError("Could not read frames from %s" % os.path.basename(src))
    fr = buf[:nf * tw * th].reshape(nf, th, tw).astype(np.float32)

    bright = fr.reshape(nf, -1).mean(axis=1)
    motion = np.concatenate([[0.0], np.abs(np.diff(fr.reshape(nf, -1), axis=0)).mean(axis=1)])

    snd = np.zeros(nf)
    if has_audio(src):
        sr = 8000
        q = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", src,
                            "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=FFMPEG_TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        y = np.frombuffer(q.stdout, dtype="<f4").astype(np.float64)
        step = max(1, int(sr / rate))
        r = np.array([float(np.sqrt(np.mean(y[i:i + step] ** 2)) + 1e-9)
                      for i in range(0, max(step, len(y) - step), step)])
        r = 20 * np.log10(r / max(r.max(), 1e-9))
        r = np.clip((r + 45) / 45.0, 0, 1)
        snd = np.interp(np.linspace(0, len(r) - 1, nf), np.arange(len(r)), r)

    # A cut is a jump in the picture far above what this film normally does, so the
    # threshold comes from the film itself - a fixed one finds twelve cuts in a
    # seven-shot piece, or none at all in a gentle one.
    thr = float(np.median(motion) + 4.5 * (np.percentile(motion, 75) -
                                           np.percentile(motion, 25)) + 2.0)
    cuts = [i for i in range(1, nf) if motion[i] > thr]
    cuts = [c for i, c in enumerate(cuts) if i == 0 or c - cuts[i - 1] > rate * 0.4]

    # ------------------------------------------------------------------ drawing
    W, LANE, PAD, THUMB = 1180, 74, 34, 96
    H = PAD + THUMB + LANE * 3 + 46
    im = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(im)
    x_of = lambda i: PAD + int(i / max(1, nf - 1) * (W - PAD * 2))

    n_th = max(4, min(11, int((W - PAD * 2) / (THUMB * 1.5))))
    tw2 = int((W - PAD * 2) / n_th) - 4
    for k in range(n_th):
        at = total * (k + 0.5) / n_th
        f = os.path.join(_tmpdir(), "shape_%d_%d.png" % (os.getpid(), k))
        subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                        "-ss", "%.3f" % at, "-i", src, "-frames:v", "1",
                        "-vf", "scale=%d:-1" % tw2, f],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.path.isfile(f):
            with Image.open(f) as t:
                t = t.convert("RGB")
                t.thumbnail((tw2, THUMB))
                im.paste(t, (PAD + k * (tw2 + 4), PAD))

    def lane(idx, values, colour, title, lo=None, hi=None):
        top = PAD + THUMB + 8 + idx * LANE
        base = top + LANE - 20
        d.rectangle([PAD, top, W - PAD, base], fill=(22, 26, 34))
        v = np.asarray(values, dtype=np.float64)
        lo = float(v.min()) if lo is None else lo
        hi = float(v.max()) if hi is None else hi
        rng = max(1e-6, hi - lo)
        pts = [(x_of(i), base - int((v[i] - lo) / rng * (LANE - 26)))
               for i in range(len(v))]
        d.line(pts, fill=colour, width=2)
        d.text((PAD + 3, top + 2), title, fill=(150, 160, 172))
        d.text((W - PAD - 74, top + 2), "%.0f-%.0f" % (lo, hi), fill=(105, 114, 126))
        return top, base

    # Drawn against its OWN range, not 0-255. On an absolute scale a 78-point
    # swing across twenty-one photos is a barely-visible wobble, which is exactly
    # the flicker this lane exists to show. The printed range says how big it is.
    lane(0, bright, (255, 214, 120), "BRIGHTNESS   zig-zag = the montage flickers")
    lane(1, motion, (120, 200, 255), "MOTION   flat = a dead stretch, spikes = cuts")
    lane(2, snd, (120, 230, 150), "SOUND   should dip where there is a voice", 0, 1)

    for c in cuts:
        d.line([(x_of(c), PAD + THUMB + 8), (x_of(c), PAD + THUMB + 8 + LANE * 3 - 20)],
               fill=(90, 70, 70), width=1)
    for s in range(0, int(total) + 1, max(1, int(total / 12) or 1)):
        x = x_of(int(s * rate))
        d.line([(x, H - 30), (x, H - 24)], fill=(90, 98, 110))
        d.text((x - 8, H - 20), "%ds" % s, fill=(120, 130, 142))

    out = make_output(src, "shape", a.get("output"), ".png")
    im.save(out)

    # ------------------------------------------------------------------ reading
    flick = float(np.abs(np.diff(bright)).mean())
    jumps = [abs(float(bright[c] - bright[c - 1])) for c in cuts] or [0.0]
    dead = []
    run_len = 0
    for i, m in enumerate(motion):
        if m < np.percentile(motion, 20):
            run_len += 1
        else:
            if run_len >= rate * 3:
                dead.append((round((i - run_len) / rate, 1), round(run_len / rate, 1)))
            run_len = 0

    notes = ["%s  %.1fs, %d cut(s) found" % (os.path.basename(src), total, len(cuts))]
    notes.append("  Brightness moves %.1f of 255 between samples; the biggest jump "
                 "across a cut is %.0f." % (flick, max(jumps)))
    if max(jumps) > 28:
        notes.append("  That is enough to SEE as a flash. Even the exposure across "
                     "the shots.")
    if dead:
        notes.append("  Nothing much moves for %s." %
                     ", ".join("%.1fs at %ds" % (l, t) for t, l in dead[:3]))
    if has_audio(src):
        quiet = float((snd < 0.25).mean() * 100)
        notes.append("  Sound sits under a quarter level for %.0f%% of it." % quiet)
    notes.append("\n  Look at the image: four lanes on one clock. This is the part a "
                 "grid of stills cannot show.")
    return [{"type": "text", "text": "\n".join(notes) + "\n  -> " + out},
            image_content(out)]


def t_font_library(a):
    """Every face in the library, showing YOUR words, so the choice is made by eye.

    A list of font names is useless - nobody can picture what "Taviraj" looks like
    set in Thai at caption size over a photograph. This renders the actual text,
    at the actual size, and where a frame is given, over the actual footage.

    It also refuses to pretend. Nine of these faces have no Thai glyphs at all,
    which is the one mistake that really spoils a caption: libass falls back per
    GLYPH, so Thai words and Latin digits come out in two different typefaces and
    it reads as a fault rather than a choice.
    """
    from PIL import Image, ImageDraw, ImageFont

    text = a.get("text") or u"\u0e2a\u0e38\u0e02\u0e2a\u0e31\u0e19\u0e15\u0e4c\u0e27\u0e31\u0e19\u0e40\u0e01\u0e34\u0e14"
    kind = (a.get("kind") or "all").lower()
    if kind not in ("all", "caption", "title", "hand"):
        raise ToolError("kind must be all, caption, title or hand.")
    want_thai = a.get("thai")
    if want_thai is None:
        want_thai = any(u"\u0e01" <= c <= u"\u0e5b" for c in text)

    names = []
    for fam, meta in SUBTITLE_FONTS.items():
        if kind != "all" and (meta.get("kind") or "caption") != kind:
            continue
        if want_thai and meta.get("thai") is False:
            continue
        names.append(fam)
    if not names:
        raise ToolError("No font matches that filter.")
    names.sort(key=lambda n: ((SUBTITLE_FONTS[n].get("kind") or "caption"), n))

    tmp = _tmpdir()
    bg = a.get("over")
    W, TILE_H, PAD = 620, 190, 30
    if bg:
        bg = check_input(bg, "image or video")
        frame = os.path.join(tmp, "fl_bg_%d.png" % os.getpid())
        at = float(a.get("at", 1.0))
        if os.path.splitext(bg)[1].lower() in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                            "-ss", "%.2f" % at, "-i", bg, "-frames:v", "1", frame],
                           capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            frame = bg
        base = Image.open(frame).convert("RGB")
        base = base.crop((0, int(base.height * 0.55), base.width,
                          min(base.height, int(base.height * 0.55) + int(base.width * 0.30))))
        base = base.resize((W, TILE_H - 34), Image.LANCZOS)
    else:
        base = None

    label = ImageFont.truetype("C:/Windows/Fonts/tahomabd.ttf", 21)
    small = ImageFont.truetype("C:/Windows/Fonts/tahoma.ttf", 17)
    tiles = []
    for fam in names:
        meta = SUBTITLE_FONTS[fam]
        path = _font_file(fam)
        tile = Image.new("RGB", (W, TILE_H), (13, 16, 22))
        if base is not None:
            tile.paste(base.copy(), (0, 34))
        d = ImageDraw.Draw(tile)
        d.text((10, 5), fam, font=label, fill=(255, 214, 120))
        d.text((10 + int(d.textlength(fam, font=label)) + 14, 8),
               "%s  ·  %s" % (meta.get("kind", "caption"), meta.get("note", ""))[:66],
               font=small, fill=(132, 143, 156))
        if path:
            px = int(58 * float(meta.get("size", 1.0)))
            try:
                fnt = ImageFont.truetype(path, px)
                bb = d.textbbox((0, 0), text, font=fnt)
                x = (W - (bb[2] - bb[0])) // 2
                y = 34 + (TILE_H - 34 - (bb[3] - bb[1])) // 2 - bb[1]
                for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2)):
                    d.text((x + dx, y + dy), text, font=fnt, fill=(20, 20, 24))
                d.text((x, y), text, font=fnt, fill=(255, 255, 255))
            except Exception:
                d.text((14, 80), "could not render", font=label, fill=(248, 81, 73))
        else:
            d.text((14, 80), "installed on this PC - not bundled",
                   font=small, fill=(132, 143, 156))
        tiles.append(tile)

    cols = 2 if len(tiles) > 6 else 1
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W + 8) - 8, rows * (TILE_H + 8) - 8), (8, 10, 15))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * (W + 8), (i // cols) * (TILE_H + 8)))
    out = make_output(names[0] + ".png", "fonts", a.get("output"), ".png")
    sheet.save(out)

    hidden = [n for n, m in SUBTITLE_FONTS.items() if want_thai and m.get("thai") is False]
    note = ["%d font(s) shown, set in your own words." % len(names)]
    if hidden:
        note.append("  %d Latin-only face(s) left out because the text is Thai: %s. "
                    "Naming one of those for a Thai caption makes libass fall back "
                    "glyph by glyph, so the words and the numbers come out in "
                    "different typefaces." % (len(hidden), ", ".join(sorted(hidden))))
    note.append("  Filter with kind: caption / title / hand, and pass `over` to set "
                "them on a real frame from your own footage.")
    return [{"type": "text", "text": "\n".join(note) + "\n  -> " + out},
            image_content(out, max_w=1500)]


def t_music_describe(a):
    """What a piece of music actually IS - so a choice can be judged, not guessed.

    Written because of a real failure. Scoring a family birthday film, I picked the
    music and then had to admit I could not say whether it suited: audio never
    reaches me, so "does this fit" was a guess dressed up as a decision. I cannot
    hear, and no tool changes that. What CAN be done is turn the qualities a person
    hears into ones that can be read.

    Four things decide whether a track fits a film, and all four are measurable:
      WHAT IT IS      piano, strings, drums, singing - YAMNet already knows
      HOW FAST        tempo, and whether it is steady
      ITS SHAPE       does it build toward something, or sit at one level? A flat
                      track under a story that has a turn is what makes an edit
                      feel like a slideshow.
      ITS COLOUR      bright and sparkling, or warm and dark. Spectral centroid.

    None of that is taste. But it is enough to stop a bright 160bpm corporate track
    going under a grandmother's birthday by accident.
    """
    import numpy as np
    src = check_input(a.get("path"), "audio")
    total = duration_of(src)
    win = max(4.0, min(12.0, total / 6.0))

    sr = 22050
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", src,
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    y = np.frombuffer(p.stdout, dtype="<f4").astype(np.float64)
    if y.size < sr:
        raise ToolError("Too short to describe: %s" % os.path.basename(src))

    # --- the shape: loudness second by second, then what that curve DOES --------
    step = sr // 2
    frames = [y[i:i + step] for i in range(0, len(y) - step, step)]
    rms = np.array([float(np.sqrt(np.mean(f ** 2))) + 1e-9 for f in frames])
    db = 20 * np.log10(rms / max(rms.max(), 1e-9))
    third = max(1, len(db) // 3)
    start_l, mid_l, end_l = (float(db[:third].mean()), float(db[third:2 * third].mean()),
                             float(db[2 * third:].mean()))
    swing = float(db.max() - np.percentile(db, 5))

    if end_l - start_l > 3.0:
        shape = "builds - it is louder at the end than the start"
    elif start_l - end_l > 3.0:
        shape = "fades away toward the end"
    elif mid_l - (start_l + end_l) / 2 > 3.0:
        shape = "peaks in the middle then comes back down"
    elif swing < 6.0:
        shape = ("FLAT - it sits at one level throughout. Under a story with a turn "
                 "in it, that is what makes an edit feel like a slideshow")
    else:
        shape = "varies without a clear arc"

    # --- colour: where the energy sits in the spectrum -------------------------
    seg = y[:sr * 30] if len(y) > sr * 30 else y
    n = 1 << 12
    cents = []
    for i in range(0, max(1, len(seg) - n), n):
        mag = np.abs(np.fft.rfft(seg[i:i + n] * np.hanning(n)))
        f = np.fft.rfftfreq(n, 1.0 / sr)
        if mag.sum() > 1e-6:
            cents.append(float((f * mag).sum() / mag.sum()))
    centroid = float(np.median(cents)) if cents else 0.0
    colour = ("dark and warm" if centroid < 900 else
              "warm" if centroid < 1600 else
              "bright" if centroid < 2800 else "very bright, sparkling")

    # --- what it is, sampled across the track rather than only at the start ----
    heard = {}
    for at in (total * 0.15, total * 0.5, total * 0.8):
        clip = os.path.join(_tmpdir(), "md_%d_%d.wav" % (os.getpid(), int(at * 10)))
        subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                        "-ss", "%.2f" % max(0, at - win / 2), "-i", src,
                        "-t", "%.2f" % win, "-ac", "1", "-ar", "16000", clip],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            for lbl, sc in listen_to(clip, win):
                heard[lbl] = max(heard.get(lbl, 0.0), sc)
        except ToolError:
            pass
    names = [k for k, v in sorted(heard.items(), key=lambda kv: -kv[1])[:6] if v >= 0.10]

    try:
        env, rate = _onset_envelope(src)
        bpm, _beats = _beat_grid(env, rate)
    except Exception:
        bpm = None

    lines = ["%s  (%.1fs)" % (os.path.basename(src), total)]
    lines.append("  Sounds like : %s" % (", ".join(names) if names
                                         else "nothing the classifier is sure of"))
    lines.append("  Tempo       : %s" % ("%.0f BPM" % bpm if bpm else "no steady pulse"))
    lines.append("  Colour      : %s (centre of energy %.0f Hz)" % (colour, centroid))
    lines.append("  Shape       : %s" % shape)
    lines.append("  Swing       : %.1f dB between its quietest and loudest" % swing)

    fit = []
    if bpm and bpm >= 140:
        fit.append("At %.0f BPM this is fast - fine for something energetic, "
                   "restless under anything tender." % bpm)
    if bpm and bpm <= 80:
        fit.append("At %.0f BPM this is slow - good for a reflective piece, "
                   "sleepy under something upbeat." % bpm)
    if centroid >= 2800:
        fit.append("Very bright, so it will sit on top of the picture and be noticed "
                   "rather than sitting under it.")
    if swing < 6.0:
        fit.append("With no dynamic arc it cannot follow a story - if the film has a "
                   "turn, plan to change track there or duck it yourself.")
    if any(k.lower() in ("speech", "singing", "conversation", "narration, monologue")
           for k in names):
        fit.append("There are VOICES in this. Under dialogue that will fight; "
                   "check before using it as a bed.")
    lines.append("\n  " + (" ".join(fit) if fit else
                           "Nothing here argues against using it as a bed."))
    lines.append("\n  This is measured, not heard. It says what the music IS; whether "
                 "it suits your film is still yours to decide.")
    return "\n".join(lines)


def t_sound_identify(a):
    """Label every sound in a folder by what it actually is, not what it is called."""
    folder = a.get("folder")
    one = a.get("path")
    if not folder and not one:
        raise ToolError("Give a 'folder' of sounds, or a single 'path'.")
    if one:
        files = [check_input(one, "audio")]
    else:
        folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
        if not os.path.isdir(folder):
            raise ToolError("Folder not found: %s" % folder)
        files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                 if f.lower().endswith(AUDIO_EXTS + (".mp4", ".mov", ".mkv", ".webm"))]
    if not files:
        raise ToolError("No audio or video files found there.")

    rows, unsure = [], 0
    for f in files[:80]:
        try:
            top = listen_to(f)
        except ToolError as e:
            rows.append("  %-38s could not read (%s)" % (os.path.basename(f)[:38], e))
            continue
        # Below about 40% the model is reaching. Say so rather than presenting a guess.
        labels = ", ".join("%s %.0f%%" % (n, s * 100) for n, s in top[:3])
        weak = "   (uncertain)" if (not top or top[0][1] < 0.40) else ""
        if weak:
            unsure += 1
        rows.append("  %-38s %s%s" % (os.path.basename(f)[:38], labels, weak))

    note = ""
    if unsure:
        note = ("\n\n%d file(s) came back under 40%% confidence - the model is guessing "
                "there. Very short clips and stylised effects often have no matching "
                "category." % unsure)
    return ("Listened to %d file(s) in %s\n\n%s%s"
            % (len(rows), folder or os.path.dirname(files[0]), "\n".join(rows), note))


def find_sound(folder, description, threshold=0.25):
    """Rank sounds in a folder by how well they match a description."""
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        raise ToolError("Folder not found: %s" % folder)
    want = [w for w in re.split(r"[\s,]+", description.lower()) if len(w) > 2]
    scored = []
    for f in sorted(os.listdir(folder)):
        if not f.lower().endswith(AUDIO_EXTS):
            continue
        path = os.path.join(folder, f)
        try:
            top = listen_to(path)
        except ToolError:
            continue
        best = 0.0
        for label, conf in top:
            low = label.lower()
            if any(w in low or low in w for w in want):
                best = max(best, conf)
        # Fall back to the filename when the model has no matching category - a
        # stylised effect may be named correctly even if YAMNet cannot place it.
        if best == 0 and any(w in f.lower() for w in want):
            best = 0.2
        if best >= threshold or best == 0.2:
            scored.append((best, path, top))
    scored.sort(key=lambda x: -x[0])
    return scored


def t_find_sound(a):
    """Search a sound folder by what things sound like rather than by filename."""
    folder = a.get("folder")
    desc = (a.get("description") or "").strip()
    if not folder or not desc:
        raise ToolError("Give a 'folder' and a 'description', e.g. 'whoosh' or 'applause'.")
    hits = find_sound(folder, desc, float(a.get("threshold", 0.25)))
    if not hits:
        raise ToolError("Nothing in that folder sounds like '%s'. Try sound_identify to "
                        "see what is actually there." % desc)
    rows = []
    for score, path, top in hits[:12]:
        how = "filename only" if score == 0.2 else "%.0f%% match" % (score * 100)
        rows.append("  %-38s %-16s %s" % (os.path.basename(path)[:38], how,
                                          ", ".join(n for n, _s in top[:2])))
    return ("Sounds matching '%s':\n%s\n\nBest: %s"
            % (desc, "\n".join(rows), hits[0][1]))


def t_sfx_demo(a):
    """Build an audition reel: every sound played in turn, named on screen."""
    out = a.get("output")
    if not out:
        out = os.path.join(os.getcwd(), "sfx_demo.mp4")
    out = os.path.abspath(os.path.expandvars(os.path.expanduser(out)))
    parent = os.path.dirname(out)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    names = a.get("sounds") or sorted(SFX_LIBRARY)
    for n in names:
        if n not in SFX_LIBRARY:
            raise ToolError("Unknown sound '%s'." % n)
    gap = float(a.get("gap", 0.45))
    w, h = 720, 720
    tmp = _tmpdir()
    font = r"C:\Windows\Fonts\tahomabd.ttf"
    fontfile = ":fontfile='%s'" % escape_filter_path(font) if os.path.isfile(font) else ""

    clips = []
    for i, n in enumerate(names):
        wav = os.path.join(tmp, "demo_%d_%s.wav" % (os.getpid(), n))
        dur = render_sfx(n, wav) + gap
        seg = os.path.join(tmp, "demo_%d_%s.mp4" % (os.getpid(), n))
        label = "%d/%d   %s" % (i + 1, len(names), n.replace("_", " "))
        vf = ("drawtext=text='%s':fontsize=54:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2%s,"
              "drawtext=text='%s':fontsize=26:fontcolor=0x8fb8d8:x=(w-text_w)/2:y=(h-text_h)/2+70%s"
              % (label, fontfile, "%.2fs" % SFX_LIBRARY[n][1], fontfile))
        ffmpeg_run(["-f", "lavfi", "-i", "color=c=0x11181f:s=%dx%d:r=25:d=%.2f" % (w, h, dur),
                    "-i", wav, "-vf", vf, "-map", "0:v", "-map", "1:a",
                    "-t", "%.2f" % dur, "-af", "apad", "-shortest"]
                   + VIDEO_ENC + AUDIO_ENC + ["-ar", "48000", seg])
        clips.append(seg)

    listing = os.path.join(tmp, "demo_list_%d.txt" % os.getpid())
    with io.open(listing, "w", encoding="utf-8") as fh:
        for c in clips:
            fh.write("file '%s'\n" % c.replace("\\", "/").replace("'", "'\\''"))
    try:
        ffmpeg_run(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", out])
    finally:
        for c in clips + [listing]:
            try:
                os.remove(c)
            except OSError:
                pass
    return done(out, "Audition reel: %d sound(s), each named on screen. Play it to pick the "
                     "ones you want." % len(names))


def t_add_sfx(a):
    """Lay sound effects onto a video at given times, or on every transition."""
    src = check_input(a.get("path"), "video")
    total = duration_of(src)
    events = []

    library = a.get("sound_folder")
    if library:
        library = os.path.abspath(os.path.expandvars(os.path.expanduser(library)))
        if not os.path.isdir(library):
            raise ToolError("sound_folder not found: %s" % library)

    def resolve(nm):
        """A name may be a built-in, a file in the user's folder, or a description of one."""
        if nm in SFX_LIBRARY or nm in SFX_COMBOS:
            return nm
        if library:
            direct = os.path.join(library, nm)
            if os.path.isfile(direct):
                return direct
            hits = find_sound(library, nm)
            if hits:
                return hits[0][1]
        if os.path.isfile(nm):
            return nm
        return None

    def expand(nm, at, gain):
        """A combo is several sounds around one hit point, the way an editor stacks them."""
        if nm in SFX_COMBOS:
            return [(s, max(0.0, at + off), gain * g) for s, off, g in SFX_COMBOS[nm]]
        return [(nm, at, gain)]

    for spec in (a.get("sounds") or []):
        if isinstance(spec, dict):
            nm, at = spec.get("sound"), parse_time(spec.get("at"), "at")
            gain = float(spec.get("gain", 1.0))
        else:
            raise ToolError("'sounds' takes objects like {\"sound\":\"whoosh\",\"at\":\"0:03\"}.")
        if not nm or at is None:
            raise ToolError("Each sound needs both 'sound' and 'at'.")
        found = resolve(nm)
        if not found:
            raise ToolError("Unknown sound '%s'.\nBuilt in: %s\nCombos: %s%s"
                            % (nm, ", ".join(sorted(SFX_LIBRARY)), ", ".join(sorted(SFX_COMBOS)),
                               "\nOr pass 'sound_folder' to use your own files."
                               if not library else "\nNothing in %s sounds like that either."
                               % library))
        events += expand(found, at, gain)

    auto = a.get("on_transitions")
    if auto:
        nm = auto if isinstance(auto, str) else "whoosh"
        lead = float(a.get("lead", 0.25))     # start just before the cut
        sens = a.get("shot_sensitivity")
        # A dissolve changes the picture gradually, so the default hard-cut threshold
        # misses it entirely. Step the sensitivity down until transitions show up.
        ladder = [float(sens)] if sens else [8.0, 4.0, 2.5, 1.6]
        found = []
        for th in ladder:
            cuts = [c for c in _scene_cuts(src, th) if 0.6 < c < total - 0.6]
            if cuts:
                found = cuts
                break
        for c in found:
            if nm in SFX_COMBOS:
                # Combos carry their own offsets; the cut itself is the hit point.
                events += expand(nm, c, float(a.get("gain", 0.9)))
            else:
                events.append((nm, max(0.0, c - lead), float(a.get("gain", 0.9))))
        if not found:
            raise ToolError("No shot changes were found, so there is nothing to sit a sound on. "
                            "Place them yourself with 'sounds', or lower 'shot_sensitivity'.")
    if not events:
        raise ToolError("Nothing to add. Give 'sounds', or set on_transitions.")

    tmp = _tmpdir()
    files, inputs = [], []
    for i, (nm, at, gain) in enumerate(events):
        p = os.path.join(tmp, "sfx_%d_%d.wav" % (os.getpid(), i))
        render_sfx(nm, p, gain)
        files.append((p, at))
        inputs += ["-i", p]

    parts, labels = [], []
    for i, (_p, at) in enumerate(files, start=1):
        parts.append("[%d:a]adelay=%d|%d,volume=%.3f[s%d]"
                     % (i, int(at * 1000), int(at * 1000), float(a.get("mix", 0.95)), i))
        labels.append("[s%d]" % i)

    keep = has_audio(src)
    if keep:
        # Duck the original slightly under each effect rather than fighting it.
        parts.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo[base]")
        parts.append("%s%samix=inputs=%d:duration=first:normalize=0[aout]"
                     % ("[base]", "".join(labels), len(labels) + 1))
    else:
        parts.append("%samix=inputs=%d:duration=longest:normalize=0[aout]"
                     % ("".join(labels), len(labels)))

    out = make_output(src, "sfx", a.get("output"), ".mp4")
    script = os.path.join(os.path.dirname(out), ".sfx_%d.txt" % os.getpid())
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(parts))
    try:
        ffmpeg_run(["-i", src] + inputs + ["-filter_complex_script", script,
                    "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-shortest"]
                   + AUDIO_ENC + ["-ar", "48000", out])
    finally:
        for p, _ in files:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.remove(script)
        except OSError:
            pass
    listing = ", ".join("%s@%.2fs" % (n, t) for n, t, _g in events[:8])
    return done(out, "Placed %d sound(s): %s%s"
                % (len(events), listing, " ..." if len(events) > 8 else ""))


# ---------------------------------------------------------------- music beds
def _midi_hz(m):
    return 440.0 * (2.0 ** ((m - 69) / 12.0))


# Progressions as MIDI roots + chord shape. Kept deliberately simple: these sit under
# dialogue, so they need to be unobtrusive rather than interesting.
MUSIC_MOODS = {
    "calm":      dict(chords=[(57, "min"), (53, "maj"), (48, "maj"), (55, "maj")],
                      bpm=72, bright=1800, wave="soft"),
    "uplifting": dict(chords=[(48, "maj"), (55, "maj"), (57, "min"), (53, "maj")],
                      bpm=100, bright=2600, wave="bright"),
    "warm":      dict(chords=[(53, "maj"), (48, "maj"), (55, "maj"), (57, "min")],
                      bpm=84, bright=1500, wave="soft"),
    "tense":     dict(chords=[(57, "min"), (56, "min"), (53, "maj"), (52, "maj")],
                      bpm=96, bright=2200, wave="bright"),
    "gentle":    dict(chords=[(48, "maj"), (53, "maj"), (48, "maj"), (55, "maj")],
                      bpm=64, bright=1200, wave="soft"),
}
CHORD_SHAPES = {"maj": [0, 4, 7, 12], "min": [0, 3, 7, 12]}


def t_music_generate(a):
    """Synthesise a simple music bed - chord pad, sub bass, optional soft pulse."""
    mood = a.get("mood") or "calm"
    if mood not in MUSIC_MOODS:
        raise ToolError("mood must be one of: %s" % ", ".join(MUSIC_MOODS))
    spec = MUSIC_MOODS[mood]
    total = float(a.get("duration", 20.0))
    if not 2 <= total <= 600:
        raise ToolError("duration must be between 2 and 600 seconds.")
    bpm = float(a.get("bpm") or spec["bpm"])
    bar = 4 * 60.0 / bpm                       # one chord per bar
    tmp = _tmpdir()

    chords = spec["chords"]
    n_bars = int(math.ceil(total / bar))
    pieces = []
    for i in range(n_bars):
        root, shape = chords[i % len(chords)]
        notes = [_midi_hz(root + s) for s in CHORD_SHAPES[shape]]
        # Slow attack and release so chords breathe into each other.
        env = "min(t/0.5\\,1)*min((%.3f-t)/0.6\\,1)" % bar
        if spec["wave"] == "bright":
            voices = "+".join("(sin(2*PI*%.3f*t)+0.25*sin(4*PI*%.3f*t))" % (f, f) for f in notes)
            amp = 0.10
        else:
            voices = "+".join("sin(2*PI*%.3f*t)" % f for f in notes)
            amp = 0.13
        bass = "0.35*sin(2*PI*%.3f*t)" % _midi_hz(root - 12)
        expr = "%.3f*((%s)+%s)*%s" % (amp, voices, bass, env)
        p = os.path.join(tmp, "chord_%d_%d.wav" % (os.getpid(), i))
        ffmpeg_run(["-f", "lavfi", "-i", "aevalsrc=%s:d=%.3f:s=48000" % (expr, bar),
                    "-ac", "2", "-ar", "48000", p])
        pieces.append(p)

    listing = os.path.join(tmp, "chords_%d.txt" % os.getpid())
    with io.open(listing, "w", encoding="utf-8") as fh:
        for p in pieces:
            fh.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))

    out = a.get("output")
    if not out:
        out = os.path.join(os.getcwd(), "music_%s_%ds.mp3" % (mood, int(total)))
    out = os.path.abspath(os.path.expandvars(os.path.expanduser(out)))
    parent = os.path.dirname(out)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Pad + air + optional pulse, softened and rolled off, then levelled.
    beat = 60.0 / bpm
    air = "anoisesrc=c=pink:d=%.3f:a=0.05" % total
    chain = ["[0:a]atrim=0:%.3f,lowpass=f=%d,aecho=0.8:0.85:60|180:0.25|0.15[pad]"
             % (total, int(spec["bright"]))]
    inputs = ["-f", "concat", "-safe", "0", "-i", listing, "-f", "lavfi", "-i", air]
    mix = "[pad][air]"
    chain.append("[1:a]lowpass=f=900,volume=0.5[air]")
    n_in = 2
    if a.get("pulse", True):
        kick = ("0.5*sin(2*PI*(70*exp(-9*mod(t\\,%.4f)))*mod(t\\,%.4f))"
                "*exp(-11*mod(t\\,%.4f))" % (beat, beat, beat))
        inputs += ["-f", "lavfi", "-i", "aevalsrc=%s:d=%.3f:s=48000" % (kick, total)]
        chain.append("[2:a]lowpass=f=200,volume=%.2f[pulse]" % float(a.get("pulse_level", 0.5)))
        mix += "[pulse]"
        n_in = 3
    chain.append("%samix=inputs=%d:duration=first:normalize=0,"
                 "afade=t=in:st=0:d=1.2,afade=t=out:st=%.3f:d=1.5,"
                 "loudnorm=I=%.1f:TP=-2:LRA=11[out]"
                 % (mix, n_in, max(0.0, total - 1.5), float(a.get("target_lufs", -20.0))))

    script = os.path.join(tmp, "music_%d.txt" % os.getpid())
    with io.open(script, "w", encoding="utf-8") as fh:
        fh.write(";".join(chain))
    try:
        ffmpeg_run(inputs + ["-filter_complex_script", script, "-map", "[out]",
                             "-t", "%.3f" % total, "-c:a", "libmp3lame", "-q:a", "3", out])
    finally:
        for p in pieces + [listing, script]:
            try:
                os.remove(p)
            except OSError:
                pass
    names = " - ".join("%s%s" % (["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][r % 12],
                                 "m" if s == "min" else "")
                       for r, s in chords)
    return done(out, "Synthesised a '%s' bed: %s, %.0f BPM, %.1fs, mixed at %.0f LUFS so it "
                     "sits under dialogue.\nNote: this is generated, not a licensed track - "
                     "simple by design." % (mood, names, bpm, duration_of(out),
                                            float(a.get("target_lufs", -20.0))))


# ---------------------------------------------------------------- shot craft
def t_reverse(a):
    """Play a shot backwards. Sound follows unless it is asked not to."""
    src = check_input(a.get("path"), "video")
    keep = bool(a.get("keep_audio", True)) and has_audio(src)
    out = make_output(src, "reverse", a.get("output"), ".mp4")
    fc = "[0:v]reverse[v]"
    maps = ["-map", "[v]"]
    if keep:
        fc += ";[0:a]areverse[aud]"
        maps += ["-map", "[aud]"]
    ffmpeg_run(["-i", src, "-filter_complex", fc] + maps + VIDEO_ENC +
               (AUDIO_ENC if keep else ["-an"]) + [out])
    return done(out, "Reversed.%s" % ("" if keep else " Silent."))


def t_freeze(a):
    """Hold a single frame, the way an editor lands a punchline or a product shot."""
    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    at = parse_time(a.get("at"), "at")
    if at is None or not 0 <= at <= total:
        raise ToolError("'at' must be inside the clip (0 - %.2fs)." % total)
    hold = float(a.get("hold", 1.2))
    if hold <= 0:
        raise ToolError("'hold' must be more than 0 seconds.")
    fps = fps_of(src)
    tmp = _tmpdir()
    still = os.path.join(tmp, "freeze_%d.png" % os.getpid())
    grab_frame(src, at, still)

    # The still becomes its own clip and is spliced in, so the freeze holds for exactly
    # as long as asked instead of depending on how the source was encoded.
    held = os.path.join(tmp, "held_%d.mp4" % os.getpid())
    args = ["-loop", "1", "-framerate", "%.6f" % fps, "-t", "%.3f" % hold, "-i", still]
    if has_audio(src):
        args += ["-f", "lavfi", "-t", "%.3f" % hold, "-i", "anullsrc=r=48000:cl=stereo"]
    ffmpeg_run(args + ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"] +
               FAST_ENC + (AUDIO_ENC if has_audio(src) else []) + [held])

    parts = []
    for i, (s, e) in enumerate(((0.0, at), (at, total))):
        if e - s < 0.04:
            continue
        seg = os.path.join(tmp, "fz%d_%d.mp4" % (i, os.getpid()))
        ffmpeg_run(["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % (e - s)] +
                   FAST_ENC + (AUDIO_ENC if has_audio(src) else ["-an"]) + [seg])
        parts.append(seg)
    parts.insert(1 if len(parts) > 1 else len(parts), held)

    out = make_output(src, "freeze", a.get("output"), ".mp4")
    res = t_merge({"paths": parts, "output": out})
    return done(out, "Froze the frame at %.2fs for %.1fs. New length %.2fs."
                % (at, hold, video_duration_of(out)))


def t_picture_in_picture(a):
    """Two shots on screen at once - inset, or split down the middle."""
    base = check_input(a.get("path"), "video")
    over = check_input(a.get("overlay"), "video")
    layout = (a.get("layout") or "corner").lower()
    info = probe(base)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    pad = int(a.get("margin", round(min(w, h) * 0.04)))
    scale = float(a.get("scale", 0.34))
    corner = (a.get("corner") or "top-right").lower()

    if layout == "corner":
        iw = max(2, int(w * scale) // 2 * 2)
        pos = {"top-left": ("%d" % pad, "%d" % pad),
               "top-right": ("W-w-%d" % pad, "%d" % pad),
               "bottom-left": ("%d" % pad, "H-h-%d" % pad),
               "bottom-right": ("W-w-%d" % pad, "H-h-%d" % pad)}.get(corner)
        if not pos:
            raise ToolError("corner must be top-left, top-right, bottom-left or bottom-right.")
        radius = ""
        fc = ("[0:v]scale=%d:%d,setsar=1[bg];"
              "[1:v]scale=%d:-2,setsar=1[fg];"
              "[bg][fg]overlay=%s:%s%s[v]" % (w, h, iw, pos[0], pos[1], radius))
        note = "%s inset at %.0f%% width" % (corner, scale * 100)
    elif layout in ("split-h", "split-v"):
        if layout == "split-v":                      # stacked, for a vertical frame
            ch = h // 2 // 2 * 2
            fc = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[a];"
                  "[1:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[b];"
                  "[a][b]vstack=inputs=2[v]" % (w, ch, w, ch, w, ch, w, ch))
            note = "stacked, each half %dx%d" % (w, ch)
        else:
            cw = w // 2 // 2 * 2
            fc = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[a];"
                  "[1:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[b];"
                  "[a][b]hstack=inputs=2[v]" % (cw, h, cw, h, cw, h, cw, h))
            note = "side by side, each half %dx%d" % (cw, h)
    else:
        raise ToolError("layout must be 'corner', 'split-h' or 'split-v'.")

    audio = (a.get("audio") or "base").lower()
    maps, aenc = ["-map", "[v]"], ["-an"]
    if audio == "base" and has_audio(base):
        maps += ["-map", "0:a"]
        aenc = AUDIO_ENC
    elif audio == "overlay" and has_audio(over):
        maps += ["-map", "1:a"]
        aenc = AUDIO_ENC
    elif audio == "both" and has_audio(base) and has_audio(over):
        fc += ";[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[aud]"
        maps += ["-map", "[aud]"]
        aenc = AUDIO_ENC

    out = make_output(base, "pip", a.get("output"), ".mp4")
    ffmpeg_run(["-i", base, "-i", over, "-filter_complex", fc] + maps +
               VIDEO_ENC + aenc + ["-shortest", out])
    return done(out, "Picture in picture: %s. Audio from the %s." % (note, audio))


def t_chroma_key(a):
    """Drop a green (or blue) screen and put something else behind it."""
    src = check_input(a.get("path"), "video")
    bg = a.get("background")
    colour = (a.get("colour") or "green").lower()
    keyed = {"green": "0x00d000", "blue": "0x0000d0"}.get(colour, colour)
    if not re.fullmatch(r"0x[0-9a-fA-F]{6}", keyed or ""):
        raise ToolError("colour must be 'green', 'blue' or a hex like 0x00d000.")
    sim = float(a.get("similarity", 0.30))
    blend = float(a.get("blend", 0.10))

    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2

    key = "chromakey=%s:%.3f:%.3f,despill=type=%s" % (
        keyed, sim, blend, "green" if colour == "green" else "blue")
    out = make_output(src, "keyed", a.get("output"), ".mp4")
    if bg:
        bgp = check_input(bg, "background")
        still = os.path.splitext(bgp)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        pre = ["-loop", "1", "-i", bgp] if still else ["-i", bgp]
        fc = ("[1:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[bg];"
              "[0:v]%s[fg];[bg][fg]overlay=0:0:shortest=1[v]" % (w, h, w, h, key))
        ffmpeg_run(pre[:0] + ["-i", src] + pre + ["-filter_complex", fc, "-map", "[v]"] +
                   (["-map", "0:a"] if has_audio(src) else []) + VIDEO_ENC +
                   (AUDIO_ENC if has_audio(src) else []) + ["-shortest", out])
        note = "replaced with %s" % os.path.basename(bgp)
    else:
        # No background given, so the key is written to alpha and kept in a format that
        # can actually carry it - mp4 cannot.
        out = os.path.splitext(out)[0] + ".webm"
        ffmpeg_run(["-i", src, "-vf", key + ",format=yuva420p",
                    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "24", "-b:v", "0"] +
                   (["-c:a", "libopus"] if has_audio(src) else ["-an"]) + [out])
        note = "left transparent (WebM, so the alpha survives)"
    return done(out, "Keyed out %s, %s." % (colour, note))


def t_stabilise(a):
    """Take the shake out of handheld footage. Two passes: measure, then correct."""
    src = check_input(a.get("path"), "video")
    strength = float(a.get("strength", 0.6))
    if not 0 < strength <= 1:
        raise ToolError("strength must be between 0 and 1.")
    tmp = _tmpdir()
    zoom = float(a.get("zoom", 1.0 + 0.05 * strength))
    out = make_output(src, "steady", a.get("output"), ".mp4")
    # A filter argument splits on ':', so a Windows drive letter in the path for the
    # motion data breaks the whole filter graph. Run from the folder and name the file
    # bare, the same dodge libass needs.
    name = "stab_%d.trf" % os.getpid()
    trf = os.path.join(tmp, name)

    def ff(args, label):
        p = subprocess.run(["ffmpeg", "-y", "-v", "error"] + args, cwd=tmp,
                           capture_output=True, text=True)
        if p.returncode:
            raise ToolError("%s failed:\n%s" % (label, (p.stderr or "").strip()[-400:]))

    ff(["-i", os.path.abspath(src), "-vf",
        "vidstabdetect=shakiness=%d:accuracy=15:result=%s"
        % (max(1, min(10, int(round(strength * 10)))), name), "-f", "null", "-"],
       "Measuring the shake")
    if not os.path.isfile(trf):
        raise ToolError("Stabilisation could not measure the motion in this clip.")
    ff(["-i", os.path.abspath(src), "-vf",
        "vidstabtransform=input=%s:smoothing=%d:zoom=%.2f:optzoom=0:interpol=bicubic,"
        "unsharp=5:5:0.4:3:3:0.2" % (name, max(2, int(round(10 + 40 * strength))),
                                     (zoom - 1.0) * 100)] +
       VIDEO_ENC + (["-c:a", "copy"] if has_audio(src) else ["-an"]) +
       [os.path.abspath(out)], "Stabilising")
    try:
        os.remove(trf)
    except OSError:
        pass
    return done(out, "Stabilised at strength %.2f, cropping in %.1f%% to hide the edges."
                % (strength, (zoom - 1.0) * 100))


def t_grade_lut(a):
    """Apply a .cube LUT - the way a house look is normally shipped between editors."""
    src = check_input(a.get("path"), "video")
    lut = check_input(a.get("lut"), "LUT")
    if not lut.lower().endswith(".cube"):
        raise ToolError("Give a .cube file. Other LUT formats are not supported.")
    amount = float(a.get("amount", 1.0))
    if not 0 <= amount <= 1:
        raise ToolError("amount must be between 0 and 1.")
    path = os.path.abspath(lut)
    cwd, name = os.path.dirname(path), os.path.basename(path)
    out = make_output(src, "graded", a.get("output"), ".mp4")
    if amount >= 0.999:
        vf = "lut3d=%s" % name
    else:
        # Blend the graded picture back over the original so the look can be dialled in.
        vf = ("split[o][g];[g]lut3d=%s[gg];[o][gg]blend=all_mode=normal:all_opacity=%.3f"
              % (name, amount))
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src),
                        "-vf", vf] + VIDEO_ENC +
                       (["-c:a", "copy"] if has_audio(src) else ["-an"]) +
                       [os.path.abspath(out)], cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise ToolError("LUT failed:\n" + (p.stderr or "").strip()[-400:])
    return done(out, "Applied %s at %.0f%%." % (name, amount * 100))


def t_remove_logo(a):
    """Blur out a fixed watermark or bug by interpolating from its edges."""
    src = check_input(a.get("path"), "video")
    try:
        x, y = int(a["x"]), int(a["y"])
        w, h = int(a["width"]), int(a["height"])
    except (KeyError, TypeError, ValueError):
        raise ToolError("Give x, y, width and height of the area to cover.")
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    vw, vh = int(vs.get("width", 0)), int(vs.get("height", 0))
    if x < 1 or y < 1 or x + w > vw - 1 or y + h > vh - 1:
        raise ToolError("delogo needs a one-pixel margin inside the frame: the area must sit "
                        "within 1,1 - %d,%d." % (vw - 1, vh - 1))
    out = make_output(src, "nologo", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", "delogo=x=%d:y=%d:w=%d:h=%d" % (x, y, w, h)] +
               VIDEO_ENC + (["-c:a", "copy"] if has_audio(src) else ["-an"]) + [out])
    return done(out, "Covered %dx%d at (%d,%d). This interpolates from the surrounding "
                     "pixels - it works on a flat background and smears over a busy one."
                % (w, h, x, y))


def _photo_taken(path):
    """When the photo was taken, for putting an evening back in order.

    Phones number files in shooting order, so the name is a decent fallback - but
    only within one camera. EXIF is the only thing that survives copying a folder
    together from three people's phones, which is how a family album is made.

    Returns None when there is no date, so the caller can SAY it fell back rather
    than reporting date order it did not actually do. Saving a photo through a
    chat app or a download strips EXIF completely: on the album this was written
    for, nought of twenty-one still had a date.
    """
    try:
        from PIL import Image
        exif = Image.open(path)._getexif() or {}
        for tag in (36867, 36868, 306):     # DateTimeOriginal, Digitized, DateTime
            v = exif.get(tag)
            if v:
                return str(v)
    except Exception:
        pass
    return None


def _quietest_span(src, length=4.0):
    """The calmest stretch in a clip - where its room tone lives.

    Not silence: a party recording has none. The quietest four seconds still hold
    the fridge, the fan and the shape of the room, and that is exactly what is
    missing from a photo montage. Wrong choice here and you loop somebody's
    half-sentence under twenty photographs, so it is measured, not guessed at.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    if not has_audio(src):
        return None
    raw = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                          "-i", os.path.abspath(src), "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    x = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    step = 8000                                   # half a second
    if len(x) < step * int(length * 2 + 2):
        return None
    rms = np.array([float(np.sqrt(np.mean(x[i:i + step] ** 2)) + 1e-9)
                    for i in range(0, len(x) - step, step)])
    span = max(2, int(length * 2))
    cum = np.concatenate([[0.0], np.cumsum(rms)])
    tot = cum[span:] - cum[:-span]
    i = int(tot.argmin())
    return (round(i * 0.5, 2), round(i * 0.5 + length, 2))


def _loud_windows(src, want, length, gap=4.0):
    """The moments in a clip where something actually happens.

    A birthday video is one long take in which two things matter - the singing and
    the cheer - and both are found by level, not by the words: laughter, clapping
    and singing never reach a transcript. Measured on a real one, the cheer sat 6 dB
    above the chatter either side of it.
    """
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", os.path.abspath(src),
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    total = video_duration_of(src)
    step = 0.5
    try:
        import numpy as np
    except ImportError:
        return [(0.0, min(length, total))]
    x = np.frombuffer(raw, dtype=np.float32)
    n = int(16000 * step)
    if len(x) < n * 4:
        return [(0.0, min(length, total))]
    lev = np.array([float(np.sqrt(np.mean(x[i:i + n] ** 2)))
                    for i in range(0, len(x) - n, n)])
    span = max(1, int(round(length / step)))
    # energy of every candidate window, by rolling sum
    cum = np.concatenate([[0.0], np.cumsum(lev)])
    score = cum[span:] - cum[:-span]
    picks = []
    for _ in range(want):
        if not len(score) or float(score.max()) <= 0:
            break
        i = int(score.argmax())
        start = min(max(0.0, i * step), max(0.0, total - length))
        picks.append((start, min(total, start + length)))
        lo = max(0, int(i - (length + gap) / step))
        hi = min(len(score), int(i + (length + gap) / step))
        score[lo:hi] = 0.0
    return sorted(picks) or [(0.0, min(length, total))]


def _near_twins(photos, limit=18.0):
    """Which neighbours are so alike that holding both reads as a stutter?

    People shoot the same picture twice. Two frames a second apart, held the same
    length with the same dissolve, look like the player hiccupped rather than like
    two moments. The fix is not to drop one - the family wants every photo - it is
    to let the second go past quickly, the way a second look actually feels.

    The threshold is measured, not guessed. Mean absolute difference of a 16x16
    grey signature across the twenty adjacent pairs of a real album: the one true
    repeat scored 7.7, the next closest pair 30.1, the median 53.4. 18 sits in the
    gap. Same-setup-different-pose lands in the thirties and is left alone, which
    is right - those are separate beats.
    """
    try:
        from PIL import Image
    except ImportError:
        return set()
    sigs = []
    for p in photos:
        try:
            with Image.open(p) as im:
                sigs.append(list(im.convert("L").resize((16, 16), Image.LANCZOS)
                                 .getdata()))
        except Exception:
            sigs.append(None)
    twins = set()
    for i in range(1, len(sigs)):
        a, b = sigs[i - 1], sigs[i]
        if not a or not b:
            continue
        if sum(abs(x - y) for x, y in zip(a, b)) / float(len(a)) < limit:
            twins.add(i)
    return twins


def _exposure_match(photos, strength=0.65):
    """Pull each photo's exposure toward the middle of the set.

    The most visible fault a photo film can have, and the one nobody thinks to
    look for: twenty-one pictures shot across a living room, a kitchen and an
    office flicker light-dark-light as they cut. Measured on a real album the
    brightness spread was 91.8 of 255 - one kitchen shot sat 63 below the median
    while a selfie sat 29 above. White balance, by contrast, was fine (spread
    under 12), so this corrects exposure and leaves colour alone.

    PARTIALLY, though. Dragging a genuinely dim room all the way to the median
    turns it grey and noisy and looks corrected. 0.65 closes most of the gap and
    keeps each photograph looking like the room it was taken in.

    Gamma rather than an additive lift: gamma moves the midtones and shadows and
    leaves the highlights, so a lifted photo does not go flat and milky.
    """
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return {}, 0.0, 0.0
    means = {}
    for p in photos:
        try:
            with Image.open(p) as im:
                means[p] = max(4.0, ImageStat.Stat(im.convert("L")).mean[0])
        except Exception:
            pass
    if len(means) < 3:
        return {}, 0.0, 0.0
    vals = sorted(means.values())
    med = vals[len(vals) // 2]
    out = {}
    for p, y in means.items():
        target = med + (y - med) * (1.0 - strength)
        g = math.log(y / 255.0) / math.log(max(0.02, target / 255.0))
        g = min(1.9, max(0.55, g))
        if abs(g - 1.0) > 0.02:      # below this nobody could tell, so do not bother
            out[p] = g
    return out, med, vals[-1] - vals[0]


def _kb_filter(i, dur, cw, ch, fps):
    """A slow move on a still, alternating in and out so a run never pulses.

    Driven by the output frame number rather than by accumulating `zoom+step`: the
    accumulating form drifts, and over a couple of seconds the drift shows up as the
    move easing off early. Working at twice the output size keeps it smooth -
    zoompan steps in whole source pixels.
    """
    fr = max(2, int(round(dur * fps)))

    # Every still moving the same distance at the same rate is a second kind of
    # metronome - the holds were made uneven and the MOVES were left identical,
    # which is a machine breathing steadily. Six moves, cycled by position, so a
    # run never repeats itself twice over: a hard push, a slow pull, a drift with
    # barely any zoom, a rise, a fall, and one that is almost still. Chosen by
    # index rather than at random so a saved plan renders the same film twice.
    MOVES = [
        (0.155,  1, 0.000,  0.030),   # push in, drifting down
        (0.090, -1, 0.000, -0.024),   # pull back, rising
        (0.045,  1, 0.034,  0.000),   # barely zooms - pans across instead
        (0.130, -1, -0.026, 0.000),   # pull back and slide the other way
        (0.020,  1, 0.000,  0.010),   # nearly still. Let one just sit there.
        (0.110,  1, 0.018, -0.018),   # push in on a diagonal
    ]
    amt, sign, px, py = MOVES[i % len(MOVES)]
    lo, hi = (1.0, 1.0 + amt) if sign > 0 else (1.0 + amt, 1.0)
    step = amt / fr
    z = (("min(1.0+%.6f*on,%.4f)" % (step, hi)) if sign > 0
         else ("max(%.4f-%.6f*on,1.0)" % (lo, step)))
    return ("scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d,"
            "zoompan=z='%s':d=%d:"
            "x='iw/2-(iw/zoom/2)+%.4f*iw*on/%d':y='ih/2-(ih/zoom/2)+%.4f*ih*on/%d':"
            "s=%dx%d:fps=%d,unsharp=5:5:0.45:3:3:0.2"
            % (cw * 2, ch * 2, cw * 2, ch * 2, z, fr, px, fr, py, fr, cw, ch, fps))


def _look_filter(warmth, vignette, contrast):
    """A look, rather than a saturation number.

    Three small things, none of them noticeable on its own, which together are
    most of what separates a graded film from an ungraded one:

      WARMTH    a touch of red in the highlights and blue out of the shadows. A
                room lit by tungsten and a phone's auto white balance fight each
                other; nudging everything the same way settles it.
      CONTRAST  an S-curve, not a level change. Deepens the shadows and opens the
                highlights while leaving skin - which lives in the middle - alone.
                A straight contrast lift takes the faces with it.
      VIGNETTE  barely there. The eye goes to the brightest part of the frame, so
                darkening the corners by a few percent points it at the faces
                without anyone noticing it has been done. Measured hard limit:
                past about PI/9 it crushes an already-dark foreground into what
                reads as a black bar.
    """
    parts = []
    if abs(contrast) > 0.005:
        c = max(-0.35, min(0.35, contrast))
        parts.append("curves=all='0/0 %.3f/%.3f %.3f/%.3f 1/1'"
                     % (0.25, 0.25 - c * 0.16, 0.75, 0.75 + c * 0.16))
    if abs(warmth) > 0.005:
        w = max(-1.0, min(1.0, warmth))
        parts.append("colorbalance=rs=%.3f:gs=%.3f:bs=%.3f:rh=%.3f:bh=%.3f"
                     % (-0.03 * w, -0.01 * w, 0.045 * w, 0.05 * w, -0.035 * w))
    if vignette > 0.005:
        v = max(0.0, min(1.0, vignette))
        parts.append("vignette=angle=PI/%.2f:mode=forward" % (14.0 - 5.0 * v))
    return ",".join(parts)


def _panel_filter(cw, ch, fps):
    """A clip of the wrong shape, in its own panel over a blur of itself.

    Cropping a group shot to a tall frame throws away whoever is standing at the
    edges - which at a party is half the family. The blur reads as deliberate.

    DENOISE BEFORE SHARPENING. A phone clip carries compression noise, and
    sharpening it alone amplifies the noise as much as the detail - the flat wall
    behind the family came out crawling. hqdn3d first, then a stronger unsharp,
    was visibly cleaner in the wall AND crisper on the cake sprinkles. nlmeans was
    tried too and rejected: it smoothed the faces to plastic.
    """
    return ("[0:v]split=2[a][b];"
            "[a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "gblur=sigma=30,eq=brightness=-0.07:saturation=0.75[bg];"
            "[b]hqdn3d=2:1.5:3:3,scale=%d:-2:flags=lanczos,"
            "unsharp=5:5:0.85:3:3:0.4[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=%d[v]"
            % (cw, ch, cw, ch, cw, fps))


def t_montage(a):
    """Photos and clips into one film: order, motion, music that ducks, titles.

    Everything here is a judgement that had to be made by hand the first time and
    is the same judgement every time: put the evening back in chronological order,
    open and close on the moments where the room is loudest, move slowly over every
    still, let a wrongly-shaped clip keep its edges, and pull the music down
    wherever there is something real to hear.

    What it cannot judge is which photos to leave out, and it does not try.
    """
    # A saved plan is the starting point; anything passed explicitly overrides it,
    # so "same film but 2 seconds longer on the kiss" is one argument, not a
    # rebuilt call.
    if a.get("plan"):
        pf = check_input(a["plan"], "plan")
        with io.open(pf, encoding="utf-8") as fh:
            saved = json.load(fh)
        base = {"shape": saved.get("shape"), "fps": saved.get("fps"),
                "transition": saved.get("transition"),
                "transition_seconds": saved.get("transition_seconds"),
                "title": saved.get("title"), "closing": saved.get("closing"),
                "font": saved.get("font"), "font_scale": saved.get("font_scale"),
                "accent": saved.get("accent"),
                "margin_bottom": saved.get("margin_bottom"),
                "order": "given",
                "photos": [p["file"] for p in saved.get("photos") or []],
                "seconds_each": [p["seconds"] for p in saved.get("photos") or []],
                "clips": sorted(set(c["file"] for c in saved.get("clips") or [])),
                "clip_moments": saved.get("clips") or []}
        base.update(saved.get("look") or {})
        base.update({k: v for k, v in (saved.get("sound") or {}).items() if v is not None})
        for k, v in base.items():
            if v is not None and k not in a:
                a[k] = v

    photos = a.get("photos") or []
    if isinstance(photos, str) and os.path.isdir(photos):
        photos = [os.path.join(photos, f) for f in sorted(os.listdir(photos))
                  if os.path.splitext(f)[1].lower() in
                  (".jpg", ".jpeg", ".png", ".webp", ".bmp")]
    photos = [check_input(p, "photo") for p in photos]
    if not photos:
        raise ToolError("Give 'photos': a list of image files, or a folder of them.")

    order = (a.get("order") or "date").lower()
    if order not in ("date", "name", "given"):
        raise ToolError("order must be date, name or given.")
    dated = {p: _photo_taken(p) for p in photos} if order == "date" else {}
    with_date = sum(1 for v in dated.values() if v)
    if order == "date":
        photos.sort(key=lambda p: (0, dated[p], os.path.basename(p).lower())
                    if dated[p] else (1, "", os.path.basename(p).lower()))
    elif order == "name":
        photos.sort(key=lambda p: os.path.basename(p).lower())
    order_said = {"date": "date-taken", "name": "filename",
                  "given": "the given"}[order] + " order"
    if order == "date" and with_date < len(photos):
        order_said = ("filename order - %d of %d photos have no date in them, so "
                      "date order was not available and the sequence is only as good "
                      "as the numbering"
                      % (len(photos) - with_date, len(photos))) if not with_date else (
                      "date order for %d photos, filename order for the %d with no date"
                      % (with_date, len(photos) - with_date))

    # Chronological is the truth and is usually the right spine, but the last frame
    # is the one thing it reliably gets wrong: an evening does not end on its best
    # picture, it ends on whatever happened last. On the birthday this tool was
    # built from, date order finished on two children eating cherries in the
    # kitchen instead of on both of them kissing her. Naming the closer is the one
    # piece of judgement worth asking for.
    def lift(which, to_front):
        want = a.get(which)
        if not want:
            return
        key = os.path.splitext(os.path.basename(str(want)))[0].lower()
        hit = next((p for p in photos
                    if os.path.splitext(os.path.basename(p))[0].lower() == key), None)
        if hit is None:
            hit = next((p for p in photos if key in os.path.basename(p).lower()), None)
        if hit is None:
            raise ToolError("%s: no photo here is called '%s'." % (which, want))
        photos.remove(hit)
        photos.insert(0, hit) if to_front else photos.append(hit)

    lift("open_with", True)
    lift("finish_with", False)

    clips = [check_input(c, "clip") for c in (a.get("clips") or [])]
    # seconds_each takes a LIST as readily as a number, because an even hold on
    # every photo is the thing that most makes a montage feel machine-made. A
    # person lingers on the picture that matters and moves through the ones that
    # are only there for completeness, and gives the payoff twice the room.
    _se = a.get("seconds_each", 2.4)
    if isinstance(_se, (list, tuple)):
        if len(_se) != len(photos):
            raise ToolError("seconds_each has %d entries for %d photos - give one "
                            "number, or exactly one per photo in play order."
                            % (len(_se), len(photos)))
        per_list = [max(0.4, float(x)) for x in _se]
        per = statistics.fmean(per_list)
    else:
        per = float(_se)
        per_list = None
    hold = float(a.get("hold_last", (per_list[-1] if per_list else per * 1.9)))
    xf = float(a.get("transition_seconds", 0.45))
    fps = int(a.get("fps") or 30)

    # Shape follows the photos: most family albums are portrait, and forcing them
    # into a landscape frame pillarboxes twenty pictures to accommodate one clip.
    shape = (a.get("shape") or "auto").lower()
    if shape == "auto":
        tall = 0
        for p in photos:
            try:
                from PIL import Image
                with Image.open(p) as im:
                    tall += 1 if im.height >= im.width else -1
            except Exception:
                pass
        shape = "4:5" if tall >= 0 else "16:9"
    sizes = {"4:5": (1080, 1350), "9:16": (1080, 1920), "1:1": (1080, 1080),
             "16:9": (1920, 1080), "3:4": (1080, 1440)}
    if shape not in sizes:
        raise ToolError("shape must be auto, 4:5, 9:16, 1:1, 3:4 or 16:9.")
    cw, ch = sizes[shape]

    tmp = _tmpdir()
    tag = os.getpid()

    # A single grade across the whole film is correct and slightly lifeless. Real
    # ones drift: a shade cooler before anything has happened, warmest by the end.
    # Nobody watching notices it; they notice that the ending feels warmer.
    _warm = float(a.get("warmth", 0.0))
    _drift = float(a.get("warmth_drift", 0.45 if _warm else 0.0))

    def look_at(pos):
        """pos 0..1 through the film."""
        return _look_filter(_warm * (1.0 - _drift + _drift * 2.0 * pos),
                            float(a.get("vignette", 0.0)),
                            float(a.get("contrast", 0.0)))

    look = look_at(0.5)
    gammas, exp_med, exp_spread = ({}, 0.0, 0.0)
    if a.get("match_exposure", True):
        gammas, exp_med, exp_spread = _exposure_match(
            photos, float(a.get("match_strength", 0.65)))

    def still(spec):
        i, path, dur = spec
        out = os.path.join(tmp, "mg_s%d_%03d.mp4" % (tag, i))
        vf = _kb_filter(i, dur, cw, ch, fps)
        if path in gammas:
            vf = "eq=gamma=%.4f," % gammas[path] + vf
        step_look = look_at(i / float(max(1, len(photos) - 1)))
        if step_look:
            vf = vf + "," + step_look
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-r", str(fps),
             "-i", os.path.abspath(path),
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", "%.3f" % dur, "-vf", vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-threads", "2", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-shortest", out], check=True)
        return out

    def moment(spec):
        k, src, a0, b0 = spec
        out = os.path.join(tmp, "mg_c%d_%03d.mp4" % (tag, k))
        pf = _panel_filter(cw, ch, fps)
        if look:
            pf = pf.replace(",setsar=1,fps=%d[v]" % fps,
                            "," + look + ",setsar=1,fps=%d[v]" % fps)
        args = ["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % a0,
                "-i", os.path.abspath(src), "-t", "%.3f" % (b0 - a0),
                "-filter_complex", pf, "-map", "[v]"]
        args += (["-map", "0:a"] if has_audio(src) else
                 ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "1:a",
                  "-shortest"])
        subprocess.run(args + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                               "-threads", "2", "-pix_fmt", "yuv420p",
                               "-c:a", "aac", "-b:a", "192k", out], check=True)
        return out

    # --- what goes where ------------------------------------------------------
    clips_for_tone = list(clips)
    room_tone = None
    clip_len = float(a.get("clip_seconds", 13.0))
    tail_len = float(a.get("clip_tail_seconds", 3.7))
    heads, tails = [], []
    saved_moments = a.get("clip_moments")
    if saved_moments:
        for m in saved_moments:
            (heads if m.get("role") != "close" else tails).append(
                (m["file"], float(m["start"]), float(m["end"])))
        clips = []          # already decided - do not go looking again
    for src in clips:
        if a.get("clip_highlights", True) and has_audio(src):
            head = _loud_windows(src, 1, clip_len)[0]
            heads.append((src,) + head)
            # The closing beat has to be searched at ITS OWN length. Taking the
            # second-best THIRTEEN-second window and keeping its first four
            # seconds does not find the four-second moment - on the party clip it
            # returned 27.5s of chatter and walked straight past the cheer at 49s,
            # which is 6 dB above everything around it and the obvious ending.
            dur = video_duration_of(src)
            cands = _loud_windows(src, 4, tail_len)
            after = [w for w in cands if w[0] >= head[1] + 1.0]
            # Prefer a loud moment from AFTER the opening, and the latest of those.
            # An ending comes from the end of the take: on the party clip the
            # candidates were 1.5s, 9.0s, 18.0s and 48.5s, and only the last is
            # the cheer everyone lets out when the cake is cut.
            best = (after[-1] if after else
                    ([w for w in cands if w[1] <= head[0] - 1.0] or [None])[-1])
            if best:
                tails.append((src, best[0], min(best[1], dur)))
        else:
            heads.append((src, 0.0, min(clip_len, video_duration_of(src))))

    twins = (_near_twins(photos, float(a.get("twin_limit", 18.0)))
             if a.get("shorten_repeats", True) else set())
    twin_hold = per * float(a.get("repeat_scale", 0.55))
    def dur_of(i):
        if per_list:                       # an explicit plan always wins
            return per_list[i]
        if i == len(photos) - 1:
            return hold
        return twin_hold if i in twins else per

    jobs = [(i, p, dur_of(i)) for i, p in enumerate(photos)]
    workers = max(2, min(4, (os.cpu_count() or 4) // 3))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        stills = list(ex.map(still, jobs))
        head_f = list(ex.map(moment, [(i, s, x, y) for i, (s, x, y) in enumerate(heads)]))
        tail_f = list(ex.map(moment, [(90 + i, s, x, y)
                                      for i, (s, x, y) in enumerate(tails)]))

    pieces = head_f + stills[:-1] + tail_f + stills[-1:]
    lens = ([b - x for _, x, b in heads] +
            [dur_of(i) for i in range(len(stills) - 1)] +
            [b - x for _, x, b in tails] + [dur_of(len(photos) - 1)])

    at, marks = 0.0, []
    for d in lens:
        marks.append(at)
        at += d - xf
    total = at + xf
    last_at = marks[-1]
    sound_at = ([(marks[i], marks[i] + lens[i]) for i in range(len(head_f))] +
                [(marks[len(head_f) + len(stills) - 1 + i],
                  marks[len(head_f) + len(stills) - 1 + i] + lens[len(head_f) + len(stills) - 1 + i])
                 for i in range(len(tail_f))])

    # --- their own sound, carried across the cut -------------------------------
    # The singing stopping dead the instant the photographs start is the loudest
    # amateur tell in the whole film. A cut in the PICTURE does not have to be a
    # cut in the SOUND: the room carries on for a moment underneath, then gives
    # way. Same at the other end - the cheer is heard a beat before it is seen,
    # so the ending feels arrived at rather than announced.
    bleeds = []
    bleed = float(a.get("audio_bleed", 2.2))
    if bleed > 0.05:
        for k, (src_, x0, x1) in enumerate(heads + tails):
            is_head = k < len(head_f)
            idx = k if is_head else len(head_f) + len(stills) - 1 + (k - len(head_f))
            if idx >= len(marks):
                continue
            dur_src = video_duration_of(src_)
            if is_head:                       # runs ON past the picture (L-cut)
                a0, a1 = x1, min(dur_src, x1 + bleed)
                at = marks[idx] + lens[idx] - xf
                fade = "afade=t=out:st=0.35:d=%.2f" % max(0.3, (a1 - a0) - 0.35)
            else:                             # arrives BEFORE it (J-cut)
                a0, a1 = max(0.0, x0 - bleed), x0
                at = max(0.0, marks[idx] - (a1 - a0))
                fade = "afade=t=in:st=0:d=%.2f" % max(0.3, (a1 - a0) * 0.7)
            if a1 - a0 < 0.4:
                continue
            wav = os.path.join(tmp, "mg_bleed%d_%d.wav" % (k, tag))
            # -t goes BEFORE -i, so it limits what is READ. After -i it limits the
            # output instead, and adelay has just pushed the sound past that
            # cutoff - leaving a file of pure padding. Measured: the "bleed" made
            # the moment 3 dB QUIETER, because all that reached the mix was the
            # silence in front of it.
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                 "-ss", "%.3f" % a0, "-t", "%.3f" % (a1 - a0),
                 "-i", os.path.abspath(src_), "-vn",
                 "-af", "%s,adelay=%d|%d" % (fade, int(at * 1000), int(at * 1000)),
                 "-ac", "2", "-ar", "48000", wav],
                capture_output=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and os.path.isfile(wav):
                bleeds.append((wav, "L-cut" if is_head else "J-cut", round(a1 - a0, 2)))

    # --- music, pulled down wherever the room can be heard ---------------------
    music, mvol = a.get("music"), float(a.get("music_volume", 0.75))
    if music:
        music = check_input(music, "music")
    elif a.get("music_mood", "warm"):
        music = os.path.join(tmp, "mg_bed_%d.wav" % tag)
        t_music_generate({"mood": a.get("music_mood") or "warm",
                          "duration": total + 2.0, "bpm": a.get("music_bpm") or 92,
                          "pulse_level": 0.35, "output": music})
    if music and sound_at:
        # One expression rather than a chain of volume filters, so the ramps cross
        # cleanly instead of multiplying together at the joins.
        duckv = float(a.get("music_duck", 0.20))
        e, closes = "", 0
        for s, en in sound_at:
            e += ("if(lt(t,%.2f),%.3f,if(lt(t,%.2f),%.3f+(%.3f)*(t-%.2f)/0.90,"
                  "if(lt(t,%.2f),%.3f,if(lt(t,%.2f),%.3f+(%.3f)*(t-%.2f)/0.90,"
                  % (s - 1.1, mvol, s - 0.2, mvol, duckv - mvol, s - 1.1,
                     en - 0.5, duckv, en + 0.4, duckv, mvol - duckv, en - 0.5))
            closes += 4
        e += "%.3f%s" % (mvol, ")" * closes)
        ducked = os.path.join(tmp, "mg_duck_%d.wav" % tag)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(music),
                        "-af", "volume='%s':eval=frame,afade=t=out:st=%.2f:d=2.0"
                        % (e, max(0.1, total - 2.2)), ducked], check=True)
        music, mvol = ducked, 1.0

    # --- sound design, kept deliberately small ---------------------------------
    # A montage with music and nothing else has a hole in it: between the songs
    # and the voices there is digital silence, which is the clearest sign nobody
    # was in the room. Room tone taken from the party's own recording fills it,
    # and it is the SAME room, which no synthesised hiss can be.
    #
    # Two accents at most. I cannot hear these, so I will not scatter them: what
    # can be checked is that the level is low enough not to intrude, and that is
    # all this claims.
    sfx = list(a.get("sfx") or [])
    tone_note = ""
    # Room tone only earns its place where there is SILENCE to fill. Measured on
    # the birthday film, which has music running throughout: the floor under the
    # photographs was already -17.2 dB, tone was laid in at -40, and the floor
    # afterwards was -17.8 - i.e. it did nothing at all, completely masked. A
    # feature that cannot be heard should not be claimed, so it is now skipped
    # whenever a music bed already covers the whole film, and says so.
    tone_wanted = a.get("sound_design", True) and clips_for_tone
    if tone_wanted and (a.get("music") or a.get("music_mood", "warm")) \
            and float(a.get("music_volume", 0.75)) >= 0.35:
        tone_wanted = False
        tone_note = ("room tone skipped - the music bed already runs the whole film, "
                     "so tone under it is inaudible. It is for a montage with gaps")
    if tone_wanted:
        src = clips_for_tone[0]
        quiet = _quietest_span(src, 4.0)
        tone_note = ""
        if quiet:
            tone = os.path.join(tmp, "mg_tone_%d.wav" % tag)
            q = subprocess.run(
                [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
                 "-ss", "%.2f" % quiet[0], "-i", os.path.abspath(src),
                 "-t", "%.2f" % (quiet[1] - quiet[0]), "-vn",
                 "-af", "highpass=f=60,lowpass=f=7000,afade=t=in:st=0:d=0.6,"
                        "afade=t=out:st=%.2f:d=0.6,loudnorm=I=-40:TP=-20"
                        % max(0.1, (quiet[1] - quiet[0]) - 0.6),
                 "-ac", "2", "-ar", "48000", tone],
                capture_output=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if q.returncode == 0 and os.path.isfile(tone):
                room_tone = tone
                tone_note = ("room tone lifted from the party's own recording "
                             "(%.1f-%.1fs, its quietest stretch) laid under the "
                             "photographs at -40 LUFS" % quiet)
    if a.get("sound_design", True) and not a.get("sfx"):
        if len(marks) > len(head_f):
            sfx.append({"sound": "reveal", "gain": 0.35,
                        "at": "%.2f" % max(0.0, marks[len(head_f)] - 0.12)})
        if last_at > 1.0:
            sfx.append({"sound": "sparkle", "gain": 0.28,
                        "at": "%.2f" % max(0.0, last_at - 0.05)})

    cues = list(a.get("captions") or [])
    title_at = min(1.1, total * 0.05)
    beat_note = ""
    if a.get("title") and music and a.get("align_title", True):
        try:                       # land it on a beat of the actual score
            env, rate = _onset_envelope(music)
            bpm, beats = _beat_grid(env, rate)
            near = [b for b in beats if 0.6 <= b <= 3.2]
            if near:
                title_at = min(near, key=lambda b: abs(b - title_at))
                beat_note = ("title lands on a beat of the score (%.2fs, %.0f BPM) "
                             "rather than on an arbitrary second" % (title_at, bpm))
        except Exception:
            pass
    if a.get("title"):
        cues.insert(0, {"start": "%.2f" % title_at,
                        "end": "%.2f" % min(5.4, total * 0.25),
                        "text": a["title"]})
    if a.get("closing"):
        cues.append({"start": "%.2f" % (last_at + 0.55),
                     "end": "%.2f" % max(last_at + 1.2, total - 0.35),
                     "text": a["closing"]})

    # --- the plan, written down so the next change is one line -----------------
    # Every small change used to mean rebuilding the whole call from scratch, and
    # that is why an edit took six minutes rather than one. Cheap iteration is how
    # an edit actually gets good: five versions tried beats one version reasoned
    # about. So the resolved plan - what was chosen, not what was asked for -
    # comes back out in a form that can be edited and fed straight back in.
    plan = {
        "version": 1,
        "shape": shape, "width": cw, "height": ch, "fps": fps,
        "transition_seconds": xf, "transition": a.get("transition") or "fade",
        "photos": [{"file": p, "seconds": round(dur_of(i), 3)}
                   for i, p in enumerate(photos)],
        "clips": ([{"file": s_, "start": round(x, 3), "end": round(b, 3), "role": "open"}
                   for s_, x, b in heads] +
                  [{"file": s_, "start": round(x, 3), "end": round(b, 3), "role": "close"}
                   for s_, x, b in tails]),
        "title": a.get("title"), "closing": a.get("closing"),
        "font": a.get("font") or "TH Baijam",
        "font_scale": float(a.get("font_scale", 3.5)),
        "accent": a.get("accent") or "#ffd479",
        "margin_bottom": float(a.get("margin_bottom", 0.055)),
        "look": {"warmth": float(a.get("warmth", 0.0)),
                 "contrast": float(a.get("contrast", 0.0)),
                 "vignette": float(a.get("vignette", 0.0)),
                 "grain": float(a.get("grain", 0.20)),
                 "saturation": float(a.get("saturation", 1.06)),
                 "match_exposure": bool(a.get("match_exposure", True))},
        "sound": {"music": a.get("music"), "music_mood": a.get("music_mood"),
                  "music_volume": float(a.get("music_volume", 0.75)),
                  "music_duck": float(a.get("music_duck", 0.20))},
    }
    plan_path = a.get("save_plan")
    if plan_path:
        plan_path = os.path.abspath(plan_path)
        _mkdirs(os.path.dirname(plan_path))
        with io.open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=1)

    build = {"paths": pieces, "transition": a.get("transition") or "fade",
             "duration": xf, "audio_crossfade": xf, "fps": fps,
             "grain": float(a.get("grain", 0.20)),
             "desaturate": float(a.get("saturation", 1.06)),
             "speech_timing": False,
             "font": a.get("font") or "TH Baijam",
             "font_scale": float(a.get("font_scale", 3.5)),
             "outline": float(a.get("outline", 0.095)),
             "glow": float(a.get("glow", 0.9)),
             "accent": a.get("accent") or "#ffd479",
             "text_color": a.get("text_color") or "#ffffff",
             "margin_bottom": float(a.get("margin_bottom", 0.055)),
             "target_lufs": a.get("target_lufs", -14),
             "true_peak": a.get("true_peak", -1.5),
             "output": make_output(photos[0], "montage", a.get("output"), ".mp4")}
    if cues:
        build["captions"] = cues
    if sfx:
        build["sfx"] = sfx
    if room_tone:
        # Underneath the music, not mixed into it: the bed gets ducked under the
        # clips and the room should not duck with it - it is what the clips ARE.
        bedded = os.path.join(tmp, "mg_bed_tone_%d.wav" % tag)
        n_loops = int(total / max(1.0, duration_of(room_tone))) + 2
        rc = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
             "-stream_loop", str(n_loops), "-i", room_tone] +
            (["-i", os.path.abspath(music)] if music else []) +
            ["-filter_complex",
             ("[0:a]atrim=0:%.2f,volume=%.3f[t];[1:a]volume=%.3f[m];"
              "[t][m]amix=inputs=2:duration=longest:normalize=0[o]"
              % (total, float(a.get("room_tone_level", 0.5)), mvol)) if music else
             ("[0:a]atrim=0:%.2f,volume=%.3f[o]"
              % (total, float(a.get("room_tone_level", 0.5)))),
             "-map", "[o]", "-ar", "48000", bedded],
            capture_output=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if rc.returncode == 0 and os.path.isfile(bedded):
            music, mvol = bedded, 1.0
        else:
            tone_note = ""
    if bleeds:
        mixed = os.path.join(tmp, "mg_withbleed_%d.wav" % tag)
        ins, labs = [], []
        for n, (w, _k, _d) in enumerate(bleeds):
            ins += ["-i", w]
            labs.append("[%d:a]volume=%.2f[b%d]" % (n, float(a.get("bleed_level", 0.9)), n))
        base_i = len(bleeds)
        if music:
            ins += ["-i", os.path.abspath(music)]
            labs.append("[%d:a]volume=%.3f[bed]" % (base_i, mvol))
        srcs_ = "".join("[b%d]" % n for n in range(len(bleeds))) + ("[bed]" if music else "")
        graph = ";".join(labs) + ";%samix=inputs=%d:duration=longest:normalize=0[o]" % (
            srcs_, len(bleeds) + (1 if music else 0))
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error"] + ins +
                           ["-filter_complex", graph, "-map", "[o]", "-ar", "48000", mixed],
                           capture_output=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and os.path.isfile(mixed):
            music, mvol = mixed, 1.0
        else:
            bleeds = []
    if music:
        build["music"], build["music_volume"] = music, mvol
    msg = t_build(build)

    extra = ["%d photo(s) in %s" % (len(photos), order_said)]
    if per_list:
        extra.append("pacing given per photo, %.1fs to %.1fs - the holds are not even, "
                     "which is what stops it reading as a slideshow"
                     % (min(per_list), max(per_list)))
    if twins and not per_list:
        extra.append("%d near-repeat(s) held %.1fs instead of %.1fs so the pair reads "
                     "as one beat rather than a stutter" % (len(twins), twin_hold, per))
    if gammas:
        extra.append("exposure evened across %d photo(s) - they spanned %.0f of 255 "
                     "brightness, which flickers as they cut"
                     % (len(gammas), exp_spread))
    if head_f or tail_f:
        extra.append("%d moment(s) lifted from %d clip(s) by where the room got loudest"
                     % (len(head_f) + len(tail_f), len(clips)))
    if sound_at and music:
        extra.append("music ducked under %d of them" % len(sound_at))
    if bleeds:
        extra.append("their own sound carried across %d cut(s) (%s) so the room does "
                     "not stop dead when the photographs start"
                     % (len(bleeds), ", ".join("%s %.1fs" % (k, d) for _w, k, d in bleeds)))
    if beat_note:
        extra.append(beat_note)
    if _drift > 0.01 and _warm:
        extra.append("colour drifts warmer across the film (%.2f to %.2f), so the "
                     "ending feels warmer than the opening without looking graded"
                     % (_warm * (1 - _drift), _warm * (1 + _drift)))
    if tone_note:
        extra.append(tone_note)
    if sfx and not a.get("sfx"):
        extra.append("%d quiet accent(s) placed - I cannot hear these, only "
                     "confirm they sit well below the music" % len(sfx))
    return msg + "\n  " + "; ".join(extra) + "."


def t_slideshow(a):
    """Build a clip out of stills, each drifting slowly so nothing sits dead."""
    images = a.get("images") or []
    if not isinstance(images, list) or len(images) < 1:
        raise ToolError("Give 'images': a list of photo files.")
    paths = [check_input(p, "image") for p in images]
    per = float(a.get("seconds_each", 2.5))
    if per < 0.4:
        raise ToolError("seconds_each must be at least 0.4.")
    w = int(a.get("width", 1080))
    h = int(a.get("height", 1920))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps", 30))
    move = float(a.get("move", 0.08))
    fade = float(a.get("transition", 0.5))

    tmp = _tmpdir()
    clips = []
    for i, p in enumerate(paths):
        frames = max(2, int(round(per * fps)))
        # Alternate the drift direction so a run of stills does not pulse in one rhythm.
        zi = "min(1+%0.6f*on/%d,%0.4f)" % (move, frames, 1 + move)
        zo = "max(%0.4f-%0.6f*on/%d,1.0)" % (1 + move, move, frames)
        z = zi if i % 2 == 0 else zo
        seg = os.path.join(tmp, "sl%02d_%d.mp4" % (i, os.getpid()))
        ffmpeg_run(["-loop", "1", "-framerate", "%d" % fps, "-t", "%.3f" % per, "-i", p,
                    "-vf", "scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
                           "crop=%d:%d,scale=%d:%d,zoompan=z='%s':d=1:x='iw/2-(iw/zoom/2)':"
                           "y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%d,setsar=1"
                    % (w, h, w, h, w * 2, h * 2, z, w, h, fps)] +
                   FAST_ENC + ["-an", seg])
        clips.append(seg)

    out = make_output(paths[0], "slideshow", a.get("output"), ".mp4")
    if len(clips) == 1 or fade <= 0.01:
        t_merge({"paths": clips, "output": out})
        how = "hard cuts"
    else:
        t_join_smooth({"paths": clips, "transition": a.get("transition_style") or "fade",
                       "duration": fade, "fps": fps, "output": out})
        how = "%.2fs dissolves" % fade
    return done(out, "Slideshow: %d still(s), %.1fs each, %s, slow drift on every frame.\n"
                     "  Length %.2fs at %dx%d." % (len(paths), per, how,
                                                   video_duration_of(out), w, h))


# ---------------------------------------------------------------- review
def _frame_gray(src, at, width=480):
    """One frame as a greyscale numpy array."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % max(0.0, at), "-i", src,
                          "-frames:v", "1", "-vf", "scale=%d:-2,format=gray" % width,
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True).stdout
    if not raw:
        return None
    h = len(raw) // width
    if h < 4:
        return None
    return np.frombuffer(raw[:width * h], dtype=np.uint8).reshape(h, width)


def _caption_box(gray, band=0.40):
    """Where the burned-in caption sits, as fractions of the frame.

    Captions are near-white text carrying a near-black outline, so what gets looked for
    is a very light pixel with something very dark within a few pixels of it, in rows
    dense enough to be a line of text. All three parts are needed: the looser thresholds
    this started with (>205 beside <85, any density) reported a caption spanning 24-87%
    of an extreme close-up that had no caption on it at all - lit teeth against a dark
    mouth pass a brightness test perfectly well.
    """
    import numpy as np
    try:
        from scipy.ndimage import minimum_filter
    except ImportError:
        return None
    h, w = gray.shape
    top = int(h * (1.0 - band))
    strip = gray[top:, :]
    ink = (strip > 235) & (minimum_filter(strip, size=7) < 60)
    rows = np.where(ink.sum(axis=1) >= max(3, int(w * 0.015)))[0]
    if not len(rows):
        return None
    keep = ink[rows.min():rows.max() + 1, :]
    ys, xs = np.nonzero(keep)
    if xs.size < 40:
        return None
    # Percentiles, not the outermost lit pixel. A handful of stray specks - a catchlight
    # in an eye, a rim on a shoulder - sat 65% of the frame away from the text and
    # stretched the measured box from 75% to 98% wide, which read as an overflow that
    # was not there.
    x0, x1 = np.percentile(xs, [0.5, 99.5])
    return (x0 / float(w), (top + rows.min()) / float(h),
            (x1 + 1) / float(w), (top + rows.max() + 1) / float(h))


def _review_cuts(src, min_shot=0.45):
    """Shot changes, with detection artefacts thrown out.

    scdet on graded, grainy footage reports extra cuts a fraction of a second apart -
    twelve shots where there are seven - and a short dissolve reads as two. Nothing a
    threshold alone fixes: lower and it invents cuts, higher and it misses real ones.
    Rejecting anything that would make an impossibly short shot does fix it.
    """
    kept = []
    for c in _scene_cuts(src, 14.0):
        if not kept or c - kept[-1] >= min_shot:
            kept.append(c)
    return kept


def _srt_cues(path):
    """Cues with numeric times and their LINE BREAKS intact.

    parse_srt joins the body with spaces, which is fine for burning and useless here -
    how many lines a cue has is one of the things worth checking.
    """
    out = []
    text = io.open(path, encoding="utf-8-sig", errors="replace").read()
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        tm = next((l for l in lines if "-->" in l), None)
        if not tm:
            continue

        def secs(s):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", s.strip())
            if not m:
                return None
            return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 +
                    int(m.group(3)) + int(m.group(4)) / 1000.0)

        a, _, b = tm.partition("-->")
        s, e = secs(a), secs(b)
        body = lines[lines.index(tm) + 1:]
        if s is None or e is None or not body:
            continue
        out.append({"start": s, "end": e, "lines": body})
    return out


def t_review(a):
    """Look at the finished video and say what is WRONG with it.

    video_check measures the technical side - levels, black bars, clipping - and never
    looks at the picture. Every caption bug in this project was found by grabbing frames
    by hand and staring at them, so this does that part: samples the moments that matter,
    tiles them to look at, and flags the faults that can be measured.
    """
    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    limit = max(2, min(16, int(a.get("frames", 12))))
    max_lines = int(a.get("max_lines", 2))
    edge = float(a.get("edge_margin", 0.045))
    safe_bottom = float(a.get("safe_bottom", 0.90))
    problems, notes = [], []

    # --- where to look -------------------------------------------------------
    marks, cues = [], []
    subs = a.get("subtitles")
    if subs:
        cues = _srt_cues(check_input(subs, "subtitle"))
        marks = [((c["start"] + c["end"]) / 2.0, "cue %d" % (i + 1))
                 for i, c in enumerate(cues)]
    elif has_audio(src):
        try:
            words, _lang = _word_timings(src, a.get("language") or "auto",
                                         a.get("model") or "large-v3")
            spans = merge_spans([(w["s"], w["e"]) for w in words], gap=0.45)
            marks = [((s + e) / 2.0, "speech %d" % (i + 1))
                     for i, (s, e) in enumerate(spans)]
        except ToolError:
            words = []
    if not marks:
        step = total / (limit + 1.0)
        marks = [(step * (i + 1), "%.1fs" % (step * (i + 1))) for i in range(limit)]
    if len(marks) > limit:                       # thin out evenly, keep first and last
        keep = [int(round(i * (len(marks) - 1) / float(limit - 1))) for i in range(limit)]
        marks = [marks[i] for i in sorted(set(keep))]

    # --- caption geometry, measured off the actual pixels --------------------
    tmp = _tmpdir()
    shots, boxed = [], 0
    for i, (t, label) in enumerate(marks):
        png = os.path.join(tmp, "rv%02d_%d.png" % (i, os.getpid()))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % t, "-i", src,
                        "-frames:v", "1", "-vf", "scale=360:-2", png], capture_output=True)
        if os.path.isfile(png):
            shots.append(png)
        g = _frame_gray(src, t)
        if g is None:
            continue
        box = _caption_box(g)
        if not box:
            continue
        boxed += 1
        x0, y0, x1, y1 = box
        if x0 < edge or x1 > 1.0 - edge:
            problems.append("%s at %.2fs: caption reaches %.1f%%-%.1f%% across the frame - "
                            "it is running into the edge." % (label, t, x0 * 100, x1 * 100))
        if y1 > safe_bottom:
            problems.append("%s at %.2fs: caption bottom sits at %.0f%% - under the "
                            "TikTok/Reels interface." % (label, t, y1 * 100))

    # --- what the cue text itself says ---------------------------------------
    font = a.get("font") or "Tahoma"
    fits = (SUBTITLE_FONTS.get(font) or {}).get("fits", 19)
    for i, c in enumerate(cues):
        lines = c["lines"]
        if len(lines) > max_lines:
            problems.append("cue %d (%.2fs): %d lines - more than %d reads as a wall of text."
                            % (i + 1, c["start"], len(lines), max_lines))
        for l in lines:
            if _visible_len(l) > fits:
                problems.append("cue %d (%.2fs): \"%s\" is %d wide, past the %d that %s fits."
                                % (i + 1, c["start"], l[:22], _visible_len(l), fits, font))

    # --- cuts against speech --------------------------------------------------
    cuts = [c for c in _review_cuts(src) if 0.2 < c < total - 0.2]
    if has_audio(src):
        try:
            words, _l = _word_timings(src, a.get("language") or "auto",
                                      a.get("model") or "large-v3")
            for c in cuts:
                hit = next((w for w in words if w["s"] + 0.04 < c < w["e"] - 0.04), None)
                if hit:
                    problems.append("cut at %.2fs lands inside the word \"%s\" (%.2f-%.2fs)."
                                    % (c, hit["t"], hit["s"], hit["e"]))
        except ToolError:
            pass

    # --- shot lengths ---------------------------------------------------------
    bounds = [0.0] + cuts + [total]
    for i in range(len(bounds) - 1):
        span = bounds[i + 1] - bounds[i]
        if span > 9.0:
            notes.append("shot %d runs %.1fs with no cut - long enough to feel static."
                         % (i + 1, span))

    # --- contact sheet --------------------------------------------------------
    sheet = None
    if shots:
        cols = min(4, len(shots))
        rows = (len(shots) + cols - 1) // cols
        layout = "|".join(("0_0" if i == 0 else
                           "%s_%s" % ("+".join("w%d" % k for k in range(i % cols)) or "0",
                                      "+".join("h%d" % (k * cols) for k in range(i // cols)) or "0"))
                          for i in range(len(shots)))
        sheet = os.path.join(tmp, "review_%d.jpg" % os.getpid())
        args = []
        for p in shots:
            args += ["-i", p]
        r = subprocess.run(["ffmpeg", "-y", "-v", "error"] + args + ["-filter_complex",
                           "".join("[%d:v]" % i for i in range(len(shots))) +
                           "xstack=inputs=%d:layout=%s:fill=black" % (len(shots), layout),
                           "-q:v", "3", sheet], capture_output=True, text=True)
        if r.returncode or not os.path.isfile(sheet):
            sheet = None

    head = ["Review of %s" % os.path.basename(src),
            "  %.2fs, %d shot(s), %d sample(s)%s"
            % (total, len(bounds) - 1, len(marks),
               ", caption ink found in %d" % boxed if boxed else ", no burned-in captions seen")]
    if problems:
        head.append("")
        head.append("PROBLEMS (%d):" % len(problems))
        head += ["  - " + p for p in problems]
    else:
        head.append("")
        head.append("Nothing measurable is wrong. Look at the frames anyway - taste is not "
                    "something this can check.")
    if notes:
        head.append("")
        head.append("Worth a look:")
        head += ["  - " + n for n in notes]

    text = "\n".join(head)
    if not sheet:
        return text
    data = base64.b64encode(io.open(sheet, "rb").read()).decode("ascii")
    return [{"type": "text", "text": text},
            {"type": "image", "data": data, "mimeType": "image/jpeg"}]


# ---------------------------------------------------------------- history
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.jsonl")


def _log_edit(tool, args, text):
    """Record what a tool did, so a file can be traced back to what made it.

    Outputs land in a folder as a pile of similar names with no record of which came
    from what. One line per call fixes that at almost no cost.
    """
    try:
        out = None
        m = re.search(r"Done -> (.+?)  \(", text or "")
        if m:
            out = m.group(1).strip()
        elif isinstance(args.get("output"), str):
            out = args["output"]
        ins = []
        for k, v in (args or {}).items():
            if k == "output":
                continue
            for cand in (v if isinstance(v, list) else [v]):
                # Testing for os.path.sep missed every path written with forward slashes,
                # which Windows accepts perfectly well - so ask the filesystem instead.
                if isinstance(cand, str) and len(cand) > 3 and os.path.isfile(cand):
                    ins.append(os.path.abspath(cand))
        out = os.path.abspath(out) if out else None
        if not out and not ins:
            return
        note = (text or "").strip().splitlines()
        entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": tool,
                 "in": ins[:6], "out": out,
                 "note": (note[1][:160] if len(note) > 1 else (note[0][:160] if note else ""))}
        with io.open(HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass                      # history is a convenience; never break a tool over it


def _read_history():
    if not os.path.isfile(HISTORY_FILE):
        return []
    rows = []
    for line in io.open(HISTORY_FILE, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def t_edit_history(a):
    """What was done, and what a given file was made from."""
    rows = _read_history()
    if not rows:
        return ("No history yet. It starts recording from the next edit.\n"
                "Kept in %s" % HISTORY_FILE)

    path = a.get("path")
    if path:
        target = os.path.abspath(path)
        chain, seen = [], set()
        cur = target
        for _ in range(24):
            made = next((r for r in reversed(rows)
                         if r.get("out") and os.path.abspath(r["out"]) == cur), None)
            if not made or cur in seen:
                break
            seen.add(cur)
            chain.append(made)
            src = [i for i in made.get("in", []) if os.path.abspath(i) != cur]
            if not src:
                break
            cur = os.path.abspath(src[0])
        if not chain:
            return ("Nothing recorded for %s.\n"
                    "Either it predates the history, or it was not made by this editor."
                    % os.path.basename(path))
        out = ["How %s was made, most recent step first:" % os.path.basename(path), ""]
        for i, r in enumerate(chain):
            out.append("  %d. %-22s %s" % (i + 1, r["tool"], r["t"]))
            if r.get("note"):
                out.append("     %s" % r["note"])
            for s in r.get("in", []):
                out.append("     from  %s" % os.path.basename(s))
        return "\n".join(out)

    n = max(1, min(60, int(a.get("limit", 20))))
    tail = rows[-n:]
    out = ["Last %d edit(s) - newest at the bottom:" % len(tail), ""]
    for r in tail:
        ins = ", ".join(os.path.basename(i) for i in r.get("in", [])[:2]) or "-"
        out.append("  %s  %-22s %s" % (r["t"][11:], r["tool"],
                                       os.path.basename(r["out"]) if r.get("out") else "(no file)"))
        out.append("      from %s" % ins)
    out.append("")
    out.append("Full record: %s" % HISTORY_FILE)
    return "\n".join(out)


# ---------------------------------------------------------------- surgery
_DT_SEQ = [0]


def drawtext_of(text, tmp, **opts):
    r"""Build a drawtext filter that renders the text EXACTLY as given.

    Escaping text inline is a losing game. drawtext reads `%{...}` as an expansion, so a
    plain "50% off today" silently rendered NOTHING at all - not mangled, absent - and a
    backslash disappeared without trace. Hand-escaping also meant swapping real
    apostrophes for curly ones to dodge the quote character.

    Writing the text to a file and pointing `textfile=` at it removes the whole problem:
    ffmpeg reads it verbatim, and `expansion=none` stops it looking for directives. The
    file needs a bare name with cwd set, since a Windows path in a filter argument breaks
    the graph on the drive-letter colon.
    """
    _DT_SEQ[0] += 1
    name = "dt_%d_%d.txt" % (os.getpid(), _DT_SEQ[0])
    with io.open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    parts = ["drawtext=textfile=%s" % name, "expansion=none"]
    for k, v in opts.items():
        if v is None:
            continue
        parts.append("%s=%s" % (k, v))
    return ":".join(parts)


def esc_expr(expr):
    r"""Escape an expression for use as a filter OPTION VALUE.

    Inside a filter argument ffmpeg reads ',' as the start of the next filter and ':' as
    the next option, so every one that belongs to the expression - `if(lt(t,3),..)`, a
    Windows font path - has to be backslashed or the graph fails to parse. This is the
    single most common way a working filter string breaks.
    """
    return str(expr).replace("\\", "\\\\").replace(",", "\\,").replace(":", "\\:")


def _spans_arg(a, total, field="ranges"):
    """Parse [{start,end}, ...] and check it against the clip."""
    raw = a.get(field) or []
    if not isinstance(raw, list) or not raw:
        raise ToolError("Give '%s': a list of {start, end}." % field)
    spans = []
    for r in raw:
        s = parse_time(r.get("start"), "start")
        e = parse_time(r.get("end"), "end")
        if s is None or e is None or e <= s:
            raise ToolError("Each range needs a start and a later end.")
        spans.append((max(0.0, s), min(total, e)))
    return merge_spans(sorted(spans), gap=0.0)


def t_cut_out(a):
    """Remove a stretch from the middle and close the gap.

    The everyday edit - lifting a fluffed line or a pause - which otherwise takes two
    trims and a merge.
    """
    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    spans = _spans_arg(a, total)
    keep, at = [], 0.0
    for s, e in spans:
        if s - at > 0.05:
            keep.append((at, s))
        at = e
    if total - at > 0.05:
        keep.append((at, total))
    if not keep:
        raise ToolError("That would remove the whole clip.")

    tmp = _tmpdir()
    parts = []
    for i, (s, e) in enumerate(keep):
        p = os.path.join(tmp, "co%02d_%d.mp4" % (i, os.getpid()))
        ffmpeg_run(["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % (e - s)] + FAST_ENC +
                   (AUDIO_ENC if has_audio(src) else ["-an"]) + [p])
        parts.append(p)

    out = make_output(src, "cutout", a.get("output"), ".mp4")
    fade = float(a.get("transition_duration", 0))
    if len(parts) == 1:
        ffmpeg_run(["-i", parts[0], "-c", "copy", out])
    elif fade > 0.01:
        t_join_smooth({"paths": parts, "transition": a.get("transition") or "fade",
                       "duration": fade, "output": out})
    else:
        t_merge({"paths": parts, "output": out})
    gone = sum(e - s for s, e in spans)
    return done(out, "Removed %d stretch(es), %.2fs in total. %.2fs -> %.2fs."
                % (len(spans), gone, total, video_duration_of(out)))


def t_split(a):
    """Chop one video into several files - for a multi-part post, or to hand out rushes."""
    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    every = a.get("every")
    at = a.get("at")
    parts = a.get("parts")
    if at:
        marks = sorted(set(parse_time(t, "at") for t in at))
        bounds = [0.0] + [m for m in marks if 0 < m < total] + [total]
    elif every:
        step = float(every)
        if step < 0.5:
            raise ToolError("'every' must be at least 0.5 seconds.")
        bounds = list(_frange(0.0, total, step)) + [total]
    elif parts:
        n = max(2, int(parts))
        step = total / n
        bounds = [i * step for i in range(n)] + [total]
    else:
        raise ToolError("Give 'every' (seconds), 'parts' (a count), or 'at' (a list of times).")
    bounds = sorted(set(round(b, 3) for b in bounds))

    folder = a.get("folder") or os.path.join(os.path.dirname(src), "split")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0][:40]
    made = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e - s < 0.2:
            continue
        p = os.path.join(folder, "%s_part%02d.mp4" % (stem, i + 1))
        ffmpeg_run(["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % (e - s)] + VIDEO_ENC +
                   (AUDIO_ENC if has_audio(src) else ["-an"]) + [p])
        made.append((os.path.basename(p), e - s))
    if not made:
        raise ToolError("Nothing long enough to make a part.")
    rows = "\n".join("    %-40s %5.2fs" % (n, d) for n, d in made)
    return "Split into %d file(s) in %s:\n%s" % (len(made), folder, rows)


def _frange(start, stop, step):
    v = start
    while v < stop - 0.05:
        yield v
        v += step


def t_loop(a):
    """Repeat a clip, or bounce it forwards and back."""
    src = check_input(a.get("path"), "video")
    times = max(2, int(a.get("times", 2)))
    boomerang = bool(a.get("boomerang"))
    tmp = _tmpdir()
    out = make_output(src, "boomerang" if boomerang else "loop", a.get("output"), ".mp4")

    if boomerang:
        # Forward then back. The reverse drops its last frame so the turnaround does not
        # show the same picture twice.
        back = os.path.join(tmp, "bm_%d.mp4" % os.getpid())
        keep = bool(a.get("keep_audio", False))
        fc = "[0:v]reverse,trim=start_frame=1,setpts=PTS-STARTPTS[v]"
        maps = ["-map", "[v]"]
        if keep and has_audio(src):
            fc += ";[0:a]areverse[aud]"
            maps += ["-map", "[aud]"]
        ffmpeg_run(["-i", src, "-filter_complex", fc] + maps + FAST_ENC +
                   (AUDIO_ENC if (keep and has_audio(src)) else ["-an"]) + [back])
        seq = [src, back] * (times // 2 if times > 2 else 1)
    else:
        seq = [src] * times

    t_merge({"paths": seq, "output": out})
    return done(out, "%s %d time(s). %.2fs -> %.2fs."
                % ("Bounced" if boomerang else "Looped", times,
                   video_duration_of(src), video_duration_of(out)))


def t_punch_in(a):
    """Push into part of the frame and ease back out - emphasis without a second camera.

    A hard cut to a tighter framing reads as a jump; easing in over a few tenths reads
    as a move. Done by hand on a reaction shot before it was worth a tool.
    """
    src = check_input(a.get("path"), "video")
    total = video_duration_of(src)
    at = parse_time(a.get("at"), "at")
    if at is None or not 0 <= at < total:
        raise ToolError("'at' must be inside the clip (0 - %.2fs)." % total)
    hold = float(a.get("hold", 1.5))
    ramp = float(a.get("ramp", 0.4))
    zoom = float(a.get("zoom", 1.25))
    if not 1.01 <= zoom <= 3.0:
        raise ToolError("zoom must be between 1.01 and 3.")
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    # Where to push toward, 0-1 across the frame. Default just above centre, which is
    # where a face sits in a vertical frame.
    fx = float(a.get("focus_x", 0.5))
    fy = float(a.get("focus_y", 0.42))

    t0, t1 = at, at + ramp
    t2, t3 = at + ramp + hold, at + ramp + hold + ramp
    # A smoothstep either side, so it accelerates and settles instead of stepping.
    z = ("if(lt(t,%f),1,"
         "if(lt(t,%f),1+%f*(3*pow((t-%f)/%f,2)-2*pow((t-%f)/%f,3)),"
         "if(lt(t,%f),%f,"
         "if(lt(t,%f),%f-%f*(3*pow((t-%f)/%f,2)-2*pow((t-%f)/%f,3)),1))))"
         % (t0, t1, zoom - 1, t0, ramp, t0, ramp, t2, zoom, t3, zoom, zoom - 1,
            t2, ramp, t2, ramp))
    ez = esc_expr(z)
    vf = ("crop=w=iw/(%s):h=ih/(%s):x=(iw-iw/(%s))*%.4f:y=(ih-ih/(%s))*%.4f,"
          "scale=%d:%d:flags=lanczos,setsar=1" % (ez, ez, ez, fx, ez, fy, w, h))
    out = make_output(src, "punch", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-vf", vf] + VIDEO_ENC +
               (["-c:a", "copy"] if has_audio(src) else ["-an"]) + [out])
    return done(out, "Punched in to %.2fx at %.2fs, held %.1fs, eased over %.2fs each side."
                % (zoom, at, hold, ramp))


# ---------------------------------------------------------------- sound surgery
def t_censor(a):
    """Bleep or silence words - a crude line without reshooting or re-recording."""
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This clip has no audio to censor.")
    total = duration_of(src)
    spans = _spans_arg(a, total)
    mode = (a.get("mode") or "beep").lower()
    if mode not in ("beep", "silence"):
        raise ToolError("mode must be 'beep' or 'silence'.")
    freq = float(a.get("frequency", 1000))
    level = float(a.get("level", 0.25))

    gate = "+".join("between(t,%.3f,%.3f)" % (s, e) for s, e in spans)
    fc = "[0:a]volume=0:enable='%s'[clean]" % esc_expr(gate)
    if mode == "beep":
        # The tone runs the length of the clip and is gated to the same windows, so it
        # starts and stops exactly where the speech was removed.
        tone = "%.4f*sin(2*PI*%.1f*t)*(%s)" % (level, freq, gate)
        fc += (";aevalsrc=%s:s=48000:d=%.3f:c=stereo[tone];"
               "[clean][tone]amix=inputs=2:duration=first:normalize=0[a]"
               % (esc_expr(tone), total))
    else:
        fc += ";[clean]anull[a]"
    out = make_output(src, "censored", a.get("output"), ".mp4")
    ffmpeg_run(["-i", src, "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                "-c:v", "copy"] + AUDIO_ENC + [out])
    covered = sum(e - s for s, e in spans)
    return done(out, "%s %d stretch(es), %.2fs of audio in total.%s"
                % ("Bleeped" if mode == "beep" else "Silenced", len(spans), covered,
                   "" if mode == "beep" else " Nothing was put in its place."))


def t_replace_audio(a):
    """Put a separately recorded track onto the picture, with an offset to line it up."""
    src = check_input(a.get("path"), "video")
    aud = check_input(a.get("audio"), "audio")
    offset = float(a.get("offset", 0))
    keep = float(a.get("keep_original", 0))
    out = make_output(src, "dub", a.get("output"), ".mp4")

    if offset >= 0:
        lay = "[1:a]adelay=%d|%d" % (int(offset * 1000), int(offset * 1000))
    else:
        lay = "[1:a]atrim=start=%.3f,asetpts=PTS-STARTPTS" % (-offset)
    if keep > 0.001 and has_audio(src):
        fc = ("%s[new];[0:a]volume=%.3f[old];"
              "[old][new]amix=inputs=2:duration=first:normalize=0[a]" % (lay, keep))
        note = "original kept underneath at %.0f%%" % (keep * 100)
    else:
        fc = "%s[a]" % lay
        note = "original replaced"
    ffmpeg_run(["-i", src, "-i", aud, "-filter_complex", fc,
                "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + AUDIO_ENC +
               ["-shortest", out])
    return done(out, "Audio swapped in, offset %+.3fs - %s." % (offset, note))


# ---------------------------------------------------------------- graphics & review
def t_lower_third(a):
    """The name bar that slides in at the bottom - a product, a price, a person."""
    src = check_input(a.get("path"), "video")
    title = (a.get("title") or "").strip()
    if not title:
        raise ToolError("Give 'title': the main line of the bar.")
    sub = (a.get("subtitle") or "").strip()
    total = video_duration_of(src)
    at = parse_time(a.get("at"), "at") or 0.6
    dur = float(a.get("duration", 3.0))
    end = min(total, at + dur)
    info = probe(src)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    font = a.get("font") or "TH Chakra Petch"
    size = int(a.get("font_size", round(h * 0.030)))
    accent = _ass_colour(a.get("accent"), "&H00E2AB6C")
    x = int(w * 0.08)
    y = int(h * float(a.get("y", 0.74)))

    head = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % w, "PlayResY: %d" % h,
            "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: T,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,"
            "1,3,2,1,0,0,0,1" % (font, size),
            "Style: S,%s,%d,%s,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,"
            "1,3,2,1,0,0,0,1" % (font, int(size * 0.62), accent),
            "", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def ts(t):
        m, s = divmod(max(0.0, t), 60)
        hh, m = divmod(int(m), 60)
        return "%d:%02d:%05.2f" % (hh, m, s)

    # Slide in from the left and fade, rather than appearing - movement is what makes a
    # bar read as a graphic instead of a caption.
    slide = int(w * 0.05)
    ev = ["Dialogue: 0,%s,%s,T,,0,0,0,,{\\move(%d,%d,%d,%d,0,260)\\fad(220,260)}%s"
          % (ts(at), ts(end), x - slide, y, x, y, title)]
    if sub:
        ev.append("Dialogue: 0,%s,%s,S,,0,0,0,,{\\move(%d,%d,%d,%d,0,320)\\fad(300,260)}%s"
                  % (ts(at + 0.12), ts(end), x - slide, y + int(size * 1.15),
                     x, y + int(size * 1.15), sub))

    tmp = _tmpdir()
    ass = os.path.join(tmp, "lt_%d.ass" % os.getpid())
    with io.open(ass, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(head + ev) + "\n")
    out = make_output(src, "lower3", a.get("output"), ".mp4")
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src),
                        "-vf", subtitles_arg(os.path.basename(ass)), "-c:a", "copy"] +
                       VIDEO_ENC + [os.path.abspath(out)],
                       cwd=os.path.dirname(ass), capture_output=True, text=True)
    try:
        os.remove(ass)
    except OSError:
        pass
    if p.returncode:
        raise ToolError("Lower third failed:\n" + (p.stderr or "").strip()[-400:])
    return done(out, "Lower third from %.2fs for %.1fs: %s%s"
                % (at, end - at, title, (" / " + sub) if sub else ""))


def t_compare(a):
    """Put two versions side by side, labelled, so a change can actually be judged."""
    paths = a.get("paths") or []
    if not isinstance(paths, list) or len(paths) != 2:
        raise ToolError("Give 'paths': exactly two videos to compare.")
    left, right = [check_input(p, "video") for p in paths]
    labels = a.get("labels") or [os.path.basename(left)[:24], os.path.basename(right)[:24]]
    stack = (a.get("layout") or "auto").lower()
    info = probe(left)
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    if stack == "auto":
        # Two 9:16 clips belong beside each other; stacking them gives a 9:32 sliver.
        # Two landscape clips are the other way round.
        stack = "horizontal" if h > w else "vertical"

    # Each panel keeps the SOURCE shape at full size and the canvas grows to fit, rather
    # than halving each clip inside the original frame - that letterboxed away most of
    # the picture. The result is then brought back under a sane maximum.
    limit = int(a.get("max_side", 1920))
    if stack == "horizontal":
        outw, outh = w * 2, h
        join = "hstack"
    else:
        outw, outh = w, h * 2
        join = "vstack"
    shrink = min(1.0, float(limit) / max(outw, outh))
    geom = (max(2, int(w * shrink) // 2 * 2), max(2, int(h * shrink) // 2 * 2))

    fontfile = None
    for cand in ("C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arial.ttf"):
        if os.path.isfile(cand):
            fontfile = cand
            break
    fs = max(16, int(min(geom) * 0.055))

    def side(i, text):
        base = ("[%d:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black,setsar=1" % (i, geom[0], geom[1],
                                                                  geom[0], geom[1]))
        if fontfile:
            # The drive letter's colon would otherwise be read as the next option.
            safe = str(text).replace("\\", "").replace("'", "").replace(":", " ")
            base += (",drawtext=fontfile='%s':text='%s':x=(w-text_w)/2:y=%d:fontsize=%d:"
                     "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=%d"
                     % (esc_expr(fontfile), safe, int(geom[1] * 0.03), fs, max(6, fs // 3)))
        return base + "[s%d]" % i

    fc = "%s;%s;[s0][s1]%s=inputs=2[v]" % (side(0, str(labels[0])), side(1, str(labels[1])),
                                           join)
    audio = (a.get("audio") or "left").lower()
    maps, aenc = ["-map", "[v]"], ["-an"]
    if audio == "left" and has_audio(left):
        maps += ["-map", "0:a"]
        aenc = AUDIO_ENC
    elif audio == "right" and has_audio(right):
        maps += ["-map", "1:a"]
        aenc = AUDIO_ENC

    out = make_output(left, "compare", a.get("output"), ".mp4")
    ffmpeg_run(["-i", left, "-i", right, "-filter_complex", fc] + maps +
               VIDEO_ENC + aenc + ["-shortest", out])
    return done(out, "Compared %s at %dx%d, each panel %dx%d%s. Audio from the %s."
                % (stack, geom[0] * (2 if join == "hstack" else 1),
                   geom[1] * (1 if join == "hstack" else 2), geom[0], geom[1],
                   "" if fontfile else " (no font found, so no labels drawn)", audio))


# ---------------------------------------------------------------- one pass
def t_build(a):
    """Join, grade, caption, score and level in ONE pass over the footage.

    Done as separate tools, each stage decodes the whole cut, encodes it again and
    writes twenty-odd megabytes that the next stage immediately reads back. Every
    one of those is a video filter, so they chain: measured on a real 25s ad, three
    passes took 44.8s and the same work fused took 26.3s.

    Loudness still needs measuring before it can be corrected, but that is an
    AUDIO-only pass - a second or two - rather than another trip through the video.
    """
    paths = a.get("paths") or []
    if len(paths) < 1:
        raise ToolError("Give 'paths': the prepared pieces, in play order.")
    srcs = [check_input(p, "video") for p in paths]

    trans = a.get("transition") or "fade"
    if trans not in TRANSITIONS:
        raise ToolError("transition must be one of: %s" % ", ".join(TRANSITIONS))
    jd = max(float(a.get("duration", 0.08)), 1.0 / 60)
    lead = float(a.get("audio_lead", 0) or 0)
    a_cross = float(a.get("audio_crossfade", 0) or 0) or max(jd, 0.25)

    info = probe(srcs[0])
    vs = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    w, h = int(vs.get("width", 1080)), int(vs.get("height", 1920))
    w -= w % 2
    h -= h % 2
    fps = int(a.get("fps") or round(fps_of(srcs[0])) or 30)
    durs = [video_duration_of(s) for s in srcs]
    has_snd = all(has_audio(s) for s in srcs)

    tmp = _tmpdir()
    parts, extra_inputs = [], []

    # --- picture: scale, join, grade, caption ---------------------------------
    for i in range(len(srcs)):
        parts.append("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                     "setsar=1,fps=%d,format=yuv420p[v%d]" % (i, w, h, w, h, fps, i))
    cur, total = "[v0]", durs[0]
    v_at = [0.0]
    for i in range(1, len(srcs)):
        out_l = "[vx%d]" % i
        parts.append("%s[v%d]xfade=transition=%s:duration=%.3f:offset=%.3f%s"
                     % (cur, i, trans, jd, max(0.0, total - jd), out_l))
        v_at.append(total - jd)
        total = total + durs[i] - jd
        cur = out_l

    vchain = []
    grain = float(a.get("grain", 0) or 0)
    if grain > 0:
        vchain.append("noise=alls=%d:allf=t+u" % max(1, int(round(grain * 12))))
    product = (a.get("product_colour") or "").strip().lower()
    if product:
        band = {"blue": "blues", "cyan": "cyans", "red": "reds", "green": "greens",
                "yellow": "yellows", "magenta": "magentas"}.get(product)
        if not band:
            raise ToolError("product_colour must be blue, cyan, red, green, yellow or magenta.")
        lift = float(a.get("product_lift", 0.14))
        shift = {"blues": "%.3f 0 -%.3f 0", "cyans": "%.3f 0 -%.3f 0",
                 "reds": "-%.3f %.3f 0 0", "greens": "-%.3f 0 %.3f 0",
                 "yellows": "0 -%.3f %.3f 0", "magentas": "0 %.3f -%.3f 0"}[band]
        vchain.append("eq=saturation=%.3f" % float(a.get("desaturate", 0.86)))
        vchain.append("selectivecolor=%s=%s" % (band, shift % (lift, lift)))
    elif a.get("desaturate"):
        vchain.append("eq=saturation=%.3f" % float(a["desaturate"]))

    # --- sound: J/L cuts, effects, music --------------------------------------
    # Built before the captions, because the captions need to hear it: the word
    # highlight is locked to the voice on the JOINED dialogue.
    aparts = []
    acur = None
    if has_snd:
        for i in range(len(srcs)):
            aparts.append("[%d:a]aresample=48000,asetpts=N/SR/TB[a%d]" % (i, i))
        if abs(lead) > 0.01:
            mixed = []
            for i in range(len(srcs)):
                start = max(0.0, v_at[i] - (lead if i else 0.0))
                head_f = "afade=t=in:st=0:d=%.3f," % a_cross if i else ""
                tail_f = ("afade=t=out:st=%.3f:d=%.3f," %
                          (max(0.0, durs[i] - a_cross), a_cross)) if i < len(srcs) - 1 else ""
                aparts.append("[a%d]%s%sadelay=%d|%d[am%d]"
                             % (i, head_f, tail_f, int(start * 1000), int(start * 1000), i))
                mixed.append("[am%d]" % i)
            aparts.append("%samix=inputs=%d:duration=longest:normalize=0[abase]"
                         % ("".join(mixed), len(mixed)))
        else:
            acur = "[a0]"
            for i in range(1, len(srcs)):
                aparts.append("%s[a%d]acrossfade=d=%.3f:c1=qsin:c2=qsin[ax%d]"
                             % (acur, i, a_cross, i))
                acur = "[ax%d]" % i
            aparts.append("%sanull[abase]" % acur)
        acur = "[abase]"
        n_dlg = len(aparts)     # the dialogue alone, before effects and music

        n_in = len(srcs)
        sfx = a.get("sfx") or []
        if sfx:
            # A combo is several sounds at offsets around the same beat, so it is
            # flattened into its parts before anything is rendered.
            flat_sfx = []
            for s in sfx:
                name = (s.get("sound") or "").strip()
                at = parse_time(s.get("at"), "at") or 0.0
                gain = float(s.get("gain", 1.0))
                if name in SFX_COMBOS:
                    for sub, off, g in SFX_COMBOS[name]:
                        flat_sfx.append((sub, max(0.0, at + off), gain * g))
                elif name in SFX_LIBRARY or os.path.isfile(name):
                    flat_sfx.append((name, at, gain))
                else:
                    raise ToolError("Unknown sound '%s'. Choose from: %s"
                                    % (name, ", ".join(sorted(SFX_LIBRARY) +
                                                       sorted(SFX_COMBOS))))
            lay = []
            for k, (name, at, gain) in enumerate(flat_sfx):
                wav = os.path.join(tmp, "bsfx_%d_%d.wav" % (os.getpid(), k))
                render_sfx(name, wav, gain)
                extra_inputs.append(wav)
                aparts.append("[%d:a]adelay=%d|%d[sx%d]"
                             % (n_in + len(extra_inputs) - 1, int(at * 1000),
                                int(at * 1000), k))
                lay.append("[sx%d]" % k)
            aparts.append("%s%samix=inputs=%d:duration=first:normalize=0[awsfx]"
                         % (acur, "".join(lay), len(lay) + 1))
            acur = "[awsfx]"

        music = a.get("music")
        if music:
            mp = check_input(music, "music")
            extra_inputs.append(mp)
            idx = n_in + len(extra_inputs) - 1
            aparts.append("[%d:a]aloop=loop=-1:size=2e9,volume=%.3f[bed]"
                         % (idx, float(a.get("music_volume", 0.22))))
            aparts.append("%s[bed]amix=inputs=2:duration=first:normalize=0[awm]" % acur)
            acur = "[awm]"

    # --- captions, timed against the joined dialogue --------------------------
    cues = a.get("captions")
    ass_name = None
    speech_note = ""
    if cues:
        # The glow has to land on the word as it is said. Spreading a cue by character
        # count instead put it an average of 0.31s and a worst of 0.75s off on the
        # MiiMuuD ad - 43 of 45 words more than a frame out.
        #
        # Recognise the JOINED dialogue, once. Recognising the pieces separately is
        # correct but far slower: whisper's per-call overhead dominates a 2-second
        # shot, and seven of them took 160s against 45s for the join.
        timer = None
        if has_snd and a.get("speech_timing", True):
            dlg = os.path.join(tmp, "build_dlg_%d.wav" % os.getpid())
            args = []
            for s_ in srcs:
                args += ["-i", os.path.abspath(s_)]
            script = os.path.join(tmp, "build_dlg_%d.txt" % os.getpid())
            with io.open(script, "w", encoding="utf-8") as fh:
                fh.write(";".join(aparts[:n_dlg]))
            p_dlg = subprocess.run(
                ["ffmpeg", "-y", "-v", "error"] + args +
                ["-filter_complex_script", script, "-map", "[abase]",
                 "-ac", "1", "-ar", "16000", dlg],
                cwd=tmp, capture_output=True, text=True)
            if p_dlg.returncode == 0 and os.path.isfile(dlg):
                try:
                    timer = _word_timings(dlg, a.get("language") or "auto",
                                          a.get("model") or "large-v3", want_map=True)
                except ToolError:
                    speech_note = ("\n  No speech found, so the highlight is spaced by "
                                   "letter count rather than sitting on the words.")
        elif not a.get("speech_timing", True):
            speech_note = ("\n  speech_timing off: the highlight is spaced by letter "
                           "count, about a third of a second off the voice. Drafts only.")
        payload = _cues_from_text(cues, total, timer)
        if payload:
            font = a.get("font") or "Tahoma"
            check_font_for(font, " ".join(
                (c.get("text") or "") for c in cues if isinstance(c, dict)))
            ass_name = "build_%d.ass" % os.getpid()
            with io.open(os.path.join(tmp, ass_name), "w", encoding="utf-8-sig") as fh:
                fh.write(_kinetic_ass_text(a, payload, w, h, font))
            vchain.append(subtitles_arg(ass_name))

    parts.append("%s%s[outv]" % (cur, ",".join(vchain) if vchain else "null"))
    n_vparts = len(parts)   # everything after this is audio, and the loudness
    parts += aparts         # measurement runs on THAT alone - see run(measure_only)

    out = make_output(srcs[0], "build", a.get("output"), ".mp4")

    def run(final_audio, dest, measure_only=False):
        args = []
        for s in srcs:
            args += ["-i", os.path.abspath(s)]
        for x in extra_inputs:
            args += ["-i", os.path.abspath(x)]
        # The measurement must not carry the picture with it. Left in, the graph
        # leaves [outv] unconnected - ffmpeg refuses the whole command, no JSON
        # comes back, and the fallback single pass silently lands a dB off target.
        graph = list(parts[n_vparts:]) if measure_only else list(parts)
        if final_audio:
            graph.append(final_audio)
        script = os.path.join(tmp, "build_%s_%d.txt"
                              % ("m" if measure_only else "v", os.getpid()))
        with io.open(script, "w", encoding="utf-8") as fh:
            fh.write(";".join(graph))
        args += ["-filter_complex_script", script]
        if measure_only:
            args += ["-map", "[aout]", "-vn", "-f", "null", "-"]
        else:
            args += ["-map", "[outv]"]
            if has_snd:
                args += ["-map", "[aout]"] + AUDIO_ENC
            args += VIDEO_ENC + ["-movflags", "+faststart", os.path.abspath(dest)]
        p = subprocess.run(["ffmpeg", "-y", "-v", "info", "-nostats"] + args,
                           cwd=tmp, capture_output=True, text=True)
        return p

    target = float(a.get("target_lufs", -14))
    peak = float(a.get("true_peak", -1.5))
    # loudnorm aims at a TRUE peak, which is an estimate of what the waveform does
    # between samples - so a source that is already squared off at full scale can
    # still put samples on the rail afterwards. A hard ceiling behind it costs
    # nothing and makes the promise in `true_peak` actually hold.
    ceiling = "alimiter=limit=%.4f:attack=5:release=60:level=disabled" \
              % min(0.999, 10.0 ** (peak / 20.0))
    note = ""
    if has_snd:
        # measure on the assembled AUDIO only - seconds, and no video decode
        m = run("%sloudnorm=I=%.1f:TP=%.1f:print_format=json[aout]" % (acur, target, peak),
                None, measure_only=True)
        vals = None
        try:
            blob = m.stderr[m.stderr.rindex("{"):]
            vals = json.loads(blob[:blob.index("}") + 1])
        except (ValueError, KeyError):
            pass
        if vals:
            # Linear mode applies ONE fixed gain across the whole thing, which is
            # what keeps a mix sounding untouched - but it does not limit. A quiet
            # cut needing a big lift will then clip: a family video measured at
            # -18.18 LUFS wanted +4.18 dB and came out slamming 0.0 dBTP. Dynamic
            # mode carries its own true-peak limiter, so hand over to it when the
            # arithmetic says linear cannot land inside the ceiling.
            gain = target - float(vals["input_i"])
            linear = float(vals["input_tp"]) + gain <= peak + 0.1
            # target_offset is not optional: leaving it out landed a finished ad
            # at -15.06 LUFS against a -14.0 target, measured.
            final = ("%sloudnorm=I=%.1f:TP=%.1f:measured_I=%s:measured_TP=%s:"
                     "measured_LRA=%s:measured_thresh=%s:offset=%s%s[aout]"
                     % (acur, target, peak, vals["input_i"], vals["input_tp"],
                        vals["input_lra"], vals["input_thresh"],
                        vals.get("target_offset", "0.0"),
                        ":linear=true" if linear else ""))
            final = final.replace("[aout]", "," + ceiling + "[aout]")
            note = "\n  Loudness measured then corrected in one go (%s -> %.1f LUFS)." \
                   % (vals["input_i"], target)
            if not linear:
                note += ("\n  A flat %+.1f dB lift would have peaked at %.1f dBTP, so the "
                         "levelling rides the loud moments instead."
                         % (gain, float(vals["input_tp"]) + gain))
        else:
            final = "%sloudnorm=I=%.1f:TP=%.1f,%s[aout]" % (acur, target, peak, ceiling)
            note = "\n  Loudness corrected in a single pass - the measurement did not parse."
    else:
        final = None

    p = run(final, out)
    if p.returncode != 0 or not os.path.isfile(out):
        raise ToolError("Build failed:\n" + (p.stderr or "").strip()[-500:])

    bits = ["%d piece(s) joined with %.2fs %s" % (len(srcs), jd, trans)]
    if abs(lead) > 0.01:
        bits.append("%s of %.2fs" % ("J-cut" if lead > 0 else "L-cut", abs(lead)))
    if grain > 0 or product:
        bits.append("graded")
    if ass_name:
        bits.append("%d caption cue(s) burned" % len(a.get("captions") or []))
    if a.get("sfx"):
        bits.append("%d effect(s)" % len(a["sfx"]))
    if a.get("music"):
        bits.append("music under")
    return done(out, "Built in ONE pass: %s.%s%s\n  %.2fs at %dx%d."
                % (", ".join(bits), note, speech_note,
                   video_duration_of(out), w, h))


# ---------------------------------------------------------------- listening
def _spectrum(path, sr=22050, n_fft=2048):
    """Average magnitude per frequency bin, and the per-frame energy envelope."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le",
                          "-ac", "1", "-ar", str(sr), "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    if x.size < n_fft * 4:
        raise ToolError("Too short to analyse.")
    hop = n_fft // 2
    n = 1 + (x.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    win = np.hanning(n_fft).astype("f4")
    mag = np.abs(np.fft.rfft(x[idx] * win, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    return freqs, mag, x


def t_sound_faults(a):
    """The faults a mix engineer listens FOR, measured rather than heard.

    Audio never reaches this model - tool results carry text and pictures, not sound -
    so 'does it sound good' is not answerable here and anything claiming to answer it
    would be invention. What IS answerable is the specific, measurable things that make
    a mix sound wrong: a boxy low-mid pile-up, piercing sibilance, mains hum, hiss in
    the gaps, and dynamics squashed flat. Each is reported with the number behind it so
    the judgement stays yours.
    """
    import numpy as np
    src = check_input(a.get("path"), "video")
    if not has_audio(src):
        raise ToolError("This file has no audio.")
    freqs, mag, x = _spectrum(src)
    avg = mag.mean(axis=0)
    total = float(avg.sum()) or 1.0

    def band(lo, hi):
        m = (freqs >= lo) & (freqs < hi)
        return float(avg[m].sum()) / total

    sub, low_mid, mid, presence, sibilance, air = (
        band(20, 90), band(180, 420), band(420, 2000),
        band(2000, 5000), band(5000, 9000), band(9000, 11000))

    problems, notes = [], []
    # Thresholds MEASURED, not guessed. A clean speech-led mix from this project sits at
    # low-mid 8-13%, sibilance ~15%, presence 21-28%. Deliberately damaged copies read
    # low-mid 26% (a +14 dB bell at 300 Hz) and sibilance 35% (+16 dB at 7 kHz). The
    # first set of thresholds here was invented rather than measured and flagged every
    # file, including the good one, which is worse than no test at all.
    if low_mid > 0.20:
        problems.append("Boxy: %.0f%% of the energy sits in 180-420 Hz, against 8-13%% for a "
                        "clean mix. Voices read as though in a cardboard box - cut a few dB "
                        "there." % (low_mid * 100))
    if sibilance > 0.26:
        problems.append("Harsh: %.0f%% in 5-9 kHz, against about 15%% for a clean mix. The s "
                        "and t sounds pierce, which is the first thing that tires a listener "
                        "on earbuds." % (sibilance * 100))
    if presence < 0.12:
        notes.append("Dull: only %.0f%% in 2-5 kHz, the band that carries consonants, against "
                     "21-28%% typical. Speech may read as muffled." % (presence * 100))

    # Hum is found by being STEADY, not by being loud. Measured on this material,
    # prominence in the 50 Hz bin barely separates a hummed file from a clean one
    # (1.65 against 3.10) while steadiness - how much that band wobbles over the
    # file - separates them cleanly: 1.89 clean against 0.49 hummed. Music and speech
    # never hold a single frequency still; mains hum does nothing else.
    hum_hz = None
    for f0 in (50.0, 60.0, 100.0, 120.0):
        m = (freqs >= f0 - 6) & (freqs <= f0 + 6)
        around = (freqs >= f0 - 40) & (freqs <= f0 + 40) & ~m
        if float(avg[around].mean() or 0) <= 0:
            continue
        prominence = float(avg[m].max()) / float(avg[around].mean())
        over_time = mag[:, m].mean(axis=1)
        steadiness = float(over_time.std() / (over_time.mean() or 1e-9))
        if steadiness < 1.0 and prominence > 2.2:
            hum_hz = f0
            break
    if hum_hz:
        problems.append("Mains hum near %d Hz - a steady tone under everything. A highpass "
                        "at 80 Hz clears it without touching the voice." % hum_hz)

    # How far the quiet moments sit below the loud ones. This is the reliable read on
    # over-compression: squashing a mix lifts the floor. Crest factor was tried first
    # and moved the WRONG WAY on a deliberately compressed file, so it is not used.
    frame_rms = np.sqrt((mag ** 2).mean(axis=1))
    quiet = float(np.percentile(frame_rms, 5))
    loud = float(np.percentile(frame_rms, 95)) or 1e-9
    floor_db = 20 * math.log10(max(quiet, 1e-9) / loud)
    rms = float(np.sqrt(np.mean(x.astype("float64") ** 2))) or 1e-9
    crest = 20 * math.log10(float(np.abs(x).max()) / rms) if x.size else 0
    if floor_db > -16:
        problems.append("Squashed or hissy: the quiet moments sit only %.0f dB below the "
                        "loud ones, against about 24 dB for a clean mix. Either it has been "
                        "compressed flat or there is noise under everything." % abs(floor_db))

    L = ["Listening to %s" % os.path.basename(src), ""]
    L.append("  spectrum   sub %.0f%%  low-mid %.0f%%  mid %.0f%%  presence %.0f%%  "
             "sibilance %.1f%%  air %.1f%%"
             % (sub * 100, low_mid * 100, mid * 100, presence * 100,
                sibilance * 100, air * 100))
    L.append("  dynamics   crest %.1f dB, noise floor %.0f dB down" % (crest, abs(floor_db)))
    L.append("")
    if problems:
        L.append("PROBLEMS (%d):" % len(problems))
        L += ["  - " + p for p in problems]
    else:
        L.append("Nothing measurable is wrong with the sound.")
    if notes:
        L.append("")
        L += ["  " + n for n in notes]
    L.append("")
    L.append("This measures faults. It cannot tell you whether the voice is convincing or "
             "the music suits the film - nothing here can, and anything that claimed to "
             "would be guessing.")
    return "\n".join(L)


# ---------------------------------------------------------------- did it work
PERF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "performance.json")


def _perf_load():
    if not os.path.isfile(PERF_FILE):
        return []
    try:
        return json.load(io.open(PERF_FILE, encoding="utf-8"))
    except (ValueError, OSError):
        return []


def _edit_shape(path):
    """The measurable facts about how a cut was made.

    Only things that can be read off the file. How long it is, how fast it cuts, how
    much of it is someone talking - the levers that were actually pulled while editing,
    so they can later be set against how the post did.
    """
    shape = {}
    try:
        total = video_duration_of(path)
        shape["seconds"] = round(total, 2)
        cuts = [c for c in _review_cuts(path) if 0.2 < c < total - 0.2]
        shape["shots"] = len(cuts) + 1
        shape["avg_shot"] = round(total / max(1, len(cuts) + 1), 2)
    except Exception:
        return shape
    try:
        if has_audio(path):
            words, _l = _word_timings(path, "auto", "large-v3")
            talk = sum(w["e"] - w["s"] for w in words)
            shape["talk_share"] = round(min(1.0, talk / max(0.1, shape["seconds"])), 3)
            if words:
                shape["first_word_at"] = round(words[0]["s"], 2)
    except Exception:
        pass
    return shape


def t_ad_record(a):
    """Log how a published edit actually performed, against how it was cut."""
    src = check_input(a.get("path"), "video")
    views = a.get("views")
    if views is None:
        raise ToolError("Give at least 'views'. The rest are optional but the more you "
                        "give, the sooner patterns mean anything.")

    def num(k):
        v = a.get(k)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            raise ToolError("'%s' must be a number." % k)

    entry = {"file": os.path.basename(src), "path": os.path.abspath(src),
             "platform": (a.get("platform") or "tiktok").lower(),
             "posted": a.get("posted") or time.strftime("%Y-%m-%d"),
             "logged": time.strftime("%Y-%m-%d %H:%M"),
             "views": num("views"), "likes": num("likes"), "comments": num("comments"),
             "shares": num("shares"), "saves": num("saves"),
             "watch_through": num("watch_through"),      # % who reached the end
             "avg_watch_seconds": num("avg_watch_seconds"),
             "note": a.get("note") or "", "shape": _edit_shape(src)}

    rows = [r for r in _perf_load()
            if not (r.get("path") == entry["path"] and r.get("platform") == entry["platform"])]
    rows.append(entry)
    with io.open(PERF_FILE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rows, ensure_ascii=False, indent=1))

    s = entry["shape"]
    return ("Recorded %s on %s: %s views%s.\n"
            "  How it was cut: %.1fs, %d shot(s), average %.1fs each%s.\n"
            "  %d ad(s) logged so far. %s"
            % (entry["file"], entry["platform"], _pretty(entry["views"]),
               ", %.0f%% watched to the end" % entry["watch_through"]
               if entry["watch_through"] is not None else "",
               s.get("seconds", 0), s.get("shots", 0), s.get("avg_shot", 0),
               ", talking %.0f%% of the time" % (s["talk_share"] * 100)
               if "talk_share" in s else "",
               len(rows),
               "Run ad_insights once there are a few to compare." if len(rows) < 4
               else "ad_insights can compare them now."))


def _pretty(n):
    if n is None:
        return "?"
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % n


def t_ad_insights(a):
    """Set how each ad was cut against how it did - and refuse to invent a pattern.

    Four numbers do not make a trend. With a handful of posts the honest answer is a
    ranked list and nothing more; claiming a correlation from five samples is how people
    end up certain of something that was noise.
    """
    rows = _perf_load()
    metric = (a.get("metric") or "watch_through").lower()
    if metric not in ("watch_through", "views", "likes", "avg_watch_seconds"):
        raise ToolError("metric must be watch_through, views, likes or avg_watch_seconds.")
    usable = [r for r in rows if r.get(metric) is not None]
    if not usable:
        return ("Nothing logged with '%s' yet.\nRecord some posts with ad_record - views "
                "at minimum, watch_through if the app shows it, since that is the number "
                "that actually reflects the edit." % metric)

    usable.sort(key=lambda r: r[metric], reverse=True)
    out = ["%d ad(s) ranked by %s:" % (len(usable), metric), ""]
    for r in usable:
        s = r.get("shape") or {}
        out.append("  %-34s %8s   %.0fs  %d shots  avg %.1fs%s"
                   % (r["file"][:34], _pretty(r[metric]), s.get("seconds", 0),
                      s.get("shots", 0), s.get("avg_shot", 0),
                      "  talk %.0f%%" % (s["talk_share"] * 100) if "talk_share" in s else ""))

    if len(usable) < 5:
        out += ["", "Too few to draw anything from. %d more and the shape of what works "
                    "starts to show; below that it is noise wearing a pattern's clothes."
                    % (5 - len(usable))]
        return "\n".join(out)

    # Rank correlation: does a lever move WITH the result, whichever way?
    def spearman(xs, ys):
        n = len(xs)
        rx = {v: i for i, v in enumerate(sorted(xs))}
        ry = {v: i for i, v in enumerate(sorted(ys))}
        d2 = sum((rx[x] - ry[y]) ** 2 for x, y in zip(xs, ys))
        return 1 - (6.0 * d2) / (n * (n * n - 1)) if n > 2 else 0.0

    out += ["", "What moves with %s:" % metric]
    for lever, label in (("seconds", "length"), ("shots", "number of shots"),
                         ("avg_shot", "average shot length"),
                         ("talk_share", "how much talking"),
                         ("first_word_at", "how soon the talking starts")):
        pairs = [(r["shape"][lever], r[metric]) for r in usable
                 if isinstance(r.get("shape"), dict) and lever in r["shape"]]
        if len(pairs) < 5:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if abs(rho) < 0.5:
            verdict = "no clear link"
        else:
            verdict = ("shorter does better" if rho < 0 else "longer does better") \
                if lever in ("seconds", "avg_shot", "first_word_at") else \
                ("less does better" if rho < 0 else "more does better")
        out.append("  %-26s rho %+.2f   %s" % (label, rho, verdict))
    out += ["", "Rank correlation across %d posts. It says what moved together, not what "
                "caused what - a good hook and a short cut often arrive in the same video."
            % len(usable)]
    return "\n".join(out)


# ---------------------------------------------------------------- finding music
# Only these may be put under an advertisement.
#   cc0 / pdm  - no strings
#   by / by-sa - free to use commercially, but the creator MUST be credited
# Everything else is excluded on purpose:
#   *-nc  forbids commercial use, and an ad for a product you sell IS commercial
#   *-nd  forbids derivatives, and trimming a track to length is a derivative
# The distinction is invisible on the download page of most "free music" sites, which
# is exactly how it ends up in a brand film by accident.
MUSIC_OK = {"cc0": "no credit needed", "pdm": "public domain, no credit needed",
            "by": "MUST credit the artist", "by-sa": "MUST credit the artist"}
_LAST_FIND = {}


ANON_PAGE_MAX = 20      # measured: 20 works, 21 returns 401. Not a rate limit.


def _openverse(query, want=20):
    """Search Openverse, paging round the anonymous limit.

    An anonymous caller may ask for at most TWENTY results per page. Ask for
    twenty-one and the answer is 401, which looks exactly like an auth failure or
    a throttle and is neither - it is a hard cap on page size. This code used to
    request fifty, so every search failed, every time, and the error it raised
    said "rate limited, wait a few minutes". That message was wrong for as long as
    it existed, and waiting could never have fixed it. Verified by bisection:
    page_size 3, 10 and 20 all return 200; 21, 30 and 50 all return 401.

    So: page through in twenties instead. A request with no User-Agent really is
    refused (403), which is a separate thing and still true.
    """
    import urllib.request, urllib.parse
    results, page, last = [], 1, None
    while len(results) < want and page <= 6:
        url = "https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(
            {"q": query, "page_size": ANON_PAGE_MAX, "page": page,
             "license_type": "commercial,modification"})
        req = urllib.request.Request(
            url, headers={"User-Agent": "video-editor-mcp/1.0 (+local editing tool)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as fh:
                data = json.load(fh)
        except Exception as e:
            last = e
            if getattr(e, "code", None) == 429 and page == 1:
                time.sleep(2.0)
                continue
            break
        got = data.get("results") or []
        results.extend(got)
        if len(got) < ANON_PAGE_MAX or page >= (data.get("page_count") or 1):
            last = None
            break
        page += 1
        last = None

    if results:
        return {"results": results, "result_count": len(results)}
    if last is None:
        return {"results": [], "result_count": 0}
    code = getattr(last, "code", None)
    if code == 429:
        raise ToolError("The music library is throttling us (HTTP 429). This one really "
                        "is a rate limit - wait a minute. Downloaded tracks are unaffected.")
    if code == 401:
        raise ToolError("The music library refused the request (HTTP 401). Anonymous "
                        "callers may ask for at most %d results a page; asking for more "
                        "returns this. If you see it, something is requesting a larger "
                        "page than that." % ANON_PAGE_MAX)
    raise ToolError("Could not reach the music library (%s: %s). This is the only tool "
                    "here that needs an internet connection."
                    % (type(last).__name__, last))


def t_music_find(a):
    """Search openly-licensed music that may legally go under an advertisement."""
    query = (a.get("query") or "").strip()
    if not query:
        raise ToolError("Give 'query': the mood or style, e.g. 'uplifting corporate'.")
    want = float(a.get("min_seconds", 20))
    limit = max(1, min(12, int(a.get("limit", 8))))

    data = _openverse(query, 50)
    rows, kept = [], []
    for r in data.get("results", []):
        lic = (r.get("license") or "").lower()
        if lic not in MUSIC_OK:
            continue                                   # belt and braces over the API filter
        secs = (r.get("duration") or 0) / 1000.0
        if secs < want:
            continue
        src = r.get("url")
        if not src:
            continue
        kept.append({"title": r.get("title") or "untitled",
                     "creator": r.get("creator") or "unknown",
                     "license": lic, "seconds": secs, "url": src,
                     "page": r.get("foreign_landing_url") or ""})
        if len(kept) >= limit:
            break

    if not kept:
        return ("Nothing matched '%s' at %.0f seconds or longer that is also cleared for "
                "commercial use.\nTry a broader word (happy, calm, energetic, cinematic) or "
                "a shorter min_seconds." % (query, want))

    _LAST_FIND.clear()
    for i, k in enumerate(kept, 1):
        _LAST_FIND[i] = k
        rows.append("%2d. %-40s %5.0fs  %-5s  %s\n      by %s"
                    % (i, k["title"][:40], k["seconds"], k["license"],
                       MUSIC_OK[k["license"]], k["creator"][:40]))
    need = sorted(set(k["license"] for k in kept if k["license"] in ("by", "by-sa")))
    note = ("\n\nTracks marked 'by' or 'by-sa' are free to use in an ad but the artist has "
            "to be credited. music_fetch writes the credit line into ATTRIBUTION.txt beside "
            "the file so it is not lost." if need else "")
    return ("Music cleared for commercial use, %.0fs or longer, matching '%s':\n\n%s%s\n\n"
            "Pick one with music_fetch(choice=N)." % (want, query, "\n".join(rows), note))


def t_music_fetch(a):
    """Download a track from the last search, and record what crediting it needs."""
    folder = a.get("folder") or os.path.join(os.path.expanduser("~"), "Downloads", "music")
    choice, url = a.get("choice"), a.get("url")
    if choice is not None:
        try:
            pick = _LAST_FIND[int(choice)]
        except (KeyError, ValueError, TypeError):
            raise ToolError("No such choice. Run music_find first, then pass one of its "
                            "numbers. (Known: %s)"
                            % (", ".join(str(k) for k in sorted(_LAST_FIND)) or "none"))
    elif url:
        pick = {"title": a.get("title") or "track", "creator": a.get("creator") or "unknown",
                "license": (a.get("license") or "cc0").lower(), "url": url, "page": url,
                "seconds": 0}
        if pick["license"] not in MUSIC_OK:
            raise ToolError("Licence '%s' is not cleared for commercial use. Allowed: %s."
                            % (pick["license"], ", ".join(sorted(MUSIC_OK))))
    else:
        raise ToolError("Give 'choice' (a number from music_find) or 'url'.")

    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w\- ]+", "", pick["title"]).strip()[:50] or "track"
    dest = os.path.join(folder, "%s.mp3" % safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(folder, "%s_%d.mp3" % (safe, n))
        n += 1

    import urllib.request
    req = urllib.request.Request(pick["url"], headers={"User-Agent": "video-editor-mcp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as fh, io.open(dest, "wb") as out:
            shutil.copyfileobj(fh, out, 1 << 16)
    except Exception as e:
        raise ToolError("Download failed (%s: %s)." % (type(e).__name__, e))
    if os.path.getsize(dest) < 8192:
        os.remove(dest)
        raise ToolError("That download came back empty.")

    # A credit obligation you cannot find later is a credit obligation you will breach.
    credit = ""
    if pick["license"] in ("by", "by-sa"):
        credit = ('"%s" by %s, licensed CC %s. %s'
                  % (pick["title"], pick["creator"], pick["license"].upper(),
                     pick.get("page") or pick["url"]))
        with io.open(os.path.join(folder, "ATTRIBUTION.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s\n  file: %s\n\n" % (credit, os.path.basename(dest)))

    try:
        env, rate = _onset_envelope(dest)
        bpm, beats = _beat_grid(env, rate)
        tempo = "\n  %.1f BPM, %d beats - ready for video_cut_to_beat." % (bpm, len(beats))
    except Exception:
        tempo = ""
    return done(dest, "\"%s\" by %s (CC %s - %s).%s%s"
                % (pick["title"], pick["creator"], pick["license"].upper(),
                   MUSIC_OK[pick["license"]], tempo,
                   "\n  Credit line saved to ATTRIBUTION.txt:\n    " + credit if credit else ""))


# ---------------------------------------------------------------- rhythm
def _onset_envelope(path, sr=22050, hop=512, n_fft=1024):
    """Spectral flux: how much the sound CHANGES frame to frame.

    Beats are not the loudest moments, they are the moments something new starts, so
    what gets measured is the rise in each frequency band rather than the level.
    """
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le",
                          "-ac", "1", "-ar", str(sr), "-"],
                         capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    if x.size < n_fft * 4:
        raise ToolError("That audio is too short to find a beat in.")
    n = 1 + (x.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    win = np.hanning(n_fft).astype("f4")
    mag = np.abs(np.fft.rfft(x[idx] * win, axis=1))
    mag = np.log1p(mag * 8.0)
    flux = np.diff(mag, axis=0)
    env = np.maximum(flux, 0).sum(axis=1)
    if env.max() > 0:
        env = env / env.max()
    return env, float(sr) / hop


def _beat_grid(env, rate, bpm_lo=60.0, bpm_hi=190.0):
    """Tempo by autocorrelation, then the phase that best lines a pulse train up."""
    import numpy as np
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lo, hi = int(round(rate * 60.0 / bpm_hi)), int(round(rate * 60.0 / bpm_lo))
    hi = min(hi, len(ac) - 1)
    if hi <= lo:
        raise ToolError("Not enough audio to measure a tempo.")
    lag = lo + int(np.argmax(ac[lo:hi]))
    bpm = 60.0 * rate / lag
    # Songs often autocorrelate strongest at half or double time; nudge into a range
    # that reads as the actual pulse of the track.
    while bpm < 85:
        bpm *= 2
    while bpm > 175:
        bpm /= 2
    period = 60.0 / bpm * rate
    best, best_score = 0.0, -1e9
    for off in np.arange(0, period, max(1.0, period / 60.0)):
        pos = np.arange(off, len(env), period).astype(int)
        pos = pos[pos < len(env)]
        score = env[pos].sum() / max(1, len(pos))
        if score > best_score:
            best, best_score = off, score
    times = (np.arange(best, len(env), period) / rate)
    return float(bpm), [round(float(t), 3) for t in times]


def t_music_beats(a):
    """Where the beats fall in a track, so cuts can be put on them."""
    src = check_input(a.get("path"), "music")
    lo = float(a.get("min_bpm", 60))
    hi = float(a.get("max_bpm", 190))

    def compute():
        env, rate = _onset_envelope(src)
        bpm, times = _beat_grid(env, rate, lo, hi)
        return {"bpm": bpm, "beats": times}

    got = cached("beats", src, {"lo": lo, "hi": hi}, compute)
    beats = got["beats"]
    show = ", ".join("%.2f" % t for t in beats[:12])
    return ("%s\n  %.1f BPM - a beat every %.3fs, %d in the track.\n"
            "  First beats: %s%s\n"
            "  Pass this file to video_cut_to_beat to lay the cuts on them."
            % (os.path.basename(src), got["bpm"], 60.0 / got["bpm"], len(beats),
               show, " ..." if len(beats) > 12 else ""))


def t_cut_to_beat(a):
    """Assemble clips so every cut lands on a beat of the music.

    This is most of what makes a social edit feel tight. Each clip is trimmed to the
    nearest whole number of beats rather than to a length in seconds, so the cuts and
    the pulse agree instead of drifting apart.
    """
    paths = a.get("paths") or []
    if not isinstance(paths, list) or len(paths) < 2:
        raise ToolError("Give 'paths': two or more clips, in play order.")
    clips = [check_input(p, "video") for p in paths]
    music = check_input(a.get("music"), "music")
    every = max(1, int(a.get("beats_each", 2)))
    start_at = float(a.get("start_beat", 0))
    fade = float(a.get("transition_duration", 0.0))
    trans = a.get("transition") or "fade"

    got = cached("beats", music, {"lo": 60.0, "hi": 190.0},
                 lambda: (lambda ev: {"bpm": ev[0], "beats": ev[1]})(
                     _beat_grid(*_onset_envelope(music))))
    bpm, beats = got["bpm"], got["beats"]
    beats = [t for t in beats if t >= start_at]
    if len(beats) < len(clips) + 1:
        raise ToolError("The track only has %d beat(s) after %.2fs - not enough for %d clips."
                        % (len(beats), start_at, len(clips)))

    tmp = _tmpdir()
    segs, plan = [], []
    for i, clip in enumerate(clips):
        want = beats[min((i + 1) * every, len(beats) - 1)] - beats[min(i * every, len(beats) - 1)]
        have = video_duration_of(clip)
        take = min(want, have)
        if take < 0.15:
            continue
        seg = os.path.join(tmp, "cb%02d_%d.mp4" % (i, os.getpid()))
        ffmpeg_run(["-i", clip, "-t", "%.3f" % take] + FAST_ENC +
                   (AUDIO_ENC if has_audio(clip) else ["-an"]) + [seg])
        segs.append(seg)
        plan.append((os.path.basename(clip), take, want, have < want))
    if len(segs) < 2:
        raise ToolError("Nothing long enough survived the trim.")

    cut = os.path.join(tmp, "beatcut_%d.mp4" % os.getpid())
    if fade > 0.01:
        t_join_smooth({"paths": segs, "transition": trans, "duration": fade, "output": cut})
    else:
        t_merge({"paths": segs, "output": cut})

    out = make_output(clips[0], "onbeat", a.get("output"), ".mp4")
    t_add_music({"path": cut, "music": music, "volume": float(a.get("volume", 0.32)),
                 "keep_original_audio": bool(a.get("keep_original_audio", True)),
                 "output": out})

    rows = "\n".join("    %-28s %5.2fs%s" % (n[:28], t, "  (clip ran out)" if short else "")
                     for n, t, _w, short in plan)
    return done(out, "Cut to the beat at %.1f BPM - one cut every %d beat(s), %.3fs apart.\n"
                     "  %d clip(s):\n%s\n  Length %.2fs."
                % (bpm, every, 60.0 / bpm * every, len(segs), rows, video_duration_of(out)))


# ---------------------------------------------------------------- voice
THAI_VOICES = {"female": "th-TH-PremwadeeNeural", "male": "th-TH-NiwatNeural"}
EN_VOICES = {"female": "en-US-AriaNeural", "male": "en-US-GuyNeural"}


def t_voice_over(a):
    """Speak a script and lay it over the video, ducking the existing sound under it.

    Runs on Microsoft's free online voices through edge-tts - no key, no account, but it
    does need a connection, unlike everything else here.
    """
    text = (a.get("text") or "").strip()
    if not text:
        raise ToolError("Give 'text': what the voice should say.")
    try:
        import edge_tts
    except ImportError:
        raise ToolError("edge-tts is not installed. Run:\n    pip install edge-tts")

    lang = (a.get("language") or "th").lower()
    gender = (a.get("gender") or "female").lower()
    voice = a.get("voice") or (THAI_VOICES if lang.startswith("th") else EN_VOICES).get(gender)
    if not voice:
        raise ToolError("gender must be 'female' or 'male', or name a 'voice' directly.")
    rate = a.get("rate") or "+0%"
    pitch = a.get("pitch") or "+0Hz"

    tmp = _tmpdir()
    speech = os.path.join(tmp, "vo_%d.mp3" % os.getpid())

    async def speak():
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(speech)

    try:
        asyncio.run(speak())
    except Exception as e:
        raise ToolError("Speech generation failed (it needs an internet connection): %s" % e)
    if not os.path.isfile(speech) or os.path.getsize(speech) < 512:
        raise ToolError("The voice came back empty. Check the text and the language.")
    spoken = duration_of(speech)

    if not a.get("path"):
        out = a.get("output") or os.path.join(os.path.dirname(os.path.abspath(speech)),
                                              "voice_over.mp3")
        shutil.copyfile(speech, out)
        return done(out, "Voice only: %s, %.2fs." % (voice, spoken))

    src = check_input(a.get("path"), "video")
    at = parse_time(a.get("at"), "at") or 0.0
    gain = float(a.get("volume", 1.0))
    duck = float(a.get("duck", 0.35))
    out = make_output(src, "vo", a.get("output"), ".mp4")

    if has_audio(src):
        # Pull the bed down only while the voice is actually talking, with a short ramp
        # either side so the level does not step.
        ramp = 0.25
        env = ("volume='1-%.3f*min(1\\,min((t-%.3f)/%.3f\\,(%.3f-t)/%.3f))':eval=frame"
               % (1.0 - duck, at, ramp, at + spoken, ramp))
        fc = ("[0:a]%s[bed];[1:a]adelay=%d|%d,volume=%.3f[vo];"
              "[bed][vo]amix=inputs=2:duration=first:normalize=0[a]"
              % (env, int(at * 1000), int(at * 1000), gain))
        ffmpeg_run(["-i", src, "-i", speech, "-filter_complex", fc,
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + AUDIO_ENC + [out])
        note = "existing sound ducked to %.0f%% under it" % (duck * 100)
    else:
        fc = "[1:a]adelay=%d|%d,volume=%.3f[a]" % (int(at * 1000), int(at * 1000), gain)
        ffmpeg_run(["-i", src, "-i", speech, "-filter_complex", fc,
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + AUDIO_ENC +
                   ["-shortest", out])
        note = "the clip had no sound of its own"
    try:
        os.remove(speech)
    except OSError:
        pass
    return done(out, "Voice-over in %s (%s), %.2fs from %.2fs - %s."
                % (lang, voice, spoken, at, note))


TOOLS = [
    {
        "name": "font_library",
        "description": "Show every typeface in the library set in YOUR OWN words, at caption "
                       "size, optionally over a real frame from your footage - because a list "
                       "of font names tells you nothing about how they look in Thai. 39 faces: "
                       "the Thai National set, the Google Thai faces social media actually "
                       "uses, and Latin display faces for English titles. Filter by kind "
                       "(caption / title / hand). Latin-only faces are hidden when the text "
                       "is Thai, and named so you know why.",
        "handler": t_font_library,
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The words to set. Defaults to a Thai sample."},
                "kind": {"enum": ["all", "caption", "title", "hand"]},
                "thai": {"type": "boolean",
                         "description": "Force the Thai filter on or off. Detected from the "
                                        "text if left out."},
                "over": {"type": "string",
                         "description": "An image or video to set the words over, so they are "
                                        "judged against real footage."},
                "at": {"type": "number", "description": "Seconds into that video."},
                "output": {"type": "string"}},
            "required": []},
    },
    {
        "name": "video_shape",
        "description": "LOOK at the SHAPE of a finished film - pacing, brightness, motion "
                       "and sound as lanes on one clock, returned as an image. A grid of "
                       "stills shows every moment and none of the shape; this shows what "
                       "changes over TIME. A zig-zag brightness lane is a montage that "
                       "flickers, a flat motion lane is a dead stretch, a sound lane that "
                       "never dips is music nobody ducked. Run it before delivering.",
        "handler": t_video_shape,
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "music_describe",
        "description": "What a piece of music actually IS, so a choice can be judged instead "
                       "of guessed: what instruments are in it, its tempo, whether it is bright "
                       "or warm, and - the one that decides most edits - whether it BUILDS or "
                       "sits flat. Run it before putting any track under a film. It measures; "
                       "it cannot hear, and it says so.",
        "handler": t_music_describe,
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "A music file, or a video to read the "
                                                   "music out of."}},
            "required": ["path"]},
    },
    {
        "name": "photo_montage",
        "description": "A folder of photos - and optionally the video someone took at the "
                       "same event - into one finished film. Puts the pictures back in the "
                       "order they were taken, moves slowly over each one, lifts the best "
                       "moments out of the clips by finding where the room got loudest, "
                       "keeps a wrongly-shaped clip's edges instead of cropping people out, "
                       "lays music underneath that ducks wherever there is something real "
                       "to hear, and burns the title and closing line. Use for a birthday, "
                       "a wedding, a trip - anything where the deliverable is 'all of these "
                       "photos, as one video'.",
        "handler": t_montage,
        "inputSchema": {
            "type": "object",
            "properties": {
                "photos": {"description": "A list of image files, or one folder path.",
                           "oneOf": [{"type": "array", "items": {"type": "string"}},
                                     {"type": "string"}]},
                "clips": {"type": "array", "items": {"type": "string"},
                          "description": "Video shot at the same event. The strongest moment "
                                         "opens the film and the next one closes it."},
                "clip_highlights": {"type": "boolean",
                                    "description": "Default true: pick those moments by "
                                                   "level. False takes each clip from its "
                                                   "start."},
                "clip_seconds": {"type": "number", "description": "Opening moment, default 13."},
                "clip_tail_seconds": {"type": "number",
                                      "description": "Closing moment, default 3.7."},
                "order": {"enum": ["date", "name", "given"],
                          "description": "Default date - EXIF date taken, which survives "
                                         "photos being collected from several phones."},
                "finish_with": {"type": "string",
                                "description": "Filename of the photo to END on. Worth "
                                               "setting: date order finishes on whatever "
                                               "happened last, which is rarely the best "
                                               "last frame."},
                "open_with": {"type": "string",
                              "description": "Filename of the photo to lead with."},
                "shape": {"enum": ["auto", "4:5", "9:16", "1:1", "3:4", "16:9"],
                          "description": "Default auto: follows whichever way most of the "
                                         "photos face."},
                "seconds_each": {"description": "Seconds per photo. ONE number for an even "
                                                "montage, or a LIST with one entry per photo "
                                                "in play order - linger on what matters, move "
                                                "through the rest. Uneven holds are most of "
                                                "what separates an edit from a slideshow. "
                                                "Default 2.4.",
                                 "oneOf": [{"type": "number"},
                                           {"type": "array", "items": {"type": "number"}}]},
                "match_exposure": {"type": "boolean",
                                   "description": "Default true: pulls each photo's "
                                                  "exposure toward the middle of the set so "
                                                  "the montage stops flickering light-dark. "
                                                  "Partial by design - a dim room should "
                                                  "still look dim."},
                "match_strength": {"type": "number",
                                   "description": "How far toward the middle, 0-1. Default "
                                                  "0.65; 1.0 looks corrected."},
                "shorten_repeats": {"type": "boolean",
                                    "description": "Default true: when two neighbours are "
                                                   "nearly the same picture, the second is "
                                                   "held briefly so the pair reads as one "
                                                   "beat instead of a stutter. No photo is "
                                                   "dropped."},
                "twin_limit": {"type": "number",
                               "description": "How alike counts as a repeat, default 18. "
                                              "Measured on a real album: a true repeat "
                                              "scored 7.7, the next closest pair 30.1."},
                "repeat_scale": {"type": "number",
                                 "description": "How long a repeat is held, as a fraction "
                                                "of seconds_each. Default 0.55."},
                "hold_last": {"type": "number",
                              "description": "The final photo, so a closing line can be read."},
                "transition_seconds": {"type": "number", "description": "Dissolve, default 0.45."},
                "title": {"type": "string", "description": "Opening line, \\n for a break."},
                "closing": {"type": "string", "description": "Closing line, \\n for a break."},
                "captions": {"type": "array",
                             "description": "Any further [{start, end, text}] of your own.",
                             "items": {"type": "object", "properties": {
                                 "start": {"type": "string"}, "end": {"type": "string"},
                                 "text": {"type": "string"}}}},
                "music": {"type": "string", "description": "A track of your own. Left out, "
                                                           "one is synthesised."},
                "music_mood": {"enum": ["calm", "uplifting", "warm", "tense", "gentle"]},
                "music_bpm": {"type": "number"},
                "music_volume": {"type": "number", "description": "Default 0.75."},
                "music_duck": {"type": "number",
                               "description": "Level under the clips, default 0.20."},
                "font": {"type": "string"}, "font_scale": {"type": "number"},
                "accent": {"type": "string"}, "text_color": {"type": "string"},
                "outline": {"type": "number"}, "glow": {"type": "number"},
                "margin_bottom": {"type": "number"},
                "grain": {"type": "number"}, "saturation": {"type": "number"},
                "warmth": {"type": "number",
                           "description": "-1 to 1. Red into the highlights, blue out of "
                                          "the shadows. 0.35 settles mixed indoor light."},
                "contrast": {"type": "number",
                             "description": "-0.35 to 0.35. An S-curve, so it deepens the "
                                            "shadows without dragging faces with it. 0.18 "
                                            "is a gentle film contrast."},
                "vignette": {"type": "number",
                             "description": "0 to 1, barely-there by design. Darkens the "
                                            "corners so the eye goes to the faces. 0.4 is "
                                            "invisible and effective; 1.0 is too much."},
                "transition": {"type": "string"}, "fps": {"type": "integer"},
                "target_lufs": {"type": "number"}, "true_peak": {"type": "number"},
                "warmth_drift": {"type": "number",
                                 "description": "0-1, default 0.45 when warmth is set. How "
                                                "much cooler the opening is than the ending. "
                                                "Felt rather than seen."},
                "align_title": {"type": "boolean",
                                "description": "Default true: the title appears on a beat of "
                                               "the music, not on an arbitrary second."},
                "title_motion": {"type": "boolean",
                                 "description": "Default true: the opening title fades and "
                                                "settles in rather than simply appearing."},
                "audio_bleed": {"type": "number",
                                "description": "Seconds of the clip's own sound carried "
                                               "PAST the picture at the opening and BEFORE "
                                               "it at the ending. Default 2.2. This is the "
                                               "difference between a cut and an edit; 0 "
                                               "turns it off."},
                "bleed_level": {"type": "number", "description": "Default 0.9."},
                "sound_design": {"type": "boolean",
                                 "description": "Default true: lays room tone taken from "
                                                "your own clip under the photographs, so "
                                                "the quiet parts sound like the room rather "
                                                "than like digital silence, and places two "
                                                "quiet accents. False for music only."},
                "room_tone_level": {"type": "number", "description": "Default 0.5."},
                "sfx": {"type": "array", "description": "Your own [{sound, at, gain}]. "
                                                        "Replaces the automatic accents.",
                        "items": {"type": "object", "properties": {
                            "sound": {"type": "string"}, "at": {"type": "string"},
                            "gain": {"type": "number"}}}},
                "save_plan": {"type": "string",
                              "description": "Write the resolved plan here as JSON - what "
                                             "was actually chosen, photo by photo. Feed it "
                                             "back with `plan` to re-render, so changing "
                                             "one hold is one number instead of a rebuilt "
                                             "call."},
                "plan": {"type": "string",
                         "description": "A plan saved earlier. Anything else you pass "
                                        "overrides it."},
                "output": {"type": "string"}},
            "required": []},
    },
    {
        "name": "video_build",
        "description": "Join, grade, caption, score and level a cut in ONE pass over the "
                       "footage. Run as separate tools each stage decodes the whole thing, "
                       "encodes it again and writes twenty-odd megabytes the next stage "
                       "immediately reads back; measured on a real 25s ad, three passes took "
                       "44.8s and the same work fused took 26.3s. Feed it pieces you have "
                       "already trimmed - preparing those in parallel is quicker still.",
        "handler": t_build,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Prepared pieces, in play order."},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "duration": {"type": "number", "description": "Transition length. Default 0.08."},
            "audio_lead": {"type": "number",
                           "description": "Positive = J-cut (sound before picture), "
                                          "negative = L-cut. 0.2-0.5 is usual."},
            "audio_crossfade": {"type": "number"},
            "grain": {"type": "number", "description": "0-1. Also hides a resolution mismatch."},
            "desaturate": {"type": "number", "description": "Overall saturation, e.g. 0.88."},
            "product_colour": {"type": "string",
                               "enum": ["blue", "cyan", "red", "green", "yellow", "magenta"],
                               "description": "Pulled back everywhere else so it stands out."},
            "product_lift": {"type": "number"},
            "speech_timing": {"type": "boolean",
                              "description": "Default true: recognise the speech so each word "
                                             "lights exactly as it is said. Turning it off is "
                                             "quicker but puts the highlight about a third of a "
                                             "second off the voice - drafts only. Recognition is "
                                             "cached, so only the first build pays for it."},
            "captions": {"type": "array",
                         "description": "[{start, end, text}] with newlines for line breaks - "
                                        "same shape kinetic_captions takes.",
                         "items": {"type": "object", "properties": {
                             "start": {"type": "string"}, "end": {"type": "string"},
                             "text": {"type": "string"}}}},
            "font": {"type": "string", "enum": list(SUBTITLE_FONTS)},
            "font_scale": {"type": "number"}, "outline": {"type": "number"},
            "glow": {"type": "number"}, "accent": {"type": "string"},
            "text_color": {"type": "string"}, "margin_bottom": {"type": "number"},
            "sfx": {"type": "array",
                    "description": "[{sound, at, gain}]. Combos are expanded automatically.",
                    "items": {"type": "object", "properties": {
                        "sound": {"type": "string"}, "at": {"type": "string"},
                        "gain": {"type": "number"}}}},
            "music": {"type": "string"}, "music_volume": {"type": "number"},
            "target_lufs": {"type": "number"}, "true_peak": {"type": "number"},
            "fps": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_review",
        "description": "LOOK at a finished video and say what is wrong with it. video_check "
                       "measures levels and black bars and never looks at the picture; this "
                       "samples the moments that matter, tiles them into one image to judge, "
                       "and flags what can be measured: captions running into the edge or "
                       "sitting under the phone's interface, cues too wide or too many lines, "
                       "cuts landing inside a spoken word, shots so short they flash. Run it "
                       "before calling anything finished.",
        "handler": t_review,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "subtitles": {"type": "string",
                          "description": "The .srt that was burned in. Given one, the wording "
                                         "and line counts get checked too, not just the pixels."},
            "font": {"type": "string", "enum": list(SUBTITLE_FONTS),
                     "description": "Which font was burned in, so widths are judged against it."},
            "max_lines": {"type": "integer", "description": "Default 2."},
            "frames": {"type": "integer", "description": "How many moments to sample. Default 12."},
            "edge_margin": {"type": "number",
                            "description": "How close to the frame edge counts as overflow. "
                                           "Default 0.045."},
            "safe_bottom": {"type": "number",
                            "description": "Below this height captions collide with the "
                                           "platform interface. Default 0.90."},
            "language": {"type": "string"}, "model": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "edit_history",
        "description": "What has been done, and what a given file was made from. Every tool "
                       "call is recorded, so a folder of near-identical exports stops being "
                       "a guess - give it a file and it traces the chain back to the source.",
        "handler": t_edit_history,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Trace this file back through everything that made it. "
                                    "Leave out for a plain list of recent edits."},
            "limit": {"type": "integer", "description": "How many recent edits to list. Default 20."}},
        },
    },
    {
        "name": "video_cut_out",
        "description": "Remove one or more stretches from the middle and close the gap - "
                       "lifting a fluffed line or a long pause. The everyday edit that "
                       "otherwise takes two trims and a merge.",
        "handler": t_cut_out,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "ranges": {"type": "array", "description": "Stretches to delete.",
                       "items": {"type": "object", "properties": {
                           "start": {"type": "string"}, "end": {"type": "string"}}}},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "transition_duration": {"type": "number",
                                    "description": "0 (default) = hard cut. A short dissolve "
                                                   "hides that anything was removed."},
            "output": {"type": "string"}},
            "required": ["path", "ranges"]},
    },
    {
        "name": "video_censor",
        "description": "Bleep or silence words - a crude line fixed without reshooting. Give "
                       "the times from video_transcript.",
        "handler": t_censor,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "ranges": {"type": "array", "description": "Stretches to cover.",
                       "items": {"type": "object", "properties": {
                           "start": {"type": "string"}, "end": {"type": "string"}}}},
            "mode": {"type": "string", "enum": ["beep", "silence"],
                     "description": "beep (default) puts a tone over it; silence just drops it."},
            "frequency": {"type": "number", "description": "Tone pitch in Hz. Default 1000."},
            "level": {"type": "number", "description": "Tone loudness, 0-1. Default 0.25."},
            "output": {"type": "string"}},
            "required": ["path", "ranges"]},
    },
    {
        "name": "video_punch_in",
        "description": "Push into part of the frame at a moment and ease back out - emphasis "
                       "without a second camera. A hard cut to a tighter framing reads as a "
                       "jump; easing in over a few tenths reads as a move.",
        "handler": t_punch_in,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "at": {"type": "string", "description": "When the move starts."},
            "zoom": {"type": "number", "description": "How far in. 1.25 is a normal nudge."},
            "hold": {"type": "number", "description": "Seconds held in tight. Default 1.5."},
            "ramp": {"type": "number", "description": "Ease time each side. Default 0.4."},
            "focus_x": {"type": "number", "description": "0-1 across the frame. Default 0.5."},
            "focus_y": {"type": "number", "description": "0-1 down the frame. Default 0.42, "
                                                         "which is where a face usually sits."},
            "output": {"type": "string"}},
            "required": ["path", "at"]},
    },
    {
        "name": "video_replace_audio",
        "description": "Put a separately recorded track onto the picture, with an offset to "
                       "line it up, and optionally keep the original underneath.",
        "handler": t_replace_audio,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "audio": {"type": "string", "description": "The audio file to lay on."},
            "offset": {"type": "number", "description": "Seconds. Positive delays the new "
                                                        "audio, negative trims its head."},
            "keep_original": {"type": "number",
                              "description": "0 (default) replaces it; 0.2 keeps the original "
                                             "underneath at 20%."},
            "output": {"type": "string"}},
            "required": ["path", "audio"]},
    },
    {
        "name": "video_lower_third",
        "description": "The name bar that slides in at the bottom - a product, a price, a "
                       "person. It moves and fades rather than appearing, which is what makes "
                       "it read as a graphic instead of a caption.",
        "handler": t_lower_third,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"}, "subtitle": {"type": "string"},
            "at": {"type": "string"}, "duration": {"type": "number"},
            "accent": {"type": "string", "description": "Hex for the second line."},
            "font": {"type": "string", "enum": list(SUBTITLE_FONTS)},
            "font_size": {"type": "integer"},
            "y": {"type": "number", "description": "Height down the frame, 0-1. Default 0.74."},
            "output": {"type": "string"}},
            "required": ["path", "title"]},
    },
    {
        "name": "video_compare",
        "description": "Put two versions side by side, labelled, so a change can actually be "
                       "judged. Tall clips are stacked and wide ones set beside each other "
                       "unless told otherwise.",
        "handler": t_compare,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Exactly two videos."},
            "labels": {"type": "array", "items": {"type": "string"}},
            "layout": {"type": "string", "enum": ["auto", "horizontal", "vertical"]},
            "audio": {"type": "string", "enum": ["left", "right", "none"]},
            "output": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_loop",
        "description": "Repeat a clip, or bounce it forwards and back. The boomerang drops "
                       "the duplicated turnaround frame so the bounce does not stutter.",
        "handler": t_loop,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "times": {"type": "integer", "description": "How many passes. Default 2."},
            "boomerang": {"type": "boolean", "description": "Forwards then backwards."},
            "keep_audio": {"type": "boolean", "description": "For a boomerang, default false - "
                                                             "reversed speech is rarely wanted."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_split",
        "description": "Chop one video into several files - for a multi-part post, or to hand "
                       "out rushes. Give a length, a count, or exact times.",
        "handler": t_split,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "every": {"type": "number", "description": "Seconds per part."},
            "parts": {"type": "integer", "description": "Number of equal parts."},
            "at": {"type": "array", "items": {"type": "string"},
                   "description": "Exact times to cut at."},
            "folder": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_cut_to_beat",
        "description": "Assemble clips so every cut lands on a beat of the music - most of "
                       "what makes a social edit feel tight. Each clip is trimmed to a whole "
                       "number of beats rather than to a length in seconds, so the cutting "
                       "and the pulse stay together instead of drifting apart.",
        "handler": t_cut_to_beat,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Clips in play order."},
            "music": {"type": "string", "description": "The track to cut against."},
            "beats_each": {"type": "integer",
                           "description": "How many beats each shot holds. 2 is punchy, "
                                          "4 is a normal bar, 8 is slow. Default 2."},
            "start_beat": {"type": "number", "description": "Ignore beats before this time - "
                                                            "use it to skip an intro."},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "transition_duration": {"type": "number", "description": "0 (default) = hard cuts, "
                                                                     "which is what lands on a beat."},
            "volume": {"type": "number", "description": "Music level. Default 0.32."},
            "keep_original_audio": {"type": "boolean"},
            "output": {"type": "string"}},
            "required": ["paths", "music"]},
    },
    {
        "name": "sound_faults",
        "description": "The faults a mix engineer listens FOR, measured: a boxy low-mid "
                       "pile-up, piercing sibilance, mains hum, hiss under the quiet parts, "
                       "and dynamics squashed flat. Each is reported with the number behind "
                       "it. Use it alongside audio_scope, which answers the different "
                       "question of whether music is burying the voice.",
        "handler": t_sound_faults,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "ad_record",
        "description": "Log how a published edit actually performed, alongside how it was "
                       "cut. Read the numbers off TikTok or Instagram and pass them in - "
                       "this measures the edit itself (length, shot count, pace, how much "
                       "talking) so the two can later be set against each other. Everything "
                       "else here judges whether an edit is technically correct; this is the "
                       "only thing that knows whether it worked.",
        "handler": t_ad_record,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "The video that was posted."},
            "platform": {"type": "string", "description": "tiktok, instagram, facebook, line..."},
            "views": {"type": "number"},
            "watch_through": {"type": "number",
                              "description": "Percent who reached the end. The number that "
                                             "reflects the EDIT rather than the thumbnail."},
            "avg_watch_seconds": {"type": "number"},
            "likes": {"type": "number"}, "comments": {"type": "number"},
            "shares": {"type": "number"}, "saves": {"type": "number"},
            "posted": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
            "note": {"type": "string", "description": "Anything unusual - a boost, a trend, "
                                                      "a collaboration."}},
            "required": ["path", "views"]},
    },
    {
        "name": "ad_insights",
        "description": "Compare the ads you have logged: what was cut how, against how each "
                       "one did. Below five posts it ranks them and says plainly that there "
                       "is nothing to conclude - a correlation from four samples is noise "
                       "wearing a pattern's clothes.",
        "handler": t_ad_insights,
        "inputSchema": {"type": "object", "properties": {
            "metric": {"type": "string",
                       "enum": ["watch_through", "views", "likes", "avg_watch_seconds"],
                       "description": "What to rank by. Default watch_through."}},
        },
    },
    {
        "name": "music_find",
        "description": "Search openly-licensed music that may legally go under an "
                       "advertisement. Only CC0, public domain, BY and BY-SA are returned: "
                       "NC licences forbid commercial use and ND licences forbid trimming a "
                       "track to length, and neither distinction is visible on the download "
                       "page of most 'free music' sites. Needs an internet connection.",
        "handler": t_music_find,
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "Mood or style - 'uplifting corporate', 'calm piano', "
                                     "'energetic pop'."},
            "min_seconds": {"type": "number",
                            "description": "Shorter than your video means an audible loop. "
                                           "Default 20."},
            "limit": {"type": "integer", "description": "How many to list. Default 8."}},
            "required": ["query"]},
    },
    {
        "name": "music_fetch",
        "description": "Download a track found by music_find, measure its tempo, and write "
                       "the credit line into ATTRIBUTION.txt when the licence requires one - "
                       "a crediting obligation you cannot find later is one you will breach.",
        "handler": t_music_fetch,
        "inputSchema": {"type": "object", "properties": {
            "choice": {"type": "integer", "description": "A number from the last music_find."},
            "url": {"type": "string", "description": "Or a direct link you have checked "
                                                     "yourself; then give 'license' too."},
            "license": {"type": "string", "enum": sorted(MUSIC_OK)},
            "title": {"type": "string"}, "creator": {"type": "string"},
            "folder": {"type": "string", "description": "Default ~/Downloads/music."}},
        },
    },
    {
        "name": "music_beats",
        "description": "Find a track's tempo and where its beats fall. Measures the rise in "
                       "each frequency band rather than the level, because a beat is where "
                       "something STARTS, not where it is loudest.",
        "handler": t_music_beats,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "min_bpm": {"type": "number"}, "max_bpm": {"type": "number"}},
            "required": ["path"]},
    },
    {
        "name": "voice_over",
        "description": "Speak a script in Thai or English and lay it over the video, ducking "
                       "the existing sound under it. Leave 'path' out to just get the audio "
                       "file. Uses Microsoft's free voices - no key or account, but unlike "
                       "everything else here it needs an internet connection.",
        "handler": t_voice_over,
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "What to say."},
            "path": {"type": "string", "description": "Video to lay it over. Optional."},
            "language": {"type": "string", "enum": ["th", "en"]},
            "gender": {"type": "string", "enum": ["female", "male"]},
            "voice": {"type": "string", "description": "An exact voice name, e.g. "
                                                       "th-TH-NiwatNeural."},
            "at": {"type": "string", "description": "When it starts. Default the beginning."},
            "rate": {"type": "string", "description": "Speaking speed, e.g. '-10%' or '+15%'."},
            "pitch": {"type": "string", "description": "e.g. '-20Hz'."},
            "volume": {"type": "number", "description": "Voice level. Default 1."},
            "duck": {"type": "number", "description": "How far the existing sound drops while "
                                                      "the voice talks. Default 0.35."},
            "output": {"type": "string"}},
            "required": ["text"]},
    },
    {
        "name": "video_picture_in_picture",
        "description": "Two shots on screen at once - a corner inset, or split side by side "
                       "or stacked. Use it for reaction shots, before/after, or a product "
                       "close-up over a wide.",
        "handler": t_picture_in_picture,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "The main clip."},
            "overlay": {"type": "string", "description": "The clip that goes on top or beside."},
            "layout": {"type": "string", "enum": ["corner", "split-h", "split-v"],
                       "description": "corner (default), split-h = side by side, "
                                      "split-v = stacked."},
            "corner": {"type": "string", "enum": ["top-left", "top-right",
                                                  "bottom-left", "bottom-right"]},
            "scale": {"type": "number", "description": "Inset width as a share of frame. Default 0.34."},
            "margin": {"type": "integer"},
            "audio": {"type": "string", "enum": ["base", "overlay", "both", "none"]},
            "output": {"type": "string"}},
            "required": ["path", "overlay"]},
    },
    {
        "name": "video_freeze",
        "description": "Hold a single frame, the way an editor lands a punchline or a product "
                       "shot. The still is spliced in as its own clip, so it holds for exactly "
                       "as long as asked.",
        "handler": t_freeze,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "at": {"type": "string", "description": "The frame to hold, e.g. 4.2 or 0:04."},
            "hold": {"type": "number", "description": "Seconds to hold it. Default 1.2."},
            "output": {"type": "string"}},
            "required": ["path", "at"]},
    },
    {
        "name": "video_reverse",
        "description": "Play a shot backwards. Sound is reversed with it unless told not to.",
        "handler": t_reverse,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "keep_audio": {"type": "boolean", "description": "Default true."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_slideshow",
        "description": "Build a clip out of photos, each drifting slowly so no frame sits "
                       "dead, dissolving between them. The drift alternates direction so a "
                       "run of stills does not pulse in one rhythm.",
        "handler": t_slideshow,
        "inputSchema": {"type": "object", "properties": {
            "images": {"type": "array", "items": {"type": "string"},
                       "description": "Photo files, in order."},
            "seconds_each": {"type": "number", "description": "Default 2.5."},
            "transition": {"type": "number", "description": "Dissolve length. Default 0.5."},
            "transition_style": {"type": "string", "enum": TRANSITIONS},
            "move": {"type": "number", "description": "How far it drifts, 0-0.3. Default 0.08."},
            "width": {"type": "integer"}, "height": {"type": "integer"},
            "fps": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["images"]},
    },
    {
        "name": "video_chroma_key",
        "description": "Drop a green or blue screen and put something else behind it. With no "
                       "background given the key is written to alpha and saved as WebM, since "
                       "mp4 cannot carry transparency.",
        "handler": t_chroma_key,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "background": {"type": "string", "description": "Image or video to put behind. "
                                                            "Leave out to get transparency."},
            "colour": {"type": "string", "description": "'green' (default), 'blue', or a hex "
                                                        "like 0x00d000."},
            "similarity": {"type": "number", "description": "How much of the colour range to "
                                                            "treat as screen. Default 0.30."},
            "blend": {"type": "number", "description": "Edge softness. Default 0.10."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_stabilise",
        "description": "Take the shake out of handheld footage. Measures the motion in one "
                       "pass then corrects it in a second, and crops in slightly to hide the "
                       "moving edges.",
        "handler": t_stabilise,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "strength": {"type": "number", "description": "0-1. Default 0.6. Higher smooths "
                                                          "more and crops more."},
            "zoom": {"type": "number", "description": "Crop-in factor, e.g. 1.05."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_grade_lut",
        "description": "Apply a .cube LUT - the way a house look is normally shipped between "
                       "editors. Can be dialled back below full strength.",
        "handler": t_grade_lut,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "lut": {"type": "string", "description": "Path to a .cube file."},
            "amount": {"type": "number", "description": "0-1, default 1."},
            "output": {"type": "string"}},
            "required": ["path", "lut"]},
    },
    {
        "name": "video_remove_logo",
        "description": "Cover a fixed watermark or channel bug by interpolating from the "
                       "pixels around it. Works on a flat background; smears over a busy one.",
        "handler": t_remove_logo,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "width": {"type": "integer"}, "height": {"type": "integer"},
            "output": {"type": "string"}},
            "required": ["path", "x", "y", "width", "height"]},
    },
    {
        "name": "video_info",
        "description": "Read a video's duration, resolution, fps, codecs and whether it has sound. Use this first when you need to know what you are working with.",
        "handler": t_video_info,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Full path to the video file."}},
            "required": ["path"]},
    },
    {
        "name": "list_media",
        "description": "List video and audio files in a folder so you can find the file the user means.",
        "handler": t_list_media,
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Folder to look in. Defaults to current folder."},
            "recursive": {"type": "boolean", "description": "Also search sub-folders."}}},
    },
    {
        "name": "video_trim",
        "description": "Cut a section out of a video. Give start plus either end or duration. Times accept seconds (12.5) or MM:SS (1:30).",
        "handler": t_trim,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start": {"type": "string", "description": "Where to start, e.g. 0, 12.5 or 1:30."},
            "end": {"type": "string", "description": "Where to stop."},
            "duration": {"type": "string", "description": "How long to keep, instead of 'end'."},
            "fast": {"type": "boolean", "description": "Copy without re-encoding: instant, but the cut may land on the nearest keyframe."},
            "output": {"type": "string"}},
            "required": ["path", "start"]},
    },
    {
        "name": "video_merge",
        "description": "Join two or more videos into one, in the order given. Clips of different sizes are scaled to match.",
        "handler": t_merge,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Video paths in play order."},
            "aspect": {"type": "string", "enum": list(ASPECTS), "description": "Force an output shape. Default: match the first clip."},
            "fps": {"type": "integer"},
            "output": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_resize",
        "description": "Change a video's size or shape - e.g. make a landscape video vertical 9:16 for TikTok/Reels/Shorts.",
        "handler": t_resize,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "aspect": {"type": "string", "enum": list(ASPECTS)},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "mode": {"type": "string", "enum": ["fill", "fit", "blur"],
                     "description": "fill = crop to fill (default), fit = black bars, blur = blurred background bars."},
            "sharpen": {"type": "number",
                        "description": "0-1. Left out, it is chosen from how far the picture "
                                       "is being stretched - a big upscale gets more."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_crop",
        "description": "Crop a rectangle out of the frame.",
        "handler": t_crop,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"},
            "x": {"type": "integer"}, "y": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["path", "width", "height"]},
    },
    {
        "name": "video_speed",
        "description": "Speed up or slow down a video, audio included. factor 2 = twice as fast, 0.5 = half speed.",
        "handler": t_speed,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "factor": {"type": "number"}, "output": {"type": "string"}},
            "required": ["path", "factor"]},
    },
    {
        "name": "video_extract_audio",
        "description": "Save a video's sound as an mp3, wav or m4a file.",
        "handler": t_extract_audio,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "format": {"type": "string", "enum": ["mp3", "wav", "m4a"]},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_mute",
        "description": "Remove all sound from a video.",
        "handler": t_mute,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "output": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "video_add_music",
        "description": "Add a background music or voice-over track to a video.",
        "handler": t_add_music,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "music": {"type": "string", "description": "Path to the audio file."},
            "volume": {"type": "number", "description": "Music loudness, 0-1. Default 0.3."},
            "keep_original_audio": {"type": "boolean", "description": "Mix over the original sound (default true) or replace it (false)."},
            "output": {"type": "string"}},
            "required": ["path", "music"]},
    },
    {
        "name": "video_subtitles",
        "description": "Burn an .srt or .ass subtitle file permanently into the picture.",
        "handler": t_subtitles,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "subtitles": {"type": "string", "description": "Path to .srt or .ass file."},
            "font_size": {"type": "integer"},
            "font": {"type": "string", "description": "Font name. Default Tahoma, which renders Thai correctly."},
            "output": {"type": "string"}},
            "required": ["path", "subtitles"]},
    },
    {
        "name": "video_auto_subtitles",
        "description": "Listen to the video and write subtitles automatically, then burn them in. "
                       "Works in Thai and ~90 other languages, runs offline on this PC, free. "
                       "The first run for a given model downloads it (small ~500MB, medium ~1.5GB, "
                       "large-v3 ~3GB). For Thai, 'medium' or 'large-v3' is much more accurate than 'small'.",
        "handler": t_auto_subtitles,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "language": {"type": "string", "description": "'th' for Thai, 'en' for English, or 'auto' to detect."},
            "model": {"type": "string", "enum": ["tiny", "base", "small", "medium", "large-v3"],
                      "description": "Bigger = more accurate but slower. Default small."},
            "translate_to_english": {"type": "boolean", "description": "Translate the speech into English subtitles."},
            "burn": {"type": "boolean", "description": "Burn into the picture (default true), or just save the .srt file."},
            "font_size": {"type": "integer"},
            "font": {"type": "string"},
            "max_chars_per_line": {"type": "integer"},
            "max_seconds_per_line": {"type": "number",
                                     "description": "Longest a single caption stays on screen. Default 3."},
            "max_lines": {"type": "integer",
                          "description": "Most lines one caption may show. Default 2 - beyond that "
                                         "it covers the picture, so the cue is split in time instead."},
            "sentence_gap": {"type": "number",
                             "description": "Pause length that starts a new caption. Default 0.45s."},
            "srt_output": {"type": "string"},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_find_problems",
        "description": "Scan a video and report the timestamps of bad parts: dead silence, black screen, "
                       "frozen picture and blurry/out-of-focus sections. Changes nothing - report only. "
                       "Use this first so the user can see what would be cut.",
        "handler": t_find_problems,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "check_silence": {"type": "boolean"}, "check_black": {"type": "boolean"},
            "check_freeze": {"type": "boolean"}, "check_blur": {"type": "boolean"},
            "silence_db": {"type": "integer", "description": "Loudness counted as silence, default -30 dB."},
            "min_silence": {"type": "number", "description": "Ignore silence shorter than this, default 0.8s."},
            "min_black": {"type": "number"}, "min_freeze": {"type": "number"},
            "blur_threshold": {"type": "number", "description": "Above this = blurry. Default 30 (sharp footage is ~5)."}},
            "required": ["path"]},
    },
    {
        "name": "video_auto_cut",
        "description": "Automatically remove the bad parts of a video - dead silence, black screen, frozen "
                       "picture, and optionally blurry sections - and stitch the good parts back together. "
                       "The original file is never modified.",
        "handler": t_auto_cut,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "remove_silence": {"type": "boolean", "description": "Default true."},
            "remove_black": {"type": "boolean", "description": "Default true."},
            "remove_freeze": {"type": "boolean", "description": "Default true."},
            "remove_blurry": {"type": "boolean", "description": "Default false - turn on to also drop out-of-focus shots."},
            "silence_db": {"type": "integer"}, "min_silence": {"type": "number"},
            "min_black": {"type": "number"}, "min_freeze": {"type": "number"},
            "blur_threshold": {"type": "number"},
            "padding": {"type": "number", "description": "Seconds of breathing room kept around each cut. Default 0.15."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_remove_silence",
        "description": "Jump-cut editing: cut out every silent pause so the video is tight and fast, "
                       "the way vloggers and podcasters edit. Keeps only the parts with sound.",
        "handler": t_remove_silence,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "silence_db": {"type": "integer", "description": "Default -30 dB. Use -40 for a quiet room."},
            "min_silence": {"type": "number", "description": "Shortest pause to cut, default 0.6s."},
            "padding": {"type": "number", "description": "Breathing room kept around speech. Default 0.15s."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_effect",
        "description": "Apply a look or effect to a video. Pass one effect or several to stack them.",
        "handler": t_effect,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "effect": {"anyOf": [{"type": "string", "enum": EFFECT_NAMES},
                                 {"type": "array", "items": {"type": "string", "enum": EFFECT_NAMES}}],
                       "description": "One effect name, or a list to stack in order."},
            "intensity": {"type": "number", "description": "0 to 1. Default 0.5."},
            "output": {"type": "string"}},
            "required": ["path", "effect"]},
    },
    {
        "name": "video_look_at",
        "description": "LOOK at the video - returns a grid of real frames as an image you can see. "
                       "Use this before choosing a style, colour or music, so the decision is based "
                       "on what the footage actually looks like instead of guessing.",
        "handler": t_look_at,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "frames": {"type": "integer", "description": "How many frames to sample, 2-16. Default 9."}},
            "required": ["path"]},
    },
    {
        "name": "video_preview_effect",
        "description": "Check whether an effect suits this footage BEFORE committing. Renders the "
                       "effect on one real second of the video and returns a before/after image to "
                       "look at. Saves nothing. If it does not fit, try another effect or intensity.",
        "handler": t_preview_effect,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "effect": {"anyOf": [{"type": "string", "enum": EFFECT_NAMES},
                                 {"type": "array", "items": {"type": "string", "enum": EFFECT_NAMES}}]},
            "intensity": {"type": "number"},
            "at": {"type": "string", "description": "Which moment to test. Default 40% in."}},
            "required": ["path", "effect"]},
    },
    {
        "name": "video_analyze",
        "description": "Measure the footage - brightness, contrast, colour strength, how much movement, "
                       "scene cuts, loudness - and suggest what would improve it. Changes nothing.",
        "handler": t_analyze,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "video_auto_style",
        "description": "Automatically fix a video's look by measurement: corrects dark or blown-out "
                       "exposure, flat contrast and washed-out colour. Optionally also applies a look. "
                       "If the footage already measures well it says so and changes nothing.",
        "handler": t_auto_style,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "look": {"type": "string", "enum": EFFECT_NAMES,
                     "description": "Optional style to apply on top of the correction."},
            "intensity": {"type": "number"},
            "passes": {"type": "integer", "description": "How many measure-correct-recheck rounds. Default 3."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "music_scan",
        "description": "Analyse a folder of music: tempo (BPM) and energy of every track, so you can "
                       "tell which would suit a given video.",
        "handler": t_music_scan,
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string"}}, "required": ["folder"]},
    },
    {
        "name": "video_add_music_auto",
        "description": "Pick background music that fits the video automatically. Measures how busy the "
                       "picture is, scores every track in a folder by energy, chooses the closest "
                       "match, loops or trims it to length, fades it in and out, and ducks it under "
                       "any speech. Reports the ranking so the choice can be overridden.",
        "handler": t_add_music_auto,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "music_folder": {"type": "string", "description": "Folder of candidate tracks."},
            "volume": {"type": "number", "description": "Music level, default 0.25."},
            "duck_under_speech": {"type": "boolean", "description": "Default true."},
            "fade": {"type": "number", "description": "Fade in/out seconds, default 1.5."},
            "output": {"type": "string"}},
            "required": ["path", "music_folder"]},
    },
    {
        "name": "sfx_library",
        "description": "Write the built-in sound-effect set to a folder: impact, sub_drop, whoosh, "
                       "swoosh, riser, pop, click, sparkle, thud. They are synthesised on this PC, "
                       "so there is nothing to download and nothing to licence.",
        "handler": t_sfx_library,
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string"}}, "required": ["folder"]},
    },
    {
        "name": "video_reframe",
        "description": "Reframe to another shape while KEEPING THE SUBJECT IN FRAME - the crop "
                       "follows the face instead of sitting in the middle. Use this rather than "
                       "video_export_pack when a person moves around the shot.",
        "handler": t_reframe,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "aspect": {"type": "string", "enum": list(ASPECTS), "description": "Default 1:1."},
            "smooth": {"type": "integer", "description": "How much to average the track. "
                                                         "Higher is calmer. Default 7."},
            "headroom": {"type": "number", "description": "Bias the crop upward so foreheads "
                                                          "are not cut. Default 0.08."},
            "sample_fps": {"type": "integer", "description": "Face checks per second. Default 4."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_export_pack",
        "description": "Send one finished edit out to every platform shape at once - vertical, "
                       "square, widescreen - reframing each sensibly, plus an optional short hook "
                       "cut taken from the liveliest stretch rather than just the opening.",
        "handler": t_export_pack,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "formats": {"type": "array", "items": {"type": "string", "enum": list(ASPECTS)},
                        "description": "Default 9:16, 1:1 and 16:9."},
            "hook_seconds": {"type": "number", "description": "Also cut a short teaser, e.g. 3."},
            "folder": {"type": "string", "description": "Where to write. Default an 'export' folder."}},
            "required": ["path"]},
    },
    {
        "name": "audio_scope",
        "description": "LOOK at the sound. Returns a waveform and spectrogram image, and reports "
                       "every moment where music or effects are louder than the voice. Use this "
                       "after adding music or sfx - loudness numbers alone cannot tell you whether "
                       "the mix buries the dialogue.",
        "handler": t_audio_scope,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "sound_identify",
        "description": "Listen to sounds and say what they actually ARE - typing, applause, a "
                       "whoosh, a ding - regardless of filename. Use it to label a folder of "
                       "downloaded effects without opening each one. Confidence under 40% is "
                       "reported as uncertain rather than presented as fact.",
        "handler": t_sound_identify,
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Folder of sounds to label."},
            "path": {"type": "string", "description": "Or a single file."}}},
    },
    {
        "name": "find_sound",
        "description": "Search your own sound folder by what things SOUND like rather than by "
                       "filename - 'whoosh', 'applause', 'typing'. Falls back to the filename "
                       "when the classifier has no matching category, and says which it used.",
        "handler": t_find_sound,
        "inputSchema": {"type": "object", "properties": {
            "folder": {"type": "string"},
            "description": {"type": "string", "description": "What you want, e.g. 'typing'."},
            "threshold": {"type": "number", "description": "Minimum confidence. Default 0.25."}},
            "required": ["folder", "description"]},
    },
    {
        "name": "sfx_demo",
        "description": "Build an audition reel - every sound played in turn with its name on "
                       "screen - so you can hear the set and pick what you want.",
        "handler": t_sfx_demo,
        "inputSchema": {"type": "object", "properties": {
            "sounds": {"type": "array", "items": {"type": "string", "enum": sorted(SFX_LIBRARY)},
                       "description": "Which to include. Default: all of them."},
            "gap": {"type": "number", "description": "Silence after each. Default 0.45s."},
            "output": {"type": "string"}}},
    },
    {
        "name": "video_add_sfx",
        "description": "Lay sound effects onto a video - at exact times, or automatically on every "
                       "shot change. Accepts single sounds or COMBOS, which stack several the way "
                       "an editor does: 'transition' is a whoosh leading into the cut with an impact "
                       "landing on it; 'reveal' is riser + impact + sparkle; 'drop' is reverse "
                       "cymbal into an 808.",
        "handler": t_add_sfx,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "sounds": {"type": "array", "description": "Explicit placements.",
                       "items": {"type": "object", "properties": {
                           "sound": {"type": "string",
                                     "enum": sorted(SFX_LIBRARY) + sorted(SFX_COMBOS)},
                           "at": {"type": "string", "description": "Time, e.g. 3.2 or 0:03."},
                           "gain": {"type": "number"}},
                           "required": ["sound", "at"]}},
            "sound_folder": {"type": "string",
                             "description": "Your own sound files. Names are then matched against "
                                            "the folder too - by filename, or by what they sound "
                                            "like, so 'typing' finds the right file."},
            "on_transitions": {"anyOf": [{"type": "boolean"}, {"type": "string"}],
                               "description": "true, or a sound/combo name, for every shot change."},
            "lead": {"type": "number", "description": "Start the sound this far before the cut. Default 0.25s."},
            "gain": {"type": "number"}, "mix": {"type": "number", "description": "Effect level. Default 0.95."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "music_generate",
        "description": "Synthesise a royalty-free music bed on this PC - chord pad, sub bass and an "
                       "optional soft pulse - mixed quietly so it sits under dialogue. Deliberately "
                       "simple background music, not a produced song.",
        "handler": t_music_generate,
        "inputSchema": {"type": "object", "properties": {
            "mood": {"type": "string", "enum": list(MUSIC_MOODS),
                     "description": "calm, uplifting, warm, tense or gentle."},
            "duration": {"type": "number", "description": "Seconds. Match your video's length."},
            "bpm": {"type": "number"},
            "pulse": {"type": "boolean", "description": "Soft kick on the beat. Default true."},
            "pulse_level": {"type": "number"},
            "target_lufs": {"type": "number", "description": "Default -20, i.e. under dialogue."},
            "output": {"type": "string"}},
            "required": ["duration"]},
    },
    {
        "name": "video_auto_edit",
        "description": "ONE COMMAND: turn raw clips into a finished piece. Finds every shot, trims "
                       "the dead air off each one, cuts on shot boundaries, levels the audio so the "
                       "joins do not jump, dissolves between shots, grades, optionally writes and "
                       "burns subtitles, fades top and tail, then quality-checks the result. "
                       "Originals are never modified.",
        "handler": t_auto_edit,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Raw clips, in the order they should play."},
            "preset": {"type": "string",
                       "description": "Apply a saved house style from brand_preset. Anything you "
                                      "pass alongside overrides it for this run."},
            "end_title": {"type": "string", "description": "Append an end card with this headline."},
            "end_subtitle": {"type": "string"},
            "end_background": {"type": "string"}, "end_text_color": {"type": "string"},
            "end_duration": {"type": "number"}, "end_logo": {"type": "string"},
            "style": {"type": "string", "enum": list(EDIT_STYLES),
                      "description": "Grade. Default 'cinematic'."},
            "subtitles": {"type": "string", "description": "'th', 'en' or 'auto' to transcribe and "
                                                           "burn captions. Omit for none."},
            "subtitle_style": {"type": "string", "enum": list(SUBTITLE_PRESETS)},
            "sfx": {"anyOf": [{"type": "boolean"},
                              {"type": "string", "enum": sorted(SFX_LIBRARY) + sorted(SFX_COMBOS)}],
                    "description": "Sound on every transition - true, a sound, or a combo like "
                                   "'transition' (whoosh into the cut + impact on it)."},
            "sfx_gain": {"type": "number", "description": "Default 0.95."},
            "music": {"type": "string", "enum": list(MUSIC_MOODS),
                      "description": "Generate and lay a music bed underneath, ducked under speech."},
            "music_level": {"type": "number", "description": "Default 0.22."},
            "subtitle_model": {"type": "string", "enum": ["tiny", "base", "small", "medium", "large-v3"]},
            "target_seconds": {"type": "number", "description": "Aim for roughly this length."},
            "proof": {"type": "boolean",
                      "description": "Fast rough render at 540px to judge the cut. Use this while "
                                     "iterating, then run again without it for the real export."},
            "plan_only": {"type": "boolean",
                          "description": "List the shots it found, with timings, and render nothing. "
                                         "Do this first when clips may be alternate takes."},
            "target_duration": {"type": "number",
                                "description": "How long the finished cut should be, in "
                                               "seconds. Without it the planner keeps every "
                                               "usable shot, which for four 10s clips is 35s "
                                               "of ad. Given one, it drops the weakest shots "
                                               "to fit - silence before dialogue - and says "
                                               "which and why."},
            "protect_shots": {"type": "array", "items": {"type": "integer"},
                              "description": "Shot numbers that must survive the trim to "
                                             "'target_duration' whatever they score - the "
                                             "product reveal, the joke, the thing you know "
                                             "matters and the machine cannot."},
            "respect_speech": {"type": "boolean",
                               "description": "Move any cut that lands inside a spoken word to "
                                              "the edge of it, so no line is severed. Default true."},
            "drop_repeats": {"type": "boolean",
                             "description": "Drop shots whose spoken line repeats an earlier "
                                            "shot's - alternate takes of the same scene. Default "
                                            "true; what was dropped is always reported."},
            "drop_shots": {"type": "array", "items": {"type": "integer"},
                           "description": "Shot numbers from the plan to leave out, e.g. [4, 5]."},
            "max_chars_per_line": {"type": "integer", "description": "Caption width. Default 18."},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "transition_duration": {"type": "number", "description": "Default 0.45."},
            "pacing": {"type": "string", "enum": ["editorial", "uniform"],
                       "description": "'editorial' (default) hard-cuts inside a scene and only "
                                      "dissolves between scenes, with the audio leading the "
                                      "picture. 'uniform' dissolves every join."},
            "audio_lead": {"type": "number",
                           "description": "How far the audio crosses ahead of the picture "
                                          "(the J-cut). Default 0.32s."},
            "match_colour": {"type": "boolean",
                             "description": "Balance the source clips to a common look before "
                                            "cutting, so no shot reads cooler or darker than the "
                                            "next. Default true."},
            "match_strength": {"type": "number", "description": "0-1.5. Default 1."},
            "accelerate": {"type": "boolean",
                           "description": "Trim the air after each line progressively tighter so "
                                          "the piece gathers pace, then hold the payoff. Default true."},
            "open_pad": {"type": "number", "description": "Air left after the first line. Default 0.55s."},
            "tight_pad": {"type": "number", "description": "Air left by the climax. Default 0.14s."},
            "hold_pad": {"type": "number", "description": "Air left on the final shot. Default 0.75s."},
            "cut_on_action": {"type": "boolean",
                              "description": "Move each cut onto a nearby peak of movement so the "
                                             "motion carries across the join. Default true."},
            "font": {"type": "string", "enum": list(SUBTITLE_FONTS),
                     "description": "Caption face. Narrow Thai faces fit more per line, and the "
                                    "caption width follows automatically."},
            "min_shot": {"type": "number", "description": "Drop shots shorter than this. Default 0.9s."},
            "shot_sensitivity": {"type": "number", "description": "Lower finds more shots. Default 8."},
            "aspect": {"type": "string", "enum": list(ASPECTS)},
            "fps": {"type": "integer"},
            "fade_in": {"type": "number"}, "fade_out": {"type": "number"},
            "output": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_check",
        "description": "Quality-check a finished render before publishing: black bars, crushed "
                       "shadows, blown highlights, clipping audio, wrong loudness, and volume "
                       "jumping between joined sections. Reports what passed and what did not. "
                       "Changes nothing.",
        "handler": t_check,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "video_burn_subtitles",
        "description": "Burn an existing .srt into the picture using a style preset, re-wrapping "
                       "the lines safely first. Use this rather than raw subtitle filters for Thai: "
                       "Thai has no spaces, so an unwrapped line silently runs off both edges.",
        "handler": t_burn_subtitles,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "subtitles": {"type": "string", "description": "Path to the .srt or .ass file."},
            "style": {"type": "string", "enum": list(SUBTITLE_PRESETS),
                      "description": "premium (default), bold, tiktok, minimal, caption, "
                                     "panel, brand, elegant, cinema."},
            "max_chars_per_line": {"type": "integer", "description": "Default 18 - Thai overflows above ~20."},
            "rewrap": {"type": "boolean", "description": "Re-wrap the lines. Default true."},
            "font": {"type": "string"}, "font_size": {"type": "integer"},
            "text_color": {"type": "string", "description": "Hex like #FFFFFF."},
            "box_color": {"type": "string", "description": "Hex for the panel behind the text."},
            "box_opacity": {"type": "integer", "description": "0 solid - 255 invisible. Default 160."},
            "bold": {"type": "boolean"}, "margin": {"type": "integer"},
            "output": {"type": "string"}},
            "required": ["path", "subtitles"]},
    },
    {
        "name": "download_asset",
        "description": "Download a media file from a URL YOU picked - music, a sound effect, a "
                       "logo, a font - into a folder. Only media is fetched; archives and "
                       "anything executable are refused. It cannot check licensing for you, so "
                       "choose assets whose terms you have read.",
        "handler": t_download_asset,
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Direct link to the file itself."},
            "folder": {"type": "string", "description": "Where to save it."},
            "filename": {"type": "string"},
            "max_mb": {"type": "number", "description": "Size ceiling, default 120 MB."}},
            "required": ["url", "folder"]},
    },
    {
        "name": "brand_preset",
        "description": "Save a house style - grade, caption look, sound, music, end card - under "
                       "a name, then reuse it so every video comes out consistent. "
                       "action: save | list | delete.",
        "handler": t_brand_preset,
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["save", "list", "delete"]},
            "name": {"type": "string"},
            "style": {"type": "string", "enum": list(EDIT_STYLES)},
            "subtitles": {"type": "string"},
            "subtitle_style": {"type": "string", "enum": list(SUBTITLE_PRESETS)},
            "subtitle_model": {"type": "string"},
            "max_chars_per_line": {"type": "integer"},
            "sfx": {"type": "string"}, "sfx_gain": {"type": "number"},
            "music": {"type": "string", "enum": list(MUSIC_MOODS)},
            "music_level": {"type": "number"},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "transition_duration": {"type": "number"},
            "aspect": {"type": "string", "enum": list(ASPECTS)},
            "fps": {"type": "integer"},
            "fade_in": {"type": "number"}, "fade_out": {"type": "number"},
            "end_title": {"type": "string", "description": "End card headline, e.g. โปรดติดตามตอนต่อไป"},
            "end_subtitle": {"type": "string"},
            "end_background": {"type": "string"}, "end_text_color": {"type": "string"},
            "end_duration": {"type": "number"}, "end_logo": {"type": "string"}}},
    },
    {
        "name": "video_transcript",
        "description": "Word-by-word transcript with exact timings, plus where the natural pauses "
                       "are. Use this to rewrite captions so they break on MEANING rather than on "
                       "pauses - the one thing automatic captioning cannot judge.",
        "handler": t_transcript,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "language": {"type": "string", "description": "'th', 'en' or 'auto'."},
            "model": {"type": "string", "enum": ["tiny", "base", "small", "medium", "large-v3"]},
            "words": {"type": "boolean", "description": "Include every word's timing. Default true."}},
            "required": ["path"]},
    },
    {
        "name": "cache_clear",
        "description": "Force analysis to run again. Shot detection, silence and loudness results "
                       "are cached per file, but each entry already includes the file's timestamp, "
                       "so editing a video invalidates its own cache automatically.",
        "handler": t_cache_clear,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "video_cover",
        "description": "Pick the strongest still from a video for the post thumbnail - sharpest, "
                       "best central contrast, skipping dissolves and the very start and end - "
                       "and return it as an image to look at.",
        "handler": t_video_cover,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "skip_edges": {"type": "number", "description": "Ignore this many seconds at each end."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_polish",
        "description": "The finishing details that separate an edited video from a produced one: "
                       "continuous ROOM TONE sampled from the clip's own quiet moments (digital "
                       "silence between lines is the clearest amateur tell), FILM GRAIN (which "
                       "also disguises shots of different resolutions), restrained colour with the "
                       "product hue pushed back up, an almost invisible PUSH-IN so no frame is "
                       "truly static, and voice EQ.",
        "handler": t_polish,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "room_tone": {"type": "boolean", "description": "Default true."},
            "room_tone_from": {"type": "string",
                               "description": "Sample the room from this file instead. A finished "
                                              "cut with music under it has no silence left to "
                                              "sample - point this at the raw footage."},
            "room_tone_level": {"type": "number", "description": "0-1. Default 0.5."},
            "grain": {"type": "number", "description": "0 to about 1. Default 0.55."},
            "product_colour": {"type": "string",
                               "enum": ["blue", "cyan", "red", "green", "yellow", "magenta"],
                               "description": "Your product's colour - everything else is pulled "
                                              "back so it stands out."},
            "desaturate": {"type": "number", "description": "Overall saturation. Default 0.86."},
            "product_lift": {"type": "number", "description": "How far to push the product hue back up."},
            "push_in": {"type": "number", "description": "Slow zoom across the clip. Default 0.018 (1.8%)."},
            "voice_eq": {"type": "boolean", "description": "Default true."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "kinetic_captions",
        "description": "Captions where each word lights up as it is spoken - the style used across "
                       "TikTok and Reels. Transcribes, maps real words to their times, and animates "
                       "them over the video. Handles Thai, where the recogniser only returns "
                       "sub-syllable fragments.",
        "handler": t_kinetic_captions,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "cues": {"type": "array",
                     "description": "Supply the wording yourself instead of transcribing: "
                                    "[{start, end, text}], newline inside text for a line "
                                    "break. Only the per-word timing is worked out. Use this "
                                    "to keep captions you have already approved, or to break "
                                    "lines on meaning.",
                     "items": {"type": "object", "properties": {
                         "start": {"type": "string"}, "end": {"type": "string"},
                         "text": {"type": "string"}}}},
            "language": {"type": "string", "description": "'th', 'en' or 'auto'."},
            "model": {"type": "string", "enum": ["tiny", "base", "small", "medium", "large-v3"]},
            "font": {"type": "string", "enum": list(SUBTITLE_FONTS)},
            "outline": {"type": "number",
                        "description": "Black stroke around each word, as a share of the font "
                                       "size. Default 0.075. Needed when 'panel' is transparent."},
            "glow": {"type": "number",
                     "description": "Halo around the live word, engine 'fast' only. "
                                    "1 is the default, 0 turns it off."},
            "engine": {"type": "string", "enum": ["remotion", "fast"],
                       "description": "'fast' draws everything - colour, lift and halo - with "
                                      "libass in one pass, about four times quicker, and is "
                                      "what you usually want. 'remotion' (default, for "
                                      "compatibility) renders every frame through a browser; "
                                      "reach for it only if you need the React component "
                                      "changed in ways ASS cannot express."},
            "accent": {"type": "string", "description": "Colour of the word being spoken."},
            "text_color": {"type": "string"},
            "panel": {"type": "string", "description": "CSS colour behind the text."},
            "font_scale": {"type": "number", "description": "1 is the default size."},
            "margin_bottom": {"type": "number", "description": "Height above the bottom, 0-1. Default 0.14."},
            "max_chars_per_line": {"type": "integer"},
            "max_lines": {"type": "integer", "description": "Default 2."},
            "max_seconds_per_line": {"type": "number"},
            "sentence_gap": {"type": "number"},
            "fps": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "motion_title",
        "description": "Animated title over the video - words spring in one after another, then "
                       "ease out. Real motion graphics via Remotion, not the static text drawtext "
                       "produces. Thai renders correctly.",
        "handler": t_motion_title,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "title": {"type": "string", "description": "The headline to animate."},
            "subtitle": {"type": "string"},
            "at": {"type": "string", "description": "When it appears. Default the start."},
            "duration": {"type": "number", "description": "Seconds on screen. Default 3."},
            "accent": {"type": "string", "description": "Hex for the subtitle and rule."},
            "text_color": {"type": "string"},
            "stagger": {"type": "integer", "description": "Frames between each word. Default 3."},
            "fps": {"type": "integer"},
            "output": {"type": "string"}},
            "required": ["path", "title"]},
    },
    {
        "name": "video_end_card",
        "description": "Append a branded end card - background colour, logo image, title and "
                       "subtitle - dissolving into it from the last frame.",
        "handler": t_end_card,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"}, "subtitle": {"type": "string"},
            "logo": {"type": "string", "description": "PNG logo, ideally transparent."},
            "logo_scale": {"type": "number", "description": "Logo width as a share of frame. Default 0.42."},
            "background": {"type": "string", "description": "Hex like #0d1b2a, or a colour name."},
            "text_color": {"type": "string"},
            "duration": {"type": "number", "description": "Card length in seconds. Default 2.5."},
            "transition": {"type": "string", "enum": TRANSITIONS},
            "transition_duration": {"type": "number"},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_join_smooth",
        "description": "Join clips with a transition (dissolve, wipe, slide...) instead of a hard "
                       "cut, crossfading the audio too. Use this for a premium feel, and to hide a "
                       "tightened cut so removed dead air does not read as a jump.",
        "handler": t_join_smooth,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Clips in play order."},
            "transition": {"type": "string", "enum": TRANSITIONS, "description": "Default 'fade' (dissolve)."},
            "duration": {"type": "number", "description": "Transition length in seconds. Default 0.5."},
            "audio_crossfade": {"type": "number",
                                "description": "Cross the sound over a longer window than the "
                                               "picture. Symmetric."},
            "audio_lead": {"type": "number",
                           "description": "Seconds the sound cuts BEFORE the picture. Positive "
                                          "makes a J-cut - you hear the next shot before you "
                                          "see it. Negative makes an L-cut - the last shot's "
                                          "sound runs on under the new picture. 0.2 to 0.5 is "
                                          "the usual range; it is the commonest move in "
                                          "dialogue editing."},
            "junctions": {"type": "array",
                          "description": "Per-join overrides, one per cut: "
                                         "{transition, duration, audio_lead}.",
                          "items": {"type": "object", "properties": {
                              "transition": {"type": "string", "enum": TRANSITIONS},
                              "duration": {"type": "number"},
                              "audio_lead": {"type": "number"}}}},
            "aspect": {"type": "string", "enum": list(ASPECTS)},
            "fps": {"type": "integer"},
            "output": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_fix_audio",
        "description": "Fix audio that is too quiet, too loud, or clipping/distorting. Normalises to "
                       "the -14 LUFS level social platforms expect and caps the peaks so nothing "
                       "distorts. Reports the before/after numbers. Video is copied untouched.",
        "handler": t_fix_audio,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "target_lufs": {"type": "number", "description": "Default -14 (TikTok/YouTube/IG standard)."},
            "true_peak": {"type": "number", "description": "Peak ceiling in dB, default -1.5."},
            "denoise": {"type": "boolean", "description": "Also reduce background hiss."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "match_colour",
        "description": "Balance several clips to a common look so they cut together without one "
                       "reading cooler or darker than the next. Matches to the median clip by "
                       "default, or to one you nominate.",
        "handler": t_match_colour,
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "reference": {"type": "integer", "description": "1-based clip to match everything to."},
            "strength": {"type": "number", "description": "0-1.5. Default 1."},
            "folder": {"type": "string"}},
            "required": ["paths"]},
    },
    {
        "name": "video_speed_ramp",
        "description": "Ease into slow motion at a moment and ease back out, the way an editor "
                       "emphasises a beat. A hard speed change reads as a glitch; the ramp is what "
                       "sells it. Output is silent.",
        "handler": t_speed_ramp,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "at": {"type": "string", "description": "The moment to slow into, e.g. 4.2 or 0:04."},
            "factor": {"type": "number", "description": "Slowest speed. 0.4 = 40%. Default 0.4."},
            "ramp": {"type": "number", "description": "Ease in/out seconds. Default 0.5."},
            "hold": {"type": "number", "description": "How long to stay slow. Default 0.6."},
            "fps": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["path", "at"]},
    },
    {
        "name": "video_smooth_slowmo",
        "description": "High-quality slow motion that invents in-between frames, so it stays smooth "
                       "instead of stuttering. Slower to process. Output is silent.",
        "handler": t_smooth_slowmo,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "factor": {"type": "number", "description": "0.5 = half speed, 0.25 = quarter speed."},
            "fps": {"type": "integer", "description": "Output frame rate, default 60."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_watermark",
        "description": "Overlay a logo or PNG image on the video.",
        "handler": t_watermark,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "image": {"type": "string"},
            "position": {"type": "string", "enum": list(POSITIONS)},
            "scale": {"type": "number", "description": "Logo width as a share of video width. Default 0.15."},
            "opacity": {"type": "number", "description": "0-1. Default 1."},
            "output": {"type": "string"}},
            "required": ["path", "image"]},
    },
    {
        "name": "video_add_text",
        "description": "Draw a text caption or title onto the video, optionally only between two times.",
        "handler": t_text,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "text": {"type": "string"},
            "position": {"type": "string", "enum": list(POSITIONS)},
            "font_size": {"type": "integer"}, "color": {"type": "string"},
            "start": {"type": "string"}, "end": {"type": "string"}, "output": {"type": "string"}},
            "required": ["path", "text"]},
    },
    {
        "name": "video_to_gif",
        "description": "Turn a video (or a slice of it) into an animated GIF.",
        "handler": t_to_gif,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "string"}, "duration": {"type": "string"},
            "fps": {"type": "integer"}, "width": {"type": "integer"}, "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_thumbnail",
        "description": "Save one frame of the video as a JPG image.",
        "handler": t_thumbnail,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "at": {"type": "string", "description": "Time to grab, e.g. 5 or 0:05."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_compress",
        "description": "Make the file smaller for uploading or sending, keeping it watchable.",
        "handler": t_compress,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "level": {"type": "string", "enum": ["light", "medium", "strong", "extreme"]},
            "max_width": {"type": "integer", "description": "Also shrink the picture to at most this many pixels wide."},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_rotate",
        "description": "Rotate or mirror a video - fixes sideways phone footage.",
        "handler": t_rotate,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"},
            "direction": {"type": "string", "enum": ["right", "left", "180", "flip-horizontal", "flip-vertical"]},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "video_fade",
        "description": "Fade the video in from black at the start and out to black at the end.",
        "handler": t_fade,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "fade_in": {"type": "number"}, "fade_out": {"type": "number"},
            "output": {"type": "string"}},
            "required": ["path"]},
    },
    {
        "name": "ffmpeg_raw",
        "description": "Escape hatch: run FFmpeg with your own argument list for anything the other tools do not cover. Pass args without the word 'ffmpeg'.",
        "handler": t_ffmpeg_raw,
        "inputSchema": {"type": "object", "properties": {
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "e.g. [\"-i\",\"in.mp4\",\"-vf\",\"hue=s=0\",\"out.mp4\"]"}},
            "required": ["args"]},
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- MCP plumbing
def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def reply_error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        version = params.get("protocolVersion") or DEFAULT_PROTOCOL
        reply(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Local, free video editing with FFmpeg on this PC. Nothing is uploaded. "
                "Always use full file paths. Call video_info first when the video's size, "
                "length or sound matters. Edited files land in an 'edited' folder next to "
                "the original; the original is never modified."
            ),
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        reply(rid, {})
        return

    if method == "tools/list":
        reply(rid, {"tools": [{"name": t["name"], "description": t["description"],
                               "inputSchema": t["inputSchema"]} for t in TOOLS]})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if not tool:
            reply(rid, {"content": [{"type": "text", "text": "Unknown tool: %s" % name}],
                        "isError": True})
            return
        try:
            text = tool["handler"](args)
            # Handlers may return plain text, or a ready-made content list (e.g. images).
            content = text if isinstance(text, list) else [{"type": "text", "text": text}]
            # One place to record every call, so nothing has to remember to log itself.
            if name not in ("edit_history", "cache_clear"):
                _log_edit(name, args, "".join(c.get("text", "") for c in content
                                              if c.get("type") == "text"))
            reply(rid, {"content": content})
        except ToolError as e:
            reply(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except KeyError as e:
            reply(rid, {"content": [{"type": "text", "text": "Missing required option: %s" % e}],
                        "isError": True})
        except Exception as e:
            reply(rid, {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}],
                        "isError": True})
        return

    if rid is not None:
        reply_error(rid, -32601, "Method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:
            if req.get("id") is not None:
                reply_error(req.get("id"), -32603, "Internal error: %s" % e)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
