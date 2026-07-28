import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type TitleProps = {
  title: string;
  subtitle: string;
  accent: string;
  textColor: string;
  // Words rise in sequence rather than all at once - staggering is what reads as
  // designed rather than as a single fading block.
  stagger: number;
};

export const AnimatedTitle: React.FC<TitleProps> = ({
  title,
  subtitle,
  accent,
  textColor,
  stagger,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, height } = useVideoConfig();

  // Everything eases out together at the end so the overlay leaves cleanly.
  const out = interpolate(
    frame,
    [durationInFrames - fps * 0.6, durationInFrames - 1],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  const words = title.split(" ").filter(Boolean);

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        // Thai needs a face that carries its vowels and tone marks properly.
        fontFamily: '"TH Krub", "Leelawadee UI", Tahoma, sans-serif',
        opacity: out,
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: `0 ${height * 0.018}px`,
          maxWidth: "86%",
        }}
      >
        {words.map((word, i) => {
          const enter = spring({
            frame: frame - i * stagger,
            fps,
            config: { damping: 200, mass: 0.6 },
          });
          return (
            <span
              key={`${word}-${i}`}
              style={{
                fontSize: height * 0.062,
                fontWeight: 700,
                color: textColor,
                lineHeight: 1.35,
                opacity: enter,
                transform: `translateY(${(1 - enter) * height * 0.05}px)`,
                textShadow: "0 4px 24px rgba(0,0,0,0.55)",
              }}
            >
              {word}
            </span>
          );
        })}
      </div>

      {subtitle ? (
        <div
          style={{
            marginTop: height * 0.022,
            fontSize: height * 0.026,
            letterSpacing: height * 0.0022,
            color: accent,
            fontWeight: 600,
            opacity: spring({
              frame: frame - words.length * stagger - 4,
              fps,
              config: { damping: 200 },
            }),
            textShadow: "0 2px 16px rgba(0,0,0,0.5)",
          }}
        >
          {subtitle}
        </div>
      ) : null}

      <div
        style={{
          marginTop: height * 0.018,
          height: Math.max(2, height * 0.0035),
          width: `${interpolate(
            spring({ frame: frame - 6, fps, config: { damping: 200 } }),
            [0, 1],
            [0, 26]
          )}%`,
          background: accent,
          borderRadius: 99,
        }}
      />
    </AbsoluteFill>
  );
};
