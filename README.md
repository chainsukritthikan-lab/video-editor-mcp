# Video Editor Connector (free, local)

A connector that lets Claude edit your videos by just asking in chat.

- **Free forever** — no API key, no subscription, no credits.
- **Runs on your PC** — your videos are never uploaded anywhere.
- **Powered by FFmpeg** — the same engine used by YouTube, VLC and OBS.

Originals are **never modified**. Every result is saved into an `edited` folder
next to the source video.

---

## Install

**1. Get the code**

```bash
git clone https://github.com/chainsukritthikan-lab/video-editor-mcp.git
```

**2. Required — Python 3.10+ and FFmpeg**

```bash
winget install --id Gyan.FFmpeg -e
```

macOS: `brew install ffmpeg`. Linux: your package manager. The server itself
imports nothing outside the standard library, so there is no `pip install` step
for the core.

**3. Optional — only for the tools that need them**

| Install | Unlocks |
|---|---|
| `pip install faster-whisper` | subtitles, transcripts, kinetic captions |
| `pip install pythainlp` | Thai line breaks that land on real word boundaries |
| `pip install numpy scipy` | beat detection, visual QA |
| `pip install mediapipe` | face-following reframe, sound identification |
| `pip install edge-tts` | voice-over |
| `npm install` in `motion/` | the animated caption and title renderer |

Each tool tells you what is missing if you call it without the dependency.

**4. Point Claude at it**

Settings → **Connectors** → **Add custom / local connector**:

- **Name:** `video-editor`
- **Command:** `python`
- **Arguments:** the full path to `server.py`

Or add it to `~/.claude.json`. Putting it at the **top level** rather than under
`projects` makes it available in every session, not just one folder:

```json
{
  "mcpServers": {
    "video-editor": {
      "command": "python",
      "args": ["/full/path/to/video-editor-mcp/server.py"]
    }
  }
}
```

Restart Claude afterwards.

## What you can say

> "Cut my video C:\Videos\clip.mp4 from 0:10 to 0:45"
> "Make this vertical for TikTok"
> "Join these three clips and add fade in/out"
> "Add my logo bottom-right and burn in the subtitles"
> "Compress it so I can send it on LINE"
> "Turn seconds 5-8 into a GIF"

---

## Post everywhere from one edit

`video_export_pack` reframes a finished cut into every platform shape at once and can cut a
short hook from the **liveliest** stretch rather than assuming the opening is the best bit:

```
9:16   1080x1920  native
1:1    1080x1080  blurred surround
4:5    1080x1350  cropped to fill
16:9   1920x1080  blurred surround
hook   3.0s from 0.50s (busiest stretch)
```

Shapes close to the source get cropped; much wider ones get a blurred surround rather than
having the composition butchered.

## Seeing the sound

`audio_scope` returns a **waveform and spectrogram image**, plus every moment where music or
effects are louder than the voice. Loudness numbers cannot answer "does this bury the
dialogue" — masking is about which band wins at a given instant.

Tested against a deliberately over-loud mix: a properly ducked cut reports *"nothing is
burying the voice"*, while the same cut with music at ×1.6 flags three moments with the low
end 21–36 % above the speech band.

Use it after any music or sfx change. And trust the numbers over the picture — a spectrogram
makes natural speech pauses look like dropouts.

## Why these are synthesised, not downloaded

Free sound libraries mix CC0, CC-BY and CC-BY-**NC** files in the same listings, and an
automated download cannot reliably tell which licence applies to a given file. Putting the
wrong one in a brand commercial is a legal problem, not a stylistic one. Everything here is
generated on this PC, so it is unambiguously yours to use.

What actually makes an effect sound produced is not the waveform — it is **layers**
(sub + body + transient), a **decay tail** in a room, and **stereo width**. Each effect is
built that way:

```
impact spectral spread   1391 → 5487
riser tail after peak    0.13s → 0.90s
```

**Width comes from the reverb only; the dry hit stays centred.** Widening the dry signal with
a delay comb-filters against itself, and phone speakers are mono — measured up to **11.9 dB**
of cancellation that way, enough to make `thud` vanish on a phone. Now the worst case is
**0.2 dB**.

