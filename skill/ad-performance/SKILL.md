---
name: ad-performance
description: Logging how a published video actually performed and reading what it says about the edit - views, watch-through, likes from TikTok, Instagram, Facebook or LINE. Use when the user reports numbers from a post, asks which of their ads did best, or asks what to change in the next one.
---

# Did the ad work

Everything else in the editor judges whether a cut is technically correct. This is
the only part that knows whether anyone watched it.

## Logging

The platform APIs need business verification and developer approval, so the
numbers come from the user reading them off the app. That is fine - it takes
them ten seconds and removes a whole category of setup.

```
ad_record(path=..., platform="tiktok", views=..., watch_through=...)
```

**Ask for `watch_through` above everything else.** Views measure the thumbnail,
the caption and the algorithm. The share of people who reached the end measures
the *edit* - which is the only part that can be changed here.

`avg_watch_seconds` is the next best thing if the app does not show a percentage.

Record it against the exact file that was posted, not a re-render. The tool
measures the cut itself - length, shot count, pace, how much of it is talking -
and those numbers have to belong to what people actually saw.

## Reading it

```
ad_insights(metric="watch_through")
```

Under five posts it ranks them and says there is nothing to conclude. **Leave it
there.** Do not narrate a story about two data points; the temptation is real and
the user will believe it. Say plainly that there is not enough yet and how many
more it needs.

Above five it reports rank correlations. Read them out honestly:

- **It shows what moved together, not what caused what.** A short cut and a good
  hook tend to arrive in the same video.
- **rho below about 0.5 either way is nothing.** Say "no clear link" and move on.
- A strong correlation on a lever the user never varied is a coincidence, not a
  finding. Check the spread before repeating it.

## What to do with a finding

If shorter cuts genuinely track better watch-through, that is a `target_duration`
for the next edit, not a rule about video in general. Feed it back:

```
video_auto_edit(paths=[...], target_duration=<what the data suggests>)
```

Then log that one too. Three or four rounds of this is worth more than any
amount of theorising about what the algorithm wants.

## The honest limit

This can tell you that shorter ads held attention better. It cannot tell you the
joke landed, that the actor was likeable, or that the product looked appetising -
and those may matter more. Treat it as one input among several, and say so when
reporting.
