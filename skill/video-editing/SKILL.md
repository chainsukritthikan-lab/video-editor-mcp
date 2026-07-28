---
name: video-editing
description: Editing video with the local video-editor connector - cutting an ad or social clip, burning captions (especially Thai), sound design, grading, and checking the result before delivering. Use whenever the task involves cutting, captioning, or finishing video with that connector.
---

# Editing video with the local connector

The connector has 77 tools and no taste. It measures; it cannot judge. The value
you add is the judging, so spend the time there and let the tools do the rest.

## Always, in this order

1. **Look before planning.** `video_look_at` on every clip. Filenames lie and
   durations tell you nothing about what is in the shot.
2. **Transcribe before cutting.** `video_transcript` gives word-level times.
   Cut *around* speech, never through it. Recognition is cached, so asking twice
   is free.
3. **Plan, then prune.** `video_auto_edit` with `plan_only: true` and a
   `target_duration`. It fits the running time and drops silent filler, but it
   scores dialogue above everything - so it will throw away a silent product
   reveal or a visual joke. Name those in `protect_shots` and they survive
   whatever they score. The opening and closing shots are already safe: an
   advert ends on the product.
4. **Render in this order.** Cut → `video_fix_audio` → `video_add_sfx` →
   music → `video_polish` → `video_fix_audio` again → captions.
   The second loudness pass is not redundant: music, effects and the polish
   pass all add level, and the peaks only clip once they are stacked.
5. **Review before delivering.** `video_review` with the .srt you burned, then
   `sound_faults`, then `video_check`. Three different questions: does the
   picture read, does the mix have a fault, are the numbers right. Look at the
   contact sheet `video_review` returns - that is where every caption bug in
   this project was actually found.

## Thai captions

- **Write the cues by hand.** The segmenter still mis-splits (บอกว่า reads as
  บอ + กว่า). Automatic breaks land on pauses; good breaks land on meaning.
- **Count visible width, not characters.** Tone marks and upper/lower vowels
  stack and take no width - `อารมณ์เปลี่ยนปั๊บ` is 17 characters and 11 columns
  wide. Each font's real limit is in `SUBTITLE_FONTS[font]["fits"]`.
- **Never end a line on เ แ โ ใ ไ.** They are written before the consonant they
  belong to, so a break after one strands it.
- **Two lines maximum.** Three reads as a wall.

## Fonts

Fifteen are registered, `fits` and `size` both measured by rendering rather than
read off the font. Nine are text faces; **TH Charm of AU, TH Charmonman and TH
Srisakdi are display faces** - titles and end cards, never running captions.
Never invent a `fits` figure: deriving it from metrics once gave TH Krub 24
characters when it really fits 20, and captions ran off both edges.

## Sound

- **-14 LUFS, peaks at -1.5 dB.** That is what the platforms expect.
- **`sound_faults` names what is wrong; it cannot say what is good.** It measures
  a boxy low-mid pile-up, piercing sibilance, mains hum and a lifted noise floor,
  each against figures measured from real material. Report the number with the
  verdict. Whether a voice is convincing or music suits the film is not in it,
  and saying otherwise is invention.
- **`music_find` only returns music that may legally go under an advert** - CC0,
  public domain, BY and BY-SA. NC forbids commercial use and ND forbids trimming
  a track to length; neither is visible on a download page. `music_fetch` writes
  the credit line to ATTRIBUTION.txt where one is owed.
- **Let the music turn with the story.** One bed at one mood is what makes a cut
  feel flat. Generate two and crossfade them at the pivot - the moment the
  product works, the joke lands, the problem is solved.
- **Equal-power crossfades** (`c1=qsin:c2=qsin`). Linear dips in the middle.

## Picture

- **Grain hides a resolution mismatch.** Shots from different sources stop
  looking like different sources once they share a texture. 0.55 is plenty;
  0.65 if the sources are far apart.
- **Sharpen in proportion to the upscale.** A 3.5x blow-up needs it; a 1.5x
  source does not, and treating them alike over-cooks the good footage.
- **A punch-in reads as a second camera.** `video_punch_in`, or crop-then-scale
  a section of a long static take. A hard cut to a tighter framing reads as a
  mistake; easing in over a few tenths reads as a move.

## Traps that have actually bitten

- **Filter arguments split on `,` and `:`.** Any computed expression or Windows
  path inside one breaks the whole graph. Use `esc_expr()` for expressions,
  `escape_filter_path()` **quoted** for paths (esc_expr doubles backslashes and
  breaks them), or run ffmpeg with `cwd=` set and pass a bare filename. This has
  caused more bugs here than anything else.
- **Never escape text for `drawtext` by hand - use `drawtext_of()`.** It writes
  the text to a file and sets `expansion=none`. Escaping inline meant "50% OFF"
  rendered as NOTHING AT ALL, because drawtext read the `%` as a directive.
- **Set thresholds from measurement, never from intuition.** Invented ones have
  now shipped twice and been wrong twice: a caption detector that saw captions in
  a close-up of teeth, and an audio checker that failed every file including the
  clean one. Measure a known-good example and a deliberately broken one, then put
  the threshold between them.
- **Probe the video stream, not the container.** The container reports the
  longer of the two streams, so audio that ran past the picture has silently
  collapsed a whole cut before.
- **Look at the frames.** Every caption bug in this project was found by
  grabbing frames and staring at them, never by a passing quality check.