## Sound design — synthesised, not sampled

There is no sound library to download and nothing to licence: every effect is generated
from a maths expression by FFmpeg on this PC.

| sound | how it is made | use it for |
|---|---|---|
| `impact` | 150 Hz sine falling to sub, 4.5× decay | trailer boom |
| `sub_drop` | 90 Hz dropping over 2s | under a reveal |
| `whoosh` | noise through a gaussian, banded at 700 Hz | on a cut |
| `swoosh` | shorter, brighter, 1.4 kHz | fast cut |
| `riser` | noise climbing as t²·⁶ | into the payoff |
| `pop` | 760 Hz, 42× decay | text appearing |
| `click` | 10 ms noise transient | UI tick |
| `sparkle` | 3.1 / 4.7 / 6.3 kHz partials | product shine |
| `thud` | 70 Hz, low-passed to 220 | something lands |

`sfx_library` writes them all to a folder as .wav. `video_add_sfx` places them by time, or
automatically on every shot change — the detector steps its sensitivity down until it finds
transitions, because **a dissolve is invisible to hard-cut detection**.

`music_generate` synthesises a bed: chord pad, sub bass, optional pulse, mixed at −20 LUFS
so it sits under dialogue. Five moods (calm, uplifting, warm, tense, gentle). Verified in
the spectrum — an "uplifting" bed's strongest partials land on 196/247/294 Hz (G3, B3, D4),
real chord tones. It is deliberately simple background music, not a produced song.

## The one command

```
video_auto_edit(paths=[...], style="cinematic", subtitles="th")
```

Finds every shot, trims the dead air off each one, cuts **on** shot boundaries, levels the
audio so the joins do not jump, dissolves between shots, grades, writes and burns subtitles,
fades top and tail, then quality-checks the result. Saves the .srt next to the video so you
can correct a caption and re-burn it.

Run it with `plan_only: true` first — it lists the shots it found with timings and renders
nothing:

```
 1. Vertical_cinematic_video_.mp4        0.00 -  3.17  (3.17s)
 2. Vertical_cinematic_video_.mp4        4.74 -  7.92  (3.18s)
 ...
 4. and_next_scene_i_want_it_to_be.mp4   0.00 -  2.34  (2.34s)
```

Then re-run with `drop_shots: [4]`. **This matters when clips are alternate takes** — nothing
can tell that two takes repeat the same line, so you drop the repeat yourself.

## Quality guard

`video_check` inspects a finished render before you publish it:

- black bars / letterboxing (measured to the pixel)
- crushed shadows — a vignette that flattens dark areas to black
- blown highlights
- clipping audio, wrong loudness
- **volume jumping between joined sections** (IQR of short-term loudness; under 5 dB passes)

Tested: passes a properly levelled cut, flags an unlevelled join at 7.0 dB, and measured a
deliberate 160 px letterbox as 158 px.

### Thai captions

Thai writes no spaces, so a long line cannot be auto-wrapped by the subtitle renderer — it
just runs off both edges. The editor wraps every caption itself, never splits a combining
mark, and never ends a line on a leading vowel (เ แ โ ใ ไ).

For **word-accurate** wrapping, install a Thai segmenter — optional, the editor works without
it:

```bash
pip install pythainlp
```

Without it, breaks land on syllables rather than words (`ขึ้น` may split as `ขึ้` / `น`).

## Tools that decide for you

These are what make it "automatic" — the connector can **see** and **measure** the footage,
so the choice of style or music is based on the actual video, not a guess.

| Tool | What it does |
|---|---|
| `video_look_at` | Returns a grid of real frames **as an image Claude can look at**. This is how it knows what your video actually contains. |
| `video_preview_effect` | Renders an effect on one real second and returns a **before/after image**. Saves nothing — so a look can be checked and rejected before committing. |
| `video_analyze` | Measures brightness, contrast, colour, movement, scene cuts, loudness — and says what would improve it. |
| `video_auto_style` | Fixes exposure/contrast/colour by the numbers, then **re-measures its own result and corrects again** until it lands. Says "nothing to fix" on good footage. |
| `music_scan` | Reports BPM and energy for every track in a folder. |
| `video_add_music_auto` | Measures how busy the picture is, scores every track, picks the closest energy match, loops/trims to length, fades in and out, and **ducks the music under speech**. Shows the ranking so you can override. |

