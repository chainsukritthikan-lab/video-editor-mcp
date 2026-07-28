---
name: miimuud-brand
description: House style for MiiMuuD (มี มู้ด) beverage ads - the settings, colours, caption look, story shape and voice that were arrived at by testing. Use whenever making, re-cutting or captioning anything for MiiMuuD, so a new ad matches the ones already made instead of starting from defaults.
---

# MiiMuuD house style

MiiMuuD (มี มู้ด) — aroma drink, 0% sugar, 250 mL. Tagline **DRINK YOUR MOOD**.
Current line: **CALM / Lavender**, and the bottle reads blue on screen.

Everything below was measured or verified while making the office ad, not chosen
from taste alone. Deviate when there is a reason, but know what you are leaving.

## Format

- **9:16, 1080×1920, 30 fps.** Vertical only — these run on TikTok and Reels.
- **20–25 seconds.** The four-beat spot lands at 24.9s. Past ~25s the middle sags.

## The story shape that works

Four beats, one spoken line each. Do not add a fifth.

1. **Problem** — the character is visibly worn down, in an ordinary place
2. **The try** — sceptical, picks it up, drinks
3. **The turn** — it lands; this is where the ad earns its keep
4. **Punchline + product** — the joke, then the bottle held clean

Give beat 3 the most room. Cut beats 1 and 4 tighter than feels comfortable.

## Captions

```
style        clean          (no panel - the box covers too much picture)
font         TH Chakra Petch
font_scale   2.0            (~115px; larger reads as shouting)
outline      0.105          white text over confetti needs a real stroke
accent       #7ec8ff        the word being spoken
margin       0.155 up from the bottom, clear of the platform buttons
```

Use `kinetic_captions` with **hand-written `cues`**, one word lighting up as it
is spoken. Pass the real speech spans as cue times and let the tool work out the
per-word timing — it lands within about 0.1s.

Write the line breaks yourself. Break on meaning: `กูรู้สึกโคตรฟิน / เลยวะเนี่ย`,
never `กูรู้สึกโคตร / ฟินเลยวะเนี่ย`. Two lines maximum.

## Voice

The scripts use casual, crude Thai — **กู**, **โคตร**, **วะ**. That is the register,
not an accident: it is how the audience actually talks and it is why the ads read
as funny rather than corporate. Keep it unless told otherwise.

It is still a brand decision. Raise it once per project, then stop asking.
`video_censor` can bleep specific words without a reshoot if the answer changes.

## Grade and sound

```
video_polish   grain 0.55, desaturate 0.88, product_colour "blue", push_in 0.012
loudness       -14 LUFS, true peak -1.5 dB
end card       #0d2b4a, "โปรดติดตามตอนต่อไป"
```

Grain is doing real work: the source clips arrive at mixed resolutions (304×540
next to 720×1280) and shared texture stops them looking like different sources.
Sharpen in proportion to how far each clip is being upscaled, not uniformly.

**Make the music turn.** A tense bed under beats 1–2, uplifting from the moment
he drinks, crossfaded at the pivot with `acrossfade=c1=qsin:c2=qsin`. One flat
bed across the whole thing is what makes an ad feel like a slideshow.

Four sound effects, no more: `reveal` on the product appearing, `hard_cut` on a
punch-in, `drop` on the turn, `sparkle` on the hero shot.

## Before delivering

Run `video_review` with the .srt. Then look at the contact sheet yourself — the
things that have gone wrong here (a stranded word, a caption over the hero shot,
a cut mid-word) were all found by eye.
