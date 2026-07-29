# -*- coding: utf-8 -*-
"""video_build does in one pass what the chain of tools did in eight.

Guards the three things that were actually broken while writing it: an empty
filter left in the video chain, the loudness measurement dragging the picture
along with it (which made it fail silently and land a dB off), and captions
quietly not being burned at all.
"""
import os, sys, io, json, subprocess
sys.path.insert(0, "C:/Users/Computer/Downloads/cluade code/video-editor-mcp")
import server

o = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
log = lambda *a: (o.write(" ".join(str(x) for x in a) + "\n"), o.flush())
D = server._tmpdir()
J = lambda n: os.path.join(D, n)
fails = []


def check(name, ok, detail=""):
    log("  %-34s %s  %s" % (name, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


# two short clips with sound, so the join, the J-cut and the mix all have work
for i, colour in enumerate(("red", "blue")):
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=%s:s=480x854:d=2:r=30" % colour,
                    "-f", "lavfi", "-i", "sine=f=%d:d=2" % (220 + 110 * i),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", J("bc%d.mp4" % i)], check=True)
srcs = [J("bc0.mp4"), J("bc1.mp4")]

base = dict(paths=srcs, transition="fade", duration=0.08,
            audio_lead=0.20, audio_crossfade=0.25,
            grain=0.55, desaturate=0.88, product_colour="blue",
            target_lufs=-14, true_peak=-1.5)

plain = server.t_build(dict(base, output=J("b_plain.mp4")))
capped = server.t_build(dict(
    base, captions=[{"start": 0.2, "end": 3.5, "text": "\u0e17\u0e14\u0e2a\u0e2d\u0e1a"}],
    font="Tahoma", font_scale=2.0, glow=1.0, accent="#7ec8ff",
    sfx=[{"sound": "reveal", "at": "1.0", "gain": 0.7}],
    output=J("b_cap.mp4")))

# 2 + 2 - 0.08 join
dur = server.video_duration_of(J("b_plain.mp4"))
check("duration after the join", abs(dur - 3.92) < 0.12, "%.2fs (want 3.92)" % dur)

check("loudness measured, not guessed", "measured then corrected" in plain,
      "" if "measured then corrected" in plain else plain.strip().splitlines()[-1][:60])

err = server._probe_stderr(J("b_plain.mp4"), ["-af", "loudnorm=print_format=json"])
m = json.loads(err[err.rindex("{"):][:err[err.rindex("{"):].index("}") + 1])
lu = float(m["input_i"])
check("lands on -14 LUFS", abs(lu + 14) < 0.8, "%.2f LUFS" % lu)

# captions must actually change pixels in the lower third
def band(path, name):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.0", "-i", path,
                    "-vf", "crop=480:200:0:600", "-frames:v", "1", J(name)], check=True)
    return J(name)

diff = server._probe_stderr(band(J("b_plain.mp4"), "np.png"),
                            ["-i", band(J("b_cap.mp4"), "cp.png"),
                             "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-"])
check("captions actually burned", "average:" in diff and
      float(diff.split("average:")[1].split()[0].replace("inf", "99")) < 45,
      diff.split("average:")[1].split()[0] if "average:" in diff else "no psnr")

check("no stray empty filter", "No such filter" not in plain + capped)

log("\n" + ("ALL OK" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