Measured example — the same three-track folder, two different videos:

```
calm video  (movement 4.6,  0.0 cuts/min) -> chose calm_ambient.mp3    (70 BPM,  energy 0.05)
busy video  (movement 11.2, 7.5 cuts/min) -> chose energetic_fast.mp3 (152 BPM, energy 0.41)
```

And a dark, flat clip through `video_auto_style`:

```
brightness  52 -> 110      (very dark   -> well exposed)
contrast    98 -> 127      (flat        -> normal)
colour      28 ->  55      (washed out  -> normal)
```

## Smart tools

| Tool | What it does |
|---|---|
| `video_auto_subtitles` | Listens to the video and writes subtitles itself — Thai + ~90 languages, offline, free. Can also translate to English. |
| `video_find_problems` | Reports timestamps of dead silence, black screen, frozen picture and blurry shots. Changes nothing. |
| `video_auto_cut` | Removes those bad parts automatically and stitches the good parts back together. |
| `video_remove_silence` | Jump-cut editing — cuts every silent pause, the way vloggers edit. |
| `video_effect` | 18 looks: cinematic, vintage, black_and_white, sepia, vivid, warm, cold, vignette, sharpen, dreamy, film_grain, vhs, glitch, night_vision, blur_background, zoom_in, shake, mirror. Stack several at once. |
| `video_smooth_slowmo` | Slow motion with invented in-between frames, so it glides instead of stuttering. |

### Thai subtitles

Rendering is handled — the default font is **Tahoma**, which draws Thai vowels and tone
marks correctly. `font: "Leelawadee UI"` also works.

Accuracy depends on the speech model you pick:

| model | size | Thai quality |
|---|---|---|
| `base` | ~145 MB | rough — gets the gist, misspells words |
| `small` | ~500 MB | usable |
| `medium` | ~1.5 GB | good |
| `large-v3` | ~3 GB | **downloaded and ready — use this one** |

Measured on this PC with `large-v3`: a 9.3 s Thai clip took 21 s, so roughly
**2× the length of the video**. A 5-minute video ≈ 10 minutes of processing.

The model downloads by itself the first time you use it, then stays cached.
`base` and `small` are also cached if you ever want speed over accuracy.

## The 19 basic tools

| Tool | What it does |
|---|---|
| `video_info` | Duration, size, fps, codec, has sound? |
| `list_media` | Find video/audio files in a folder |
| `video_trim` | Cut a section (`fast: true` = instant, no re-encode) |
| `video_merge` | Join clips; auto-scales mismatched sizes |
| `video_resize` | 9:16, 16:9, 1:1, 4:5… modes: `fill` / `fit` / `blur` |
| `video_crop` | Crop a rectangle |
| `video_speed` | 2x faster, 0.5x slower — audio pitch-corrected |
| `video_extract_audio` | Save sound as mp3 / wav / m4a |
| `video_mute` | Strip the sound |
| `video_add_music` | Background music, mix or replace, loops to fit |
| `video_subtitles` | Burn in .srt / .ass permanently |
| `video_watermark` | Overlay a logo, 7 positions, opacity |
| `video_add_text` | Draw a caption, optionally only between two times |
| `video_to_gif` | Video → animated GIF (high-quality palette) |
| `video_thumbnail` | Grab one frame as JPG |
| `video_compress` | light / medium / strong / extreme + max width |
| `video_rotate` | Fix sideways phone video, or mirror it |
| `video_fade` | Fade in from / out to black, video + audio |
| `ffmpeg_raw` | Escape hatch — any FFmpeg command Claude needs |

Times accept `12.5` seconds or `1:30` (MM:SS).

## If it stops working

- **"FFmpeg is not installed"** → restart Claude fully (PATH is only read at startup).
- Connector doesn't appear → check the path in the config matches this folder exactly.
- Test it by hand:
  ```
  python "<path-to>\video-editor-mcp\video-editor-mcp\server.py"
  ```
  It should sit there silently waiting. Ctrl+C to quit. Any crash text = a real problem.
