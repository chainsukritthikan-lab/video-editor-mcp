"""Fill ~/Downloads/music/beds with real, instrumental background music.

Source: Jamendo tracks found through Openverse - real musicians, real instruments,
cleared for commercial use (cc0 / by / by-sa; credits go in ATTRIBUTION.txt).

Every track is checked before it is kept:
  - tagged instrumental, no vocal tags, no sad/dark/horror tags
  - Whisper listens to three slices - real words = it has singing, thrown out
  - YAMNet listens too - singing / speech / choir heard = thrown out
Kept tracks go in beds/CATALOGUE.json with tempo, mood and loudness, which the
auto edit reads to pick a bed.

Run:  python build_music_library.py [how_many]
"""
import io, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402  (main-guarded; importing does not start the MCP server)

OUT = os.path.join(os.path.expanduser("~"), "Downloads", "music", "beds")
CAT = os.path.join(OUT, "CATALOGUE.json")
REJ = os.path.join(OUT, "rejected.json")
LOG = os.path.join(OUT, "build.log")

# mood label -> searches. Moods are what the edit asks for.
QUERIES = {
    "happy":     ["happy upbeat instrumental", "cheerful ukulele", "fun upbeat acoustic",
                  "positive pop instrumental", "happy instrumental"],
    "energetic": ["energetic instrumental", "upbeat electronic instrumental",
                  "funk groove instrumental", "rock upbeat instrumental",
                  "hip hop beat instrumental", "dance instrumental"],
    "chill":     ["lofi chill beat", "chill hop instrumental", "lounge instrumental",
                  "relaxed acoustic guitar", "chill instrumental"],
    "inspiring": ["inspiring corporate", "motivational instrumental",
                  "uplifting piano strings", "cinematic uplifting", "inspiring instrumental",
                  "uplifting instrumental", "optimistic instrumental", "epic instrumental",
                  "hopeful piano", "corporate instrumental", "positive instrumental",
                  "building instrumental", "triumphant instrumental"],
    "warm":      ["warm acoustic instrumental", "soft piano background",
                  "gentle guitar peaceful", "peaceful instrumental"],
}
VOCAL_TAGS = {"female", "male", "vocal", "vocals", "voice", "voices", "singer", "singing",
              "vocoder", "rap", "choir", "spoken", "lyrics"}
BAD_TAGS = {"sad", "dark", "horror", "scary", "creepy", "melancholic", "melancholy",
            "dramatic", "angry", "christmas", "experimental", "noise", "drone",
            "metal", "opera"}
VOICE_LABELS = ("speech", "singing", "choir", "rapping", "narration", "conversation",
                "vocal music", "chant", "yodel")
MIN_S, MAX_S = 75, 420


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        print(line, flush=True)
    except Exception:
        pass
    with io.open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load(path, default):
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save(path, data):
    with io.open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def safe_name(s):
    s = re.sub(r"[^\w\s\-]", "", s, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:60] or "track"


