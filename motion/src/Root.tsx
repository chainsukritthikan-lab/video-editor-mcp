import React from "react";
import { Composition } from "remotion";
import { AnimatedTitle, TitleProps } from "./AnimatedTitle";
import { KineticCaptions, KineticProps } from "./KineticCaptions";

// Each render targets a specific video, so duration and size arrive as props and the
// composition resizes itself. A fixed durationInFrames would reject any longer clip.
type Sized = { durationInFrames?: number; fps?: number; width?: number; height?: number };

const sizeFromProps = ({ props }: { props: Sized }) => ({
  durationInFrames: Math.max(1, Math.round(props.durationInFrames ?? 240)),
  fps: props.fps ?? 24,
  width: props.width ?? 1080,
  height: props.height ?? 1920,
});

const TITLE_DEFAULTS: TitleProps & Sized = {
  title: "โปรดติดตามตอนต่อไป",
  subtitle: "YOUR BRAND · TAGLINE",
  accent: "#6cabe2",
  textColor: "#ffffff",
  stagger: 3,
};

const KINETIC_DEFAULTS: KineticProps & Sized = {
  cues: [],
  accent: "#6cabe2",
  textColor: "#ffffff",
  panel: "rgba(14,20,26,0.62)",
  fontFamily: '"TH Krub", "Leelawadee UI", Tahoma, sans-serif',
  fontScale: 1,
  marginBottom: 0.14,
  outline: 0.075,
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="AnimatedTitle"
        component={AnimatedTitle}
        durationInFrames={72}
        fps={24}
        width={1080}
        height={1920}
        defaultProps={TITLE_DEFAULTS}
        calculateMetadata={sizeFromProps}
      />
      <Composition
        id="KineticCaptions"
        component={KineticCaptions}
        durationInFrames={240}
        fps={24}
        width={1080}
        height={1920}
        defaultProps={KINETIC_DEFAULTS}
        calculateMetadata={sizeFromProps}
      />
    </>
  );
};
