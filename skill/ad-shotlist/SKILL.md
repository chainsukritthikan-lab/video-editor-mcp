---
name: ad-shotlist
description: Planning which few clips to generate when AI video is rationed - three 8-second clips a day, and a 30-second ad needs ten to fourteen shots. Use BEFORE spending a generation, and for getting more shots out of clips already made.
---

# Spending three clips a day

The constraint is real and it shapes everything: **three generated clips, eight
seconds each. Twenty-four seconds of material a day.** A thirty-second advert
wants ten to fourteen shots. So the job is not "generate more" - it is to spend
each generation on something that cannot be made any other way, and then take
every shot the footage will give.

## Generate only what cannot be faked

Before asking for a clip, ask what it gives that cropping, slowing or reversing
existing footage cannot:

**WORTH A GENERATION**
- A new face, place or product that is not on screen anywhere yet
- A physical event: a pour, a splash, a hand opening a bottle, a door
- A change of expression that the story turns on
- A different time of day or location

**NOT WORTH A GENERATION** - these come free from `clip_stretch`:
- A closer view of something already filmed (a punch-in reads as a second camera)
- A held beat (slow motion)
- A pause for emphasis (freeze)
- The same action from its start rather than its middle (a second section)

A generation spent on "the same thing but closer" is a generation wasted.

## Then take everything the footage will give

`clip_stretch` on the clips you have. An eight-second take yields about five:
two sections split where the picture changes most, a punch-in on the busiest
moment, a held beat at half speed, and a freeze on the peak. Three clips become
roughly fifteen options.

**They are OPTIONS, not fifteen independent shots.** A punch-in covers the same
seconds as the section it came from, so using both reads as a repeat, not as
coverage. Choose one per moment.

**The punch must be a real one.** Below about 1.4x the eye reads a tighter
framing as the same shot slightly bigger, which looks like a mistake rather than
a cut. 1.6x is the working default.

## Ordering what you have

- **Open on the problem, close on the product.** Everything between is the turn.
- **The first second decides whether anyone watches the rest.** Put the most
  arresting frame you own there, whatever order it happened in.
- Do not use two shots from the same second of the same clip.
- Vary the size: wide, then tight, then wide. Three shots at the same distance
  read as one long shot badly joined.

## Traps

- **Transcribe every generated clip before trusting it.** `seedance_2_0` returns
  Thai-sounding gibberish while looking perfectly fine. Only `gemini_omni` and
  `wan2_7` speak Thai correctly.
- **The content filter rejects the brand's crude register** (กู, ว่ะ, โคตร).
  Write generation prompts in clean Thai; the finished ad can carry the slang in
  its captions.
- **`video_references` makes generations fail** - three in a row came back nsfw
  or failed with it, and the identical prompt without it worked first time.
- Rejected and failed jobs are not charged, but they still cost a slot in the
  day if the cap is per generation rather than per success. Queue the whole
  shot list the moment the cap resets.