def slice_wav(src, at, secs, rate=16000):
    out = os.path.join(server._tmpdir(), "mlb_%d_%d.wav" % (os.getpid(), int(at * 10)))
    subprocess.run([server.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y",
                    "-ss", "%.2f" % at, "-i", src, "-t", "%.2f" % secs,
                    "-ac", "1", "-ar", str(rate), out],
                   capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out


FILLER = re.compile(r"[\W_]*(thank you( for watching)?|thanks for watching|music|you|bye|"
                    r"subtitles by.*|la+|oh+|ah+|na+)[\W_]*", re.I)


def words_heard(src, total):
    """Whisper on three 25 s slices. An instrumental gives nothing or one hallucinated
    filler ('Thank you.'); a sung track gives real lines."""
    model = server._load_whisper("small")
    found = []
    for frac in (0.2, 0.45, 0.7):
        clip = slice_wav(src, max(0.0, total * frac), 25)
        segs, _info = model.transcribe(clip, vad_filter=True, beam_size=1,
                                       condition_on_previous_text=False)
        for s in segs:
            if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0:
                continue
            t = s.text.strip()
            if FILLER.fullmatch(t):
                continue
            found.extend(re.findall(r"\w+", t, flags=re.UNICODE))
    return found


def voices_heard(src, total):
    hits = {}
    for frac in (0.2, 0.5, 0.8):
        clip = slice_wav(src, max(0.0, total * frac - 6), 12)
        try:
            for lbl, sc in server.listen_to(clip, 12.0):
                if any(v in lbl.lower() for v in VOICE_LABELS):
                    hits[lbl] = max(hits.get(lbl, 0.0), sc)
        except server.ToolError:
            pass
    return {k: round(v, 2) for k, v in hits.items() if v >= 0.15}


def describe(path):
    txt = server.t_music_describe({"path": path})

    def grab(key):
        m = re.search(r"%s\s*:\s*(.+)" % key, txt)
        return m.group(1).strip() if m else ""
    return {"sounds": grab("Sounds like"), "tempo": grab("Tempo"),
            "colour": grab("Colour"), "shape": grab("Shape"), "swing": grab("Swing")}


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    os.makedirs(OUT, exist_ok=True)
    cat, rej = load(CAT, []), load(REJ, {})
    have = {c["id"] for c in cat}
    per_mood = max(1, -(-want // len(QUERIES)))
    log("start: have %d, want %d" % (len(cat), want))

    for mood, queries in QUERIES.items():
        for q in queries:
            if len(cat) >= want or sum(c["mood"] == mood for c in cat) >= per_mood:
                break
            try:
                res = server._openverse(q, 80).get("results", [])
            except server.ToolError as e:
                log("search failed '%s': %s" % (q, e))
                time.sleep(30)
                continue
            for r in res:
                if len(cat) >= want or sum(c["mood"] == mood for c in cat) >= per_mood:
                    break
                rid = r.get("id")
                if not rid or rid in have or rid in rej:
                    continue
                tags = {t["name"].lower() for t in (r.get("tags") or [])}
                title = r.get("title") or "untitled"
                secs = (r.get("duration") or 0) / 1000.0
                lic = (r.get("license") or "").lower()
                why = None
                if lic not in server.MUSIC_OK:
                    why = "license %s" % lic
                elif r.get("source") != "jamendo":
                    why = "not jamendo"
                elif "instrumental" not in tags and "instrumental" not in title.lower():
                    why = "not tagged instrumental"
                elif tags & VOCAL_TAGS:
                    why = "vocal tag %s" % sorted(tags & VOCAL_TAGS)
                elif tags & BAD_TAGS:
                    why = "mood tag %s" % sorted(tags & BAD_TAGS)
                elif not MIN_S <= secs <= MAX_S:
                    why = "length %.0fs" % secs
                if why:
                    rej[rid] = why
                    continue

                artist = r.get("creator") or "unknown"
                fname = "%s - %s.mp3" % (safe_name(artist), safe_name(title))
                path = os.path.join(OUT, fname)
                try:
                    req = urllib.request.Request(
                        r["url"], headers={"User-Agent": "video-editor-mcp/1.0"})
                    with urllib.request.urlopen(req, timeout=90) as fh:
                        data = fh.read()
                    with open(path, "wb") as w:
                        w.write(data)
                    total = server.duration_of(path)
                    words = words_heard(path, total)
                    if len(words) >= 5:
                        raise ValueError("singing heard: %s" % " ".join(words[:10]))
                    voice = voices_heard(path, total)
                    if voice:
                        raise ValueError("voice heard: %s" % voice)
                    d = describe(path)
                    if any(b in d["sounds"].lower() for b in ("scary", "silence", "noise")):
                        raise ValueError("sounds like %s" % d["sounds"])
                    lufs = server._loudness(path)
                except Exception as e:
                    rej[rid] = str(e)[:160]
                    log("  x %s - %s" % (title[:40], str(e)[:110]))
                    if os.path.isfile(path):
                        os.remove(path)
                    save(REJ, rej)
                    continue

                cat.append(dict(id=rid, file=fname, path=path, mood=mood, title=title,
                                artist=artist, license=lic,
                                page=r.get("foreign_landing_url") or "",
                                seconds=round(total, 1), lufs=lufs, instrumental=True,
                                tags=sorted(tags), **d))
                have.add(rid)
                save(CAT, cat)
                with io.open(os.path.join(OUT, "ATTRIBUTION.txt"), "a", encoding="utf-8") as fh:
                    fh.write('"%s" by %s - %s (%s)\n'
                             % (title, artist, lic.upper(), r.get("foreign_landing_url") or ""))
                log("  + [%s] %s - %s  %s  %.0fs"
                    % (mood, artist[:20], title[:35], d["tempo"], total))
            save(REJ, rej)
    log("done: %d tracks in %s" % (len(cat), OUT))


if __name__ == "__main__":
    main()
