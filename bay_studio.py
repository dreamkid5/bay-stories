#!/usr/bin/env python3
"""
BAY STORIES — script-driven narration video studio.

Give it a script; it returns a finished MP4 in the viral karaoke-caption
style: full-bleed cinematic plates, heavy uppercase captions centred in
frame, and the word currently being spoken highlighted in a coloured pill.

    python3 bay_studio.py --script my_story.txt --out ~/Desktop/out.mp4

Script format — a title line, then `##` to start each new scene:

    # I Raised My Daughters Alone For 15 Years
    ## man sitting alone on a couch late at night, single lamp, melancholy
    Fifteen years. That is how long I gave everything I had.
    ## tired father in a diner kitchen at night, worn uniform
    I worked two jobs. Sometimes three.

The `##` line is the image prompt for that scene; the lines under it are the
narration. Captions are never authored separately — they are built from the
exact words sent to the voice engine, positioned with the word-level timings
that engine reports, so audio and captions cannot drift apart.

Cron (a video every morning at 07:00):
    0 7 * * * /usr/bin/python3 /private/tmp/bay_v2/bay_studio.py \
        --script /path/story.txt --out /path/out_$(date +\\%F).mp4 >> /tmp/bay.log 2>&1
"""
import argparse, asyncio, os, random, re, subprocess, sys, textwrap
import urllib.parse, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

# ── look ──────────────────────────────────────────────────────────────
W, H, FPS = 1280, 720, 30
VOICE     = "en-US-ChristopherNeural"
RATE      = "-6%"
PITCH     = "-3Hz"

CAP_SIZE     = 62                    # caption cap height
CAP_Y        = 0.585                 # caption baseline, fraction of height
WORDS_PER_LINE = 4                   # words visible at once
HILITE       = (124, 42, 232)        # pill behind the spoken word
STROKE       = 9                     # black outline thickness

FONT_CANDIDATES = [
    os.path.join(HERE, "fonts", "Poppins-ExtraBold.ttf"),   # shipped in this repo
    os.path.join(HERE, "fonts", "Anton-Regular.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",   # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", # Linux / CI runners
]

def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:    return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

# ── script parsing ────────────────────────────────────────────────────
def parse_script(path):
    """-> (title, [ {image, narration}, ... ])"""
    title, scenes, cur = "Bay Stories", [], None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            s = line.strip()
            if s.startswith("##"):
                cur = dict(image=s[2:].strip(), narration="")
                scenes.append(cur)
            elif s.startswith("#"):
                title = s[1:].strip()
            elif s:
                if cur is None:                       # narration before any ##
                    cur = dict(image="", narration="")
                    scenes.append(cur)
                cur["narration"] = (cur["narration"] + " " + s).strip()
    scenes = [s for s in scenes if s["narration"]]
    if not scenes:
        raise SystemExit("script has no narration")
    for s in scenes:
        if not s["image"]:                            # sensible default plate
            s["image"] = ("cinematic emotional film still, natural light, "
                          "shallow depth of field, 4k")
    return title, scenes

# ── image plates ──────────────────────────────────────────────────────
STYLE = ("cinematic film still, photorealistic, natural lighting, "
         "shallow depth of field, colour graded, 4k, no text, no watermark")

def fetch_plate(prompt, path, seed):
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    full = f"{prompt}, {STYLE}"
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full)}"
           f"?width={W}&height={H}&model=flux&nologo=true&seed={seed}")
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=90).read()
            if len(data) > 5000:
                open(path, "wb").write(data)
                return path
        except Exception as e:
            print(f"    retry: {e}")
    return None

