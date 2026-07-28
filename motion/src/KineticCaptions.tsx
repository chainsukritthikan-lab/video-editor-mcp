import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";

export type Word = { t: string; s: number; e: number };
export type Cue = { s: number; e: number; lines: Word[][] };

export type KineticProps = {
  cues: Cue[];
  accent: string;
  textColor: string;
  panel: string;
  fontFamily: string;
  fontScale: number;
  marginBottom: number;
  outline?: number;
};

export const KineticCaptions: React.FC<KineticProps> = ({
  cues,
  accent,
  textColor,
  panel,
  fontFamily,
  fontScale,
  marginBottom,
  outline = 0.075,
}) => {
  const frame = useCurrentFrame();
  const { fps, height, width } = useVideoConfig();
  const t = frame / fps;

  const cue = cues.find((c) => t >= c.s && t < c.e);
  if (!cue) return <AbsoluteFill />;

  // Fade the block in and out so captions do not pop on and off.
  const fade = Math.min(
    interpolate(t, [cue.s, cue.s + 0.12], [0, 1], { extrapolateRight: "clamp" }),
    interpolate(t, [cue.e - 0.12, cue.e], [1, 0], { extrapolateLeft: "clamp" })
  );
  const size = height * 0.030 * fontScale;
  // A transparent panel still occupies its padding, which pushes the lines apart and
  // reads as two separate captions rather than one wrapped sentence.
  const bare =
    !panel || panel === "transparent" || panel === "none" ||
    /rgba\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*0?\.?0+\s*\)/.test(panel);

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: height * marginBottom,
        fontFamily,
        opacity: fade,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center",
                    gap: bare ? size * 0.04 : size * 0.22, maxWidth: width * 0.88 }}>
        {cue.lines.map((line, li) => (
          <div
            key={li}
            style={{
              display: "flex",
              flexWrap: "nowrap",
              alignItems: "baseline",
              background: bare ? "none" : panel,
              padding: bare ? `0 ${size * 0.1}px` : `${size * 0.16}px ${size * 0.42}px`,
              borderRadius: bare ? 0 : size * 0.28,
            }}
          >
            {line.map((w, wi) => {
              // The word being spoken right now lifts and takes the accent colour.
              const live = t >= w.s && t < w.e;
              const grow = live ? 1.09 : 1;
              return (
                <span
                  key={wi}
                  style={{
                    fontSize: size,
                    fontWeight: 700,
                    lineHeight: 1.42,
                    color: live ? accent : textColor,
                    transform: `scale(${grow})`,
                    transformOrigin: "center bottom",
                    display: "inline-block",
                    whiteSpace: "pre",
                    // With no panel behind it the text has to carry itself over
                    // confetti and white shirts, so it gets a real stroke rather than
                    // the soft shadow alone. paint-order keeps the stroke behind the
                    // fill instead of eating into the letterforms.
                    WebkitTextStroke: `${size * outline}px rgba(0,0,0,0.92)`,
                    paintOrder: "stroke fill",
                    textShadow: live
                      ? `0 0 ${size * 0.5}px ${accent}88, 0 ${size * 0.05}px ${size * 0.14}px rgba(0,0,0,0.5)`
                      : `0 ${size * 0.05}px ${size * 0.14}px rgba(0,0,0,0.5)`,
                  }}
                >
                  {w.t}
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