def cover(img):
    """Fill the frame preserving aspect — crop the overflow, never squeeze."""
    iw, ih = img.size
    scale  = max(W / iw, H / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    return img.crop(((nw - W) // 2, (nh - H) // 2,
                     (nw - W) // 2 + W, (nh - H) // 2 + H))

# ── voice + word timings ──────────────────────────────────────────────
async def _speak(text, mp3):
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH,
                                boundary="WordBoundary")
    words, audio = [], bytearray()
    async for ch in comm.stream():
        if ch["type"] == "audio":
            audio.extend(ch["data"])
        elif ch["type"] == "WordBoundary":
            words.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
    open(mp3, "wb").write(bytes(audio))
    return words

def speak(text, mp3):
    return asyncio.run(_speak(text, mp3))

def group_words(words, narration="", per_line=WORDS_PER_LINE):
    """
    Chunk the word timeline into caption cards of a few words each, breaking
    at sentence ends so a card never straddles a full stop.

    The engine reports words with punctuation stripped, so sentence ends are
    recovered by walking the original narration in step with the word list.
    """
    breaks, cursor = set(), 0
    for i, (_, _, tok) in enumerate(words):
        j = narration.find(tok, cursor)
        if j < 0:
            continue
        cursor = j + len(tok)
        trailing = narration[cursor:cursor + 2].lstrip('"\'')
        if trailing[:1] in (".", "!", "?"):
            breaks.add(i)

    cards, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        if len(cur) >= per_line or i in breaks:
            cards.append(cur); cur = []
    if cur:
        cards.append(cur)
    return cards

# ── caption drawing ───────────────────────────────────────────────────
_font = load_font(CAP_SIZE)

def draw_caption(frame, card, t):
    """Draw one caption card, pill-highlighting the word being spoken."""
    d = ImageDraw.Draw(frame)
    words = [w[2].upper().strip() for w in card]
    # Word gap must clear the pill padding plus the outline on both sides,
    # or a neighbour's stroke eats into the highlight.
    gap   = 40

    widths = [d.textlength(w, font=_font) for w in words]
    total  = sum(widths) + gap * (len(words) - 1)

    # shrink to fit if a card runs wide
    fnt, scale = _font, 1.0
    if total > W * 0.88:
        scale = (W * 0.88) / total
        fnt   = load_font(max(28, int(CAP_SIZE * scale)))
        widths = [d.textlength(w, font=fnt) for w in words]
        gap    = int(gap * scale)
        total  = sum(widths) + gap * (len(words) - 1)

    x = (W - total) / 2
    y = H * CAP_Y

    # Pill bounds come from the actual cap-height of the drawn glyphs, not the
    # font's full ascent+descent — otherwise it hangs well below the text.
    ref_bb  = d.textbbox((0, y), "AWJ", font=fnt)
    top, bot = ref_bb[1], ref_bb[3]

    # Two passes: every pill first, then every word. Drawing pill-then-word
    # per item lets the next word's black stroke bite into the pill's edge.
    xs = []
    for wdt in widths:
        xs.append(x); x += wdt + gap

    pad_x, pad_y = int(16 * scale), int(10 * scale)
    for i, (start, dur, _) in enumerate(card):
        if start <= t < start + max(dur, 0.08):
            d.rounded_rectangle(
                [xs[i] - pad_x, top - pad_y, xs[i] + widths[i] + pad_x, bot + pad_y],
                radius=int(16 * scale), fill=(*HILITE, 255))

    for i, w in enumerate(words):
        d.text((xs[i], y), w, font=fnt, fill=(255, 255, 255, 255),
               stroke_width=max(3, int(STROKE * scale)), stroke_fill=(0, 0, 0, 255))

# ── frame composition ─────────────────────────────────────────────────
def ken_burns(plate, t, dur):
    """Slow push-in so a still plate never reads as frozen."""
    z = 1.05 + 0.06 * (t / max(dur, 1e-3))
    cw, ch = int(W / z), int(H / z)
    ox = int((W - cw) * (0.5 + 0.22 * np.sin(t * 0.13)))
    oy = int((H - ch) * (0.5 + 0.16 * np.cos(t * 0.10)))
    return plate.crop((ox, oy, ox + cw, oy + ch)).resize((W, H), Image.LANCZOS)

def compose(plate, cards, t, dur, fade):
    frame = ken_burns(plate, t, dur).convert("RGBA")

    # gentle top/bottom falloff — keeps captions legible on bright plates
    a = np.array(frame).astype(np.float32)
    ramp = np.ones(H, dtype=np.float32)
    ramp[: int(H * 0.18)] = np.linspace(0.72, 1.0, int(H * 0.18))
    ramp[-int(H * 0.28):] = np.linspace(1.0, 0.68, int(H * 0.28))
    a[:, :, :3] *= ramp[:, None, None]
    frame = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))

    for card in cards:
        if card[0][0] <= t < card[-1][0] + card[-1][1] + 0.18:
            draw_caption(frame, card, t)
            break

    out = np.array(frame.convert("RGB"))
    if fade < 1:
        out = (out.astype(np.float32) * fade).astype(np.uint8)
    return out

# ── build ─────────────────────────────────────────────────────────────
def build(script_path, out_path, work=None, keep_plates=False):
    from moviepy import VideoClip, AudioFileClip
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    title, scenes = parse_script(script_path)
    run = random.randint(1, 10**6)
    work = work or os.path.join(HERE, "out", str(run))
    os.makedirs(work, exist_ok=True)

    print(f"▶ {title}")
    print(f"  {len(scenes)} scenes · seed {run}\n")

    print("── voice ──")
    for i, sc in enumerate(scenes):
        mp3 = os.path.join(work, f"vo_{i}.mp3")
        sc["words"] = speak(sc["narration"], mp3)
        sc["cards"] = group_words(sc["words"], sc["narration"])
        sc["mp3"]   = mp3
        sc["dur"]   = AudioFileClip(mp3).duration
        print(f"   {i+1}. {sc['dur']:5.1f}s · {len(sc['words'])} words "
              f"· {len(sc['cards'])} cards")

    print("\n── plates ──")
    for i, sc in enumerate(scenes):
        p = fetch_plate(sc["image"], os.path.join(work, f"bg_{i}.jpg"),
                        random.randint(1, 10**6))
        sc["plate"] = (cover(Image.open(p).convert("RGB")) if p
                       else Image.new("RGB", (W, H), (20, 18, 16)))
        print(f"   {i+1}. {'ok' if p else 'FALLBACK'}  {sc['image'][:56]}")

    print("\n── render ──")
    FADE, parts = 0.35, []
    for i, sc in enumerate(scenes):
        dur, cards, plate = sc["dur"], sc["cards"], sc["plate"]

        def make(t, dur=dur, cards=cards, plate=plate):
            fade = min(1.0, t / FADE, (dur - t) / FADE)
            return compose(plate, cards, t, dur, fade)

        clip = VideoClip(make, duration=dur).with_audio(AudioFileClip(sc["mp3"]))
        part = os.path.join(work, f"scene_{i}.mp4")
        clip.write_videofile(part, fps=FPS, codec="libx264", audio_codec="aac",
                             bitrate="8000k", audio_bitrate="192k",
                             logger="bar", threads=4)
        clip.close()
        parts.append(part)

    print("\n── stitch ──")
    ins = [x for p in parts for x in ("-i", p)]
    n   = len(parts)
    fc  = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    r = subprocess.run([FFMPEG, "-y", *ins, "-filter_complex", fc,
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", out_path],
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-2500:]); raise SystemExit("stitch failed")

    if not keep_plates:
        for f in os.listdir(work):
            if f.startswith("scene_") or f.startswith("vo_"):
                os.remove(os.path.join(work, f))

    total = sum(s["dur"] for s in scenes)
    print(f"\n✓ {out_path}")
    print(f"  {os.path.getsize(out_path)/1e6:.1f}MB · {total:.0f}s · {W}x{H}@{FPS}")
    return out_path

def main():
    global VOICE
    ap = argparse.ArgumentParser(description="Build a narration video from a script.")
    ap.add_argument("--script", required=True, help="path to the story script")
    ap.add_argument("--out", default=os.path.expanduser("~/Desktop/bay_story.mp4"))
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--keep", action="store_true", help="keep intermediate files")
    a = ap.parse_args()
    VOICE = a.voice
    build(a.script, a.out, keep_plates=a.keep)

if __name__ == "__main__":
    main()
