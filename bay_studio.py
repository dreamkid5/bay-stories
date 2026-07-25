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
W, H, FPS = 1920, 1080, 30           # full HD — plates keep their detail
VOICE     = "en-US-ChristopherNeural"
RATE      = "-6%"
PITCH     = "-3Hz"

CAP_SIZE     = 92                    # caption cap height (scaled for 1080p)
CAP_Y        = 0.62                  # caption baseline, fraction of height
WORDS_PER_LINE = 4                   # words visible at once
HILITE       = (124, 42, 232)        # pill behind the spoken word
CHAR_BAND    = 0.30                  # width of the frame the narrator may use
CHAR_SIDE    = "right"               # which side the narrator stands on
PLATE_DARKEN = 0.94                  # how much to dim the plate (1.0 = none)

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
    """
    -> (title, narrator, [ {image, narration}, ... ])

    `#` title, `##` the scene's backdrop, `@` the narrator who appears on the
    left for the WHOLE video, everything else narration. One narrator tells the
    whole story, the way a host does — the same face throughout, not a new
    person each scene. Write `@` once to describe them; leave it out and a
    narrator is chosen to fit the story (see `describe_narrator`).
    """
    title, narrator, scenes, cur = "Bay Stories", "", [], None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            s = raw.strip()
            if s.startswith("##"):
                cur = dict(image=s[2:].strip(), narration="")
                scenes.append(cur)
            elif s.startswith("#"):
                title = s[1:].strip()
            elif s.startswith("@"):
                if not narrator:                      # first @ wins; one host
                    narrator = s[1:].strip()
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

    if not narrator:
        body = title + " " + " ".join(s["narration"] for s in scenes)
        narrator = describe_narrator(infer_gender(body))
    return title, narrator, scenes

# ── who the narrator is ───────────────────────────────────────────────
# Whole phrases, not loose words: "wife" alone tells you nothing about the
# speaker, but "my wife" means the speaker is telling it from a husband's side.
_MALE_CUES = [
    "my wife", "as a father", "i am a father", "i'm a father", "single father",
    "single dad", "as a husband", "as a man", "i am a man", "i'm a man",
    "called me dad", "called me daddy", "call me dad", "their father",
    "as their dad", "being a dad", "a father of", "widower",
]
_FEMALE_CUES = [
    "my husband", "as a mother", "i am a mother", "i'm a mother", "single mother",
    "single mom", "as a wife", "as a woman", "i am a woman", "i'm a woman",
    "called me mom", "called me mommy", "call me mom", "their mother",
    "as their mom", "being a mom", "a mother of", "widow", "pregnant",
    "gave birth", "my pregnancy",
]

def infer_gender(text):
    """
    Guess the narrator's gender from how they refer to themselves.

    Leans on first-person relationship phrases ("my wife", "as a mother"),
    which pin the speaker's side of a relationship. When a story gives no such
    cue it returns "male" as a neutral default — override it with an explicit
    `@` line, which always wins because it never reaches this function.
    """
    t = " " + text.lower() + " "
    male   = sum(t.count(c) for c in _MALE_CUES)
    female = sum(t.count(c) for c in _FEMALE_CUES)
    if female > male:
        return "female"
    return "male"

def describe_narrator(gender):
    """A young, believable host for the given gender (roughly 16–22)."""
    if gender == "female":
        return ("a young woman around twenty years old, youthful face, simple "
                "elegant blouse, warm composed expression, natural makeup")
    return ("a young man around twenty years old, youthful face, simple "
            "collared shirt, calm thoughtful expression")

# ── image plates ──────────────────────────────────────────────────────
# People in scenes read as young adults (roughly 16–22) unless the scene text
# itself says otherwise — an explicit "elderly" in a prompt still wins.
STYLE = ("everyone in frame is a young adult aged sixteen to twenty two, "
         "youthful faces, cinematic film still, photorealistic, natural "
         "lighting, shallow depth of field, colour graded, high resolution, "
         "sharp detail, 4k, no text, no watermark")

def fetch_plate(prompt, path, seed):
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    full = f"{prompt}, {STYLE}"
    # Fetch at full output resolution so the plate keeps its detail; a smaller
    # image upscaled into the frame is exactly the soft, compressed look to avoid.
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full)}"
           f"?width={W}&height={H}&model=flux&nologo=true&enhance=true&seed={seed}")
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

def fit(img, max_w, max_h):
    """
    Scale to fit inside a box. One scale factor for both axes — the whole
    point is that a portrait can never come out stretched or squashed.
    """
    scale = min(max_w / img.width, max_h / img.height)
    return img.resize((max(1, round(img.width * scale)),
                       max(1, round(img.height * scale))), Image.LANCZOS)

# The character is generated against a green screen and keyed out. A white
# studio backdrop seems tidier but cannot be separated reliably: a white shirt
# on a white wall is genuinely ambiguous to any colour-matching cutout, and the
# clothes get eaten along with the background. Nothing on a person is green.
CHAR_STYLE = ("medium shot, waist up portrait, head and torso filling the "
              "frame, facing camera, solid chroma key green screen background, "
              "bright even studio lighting, photorealistic, sharp focus, "
              "high resolution, 8k, no text, no watermark")

def fetch_character(prompt, path, seed):
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    full = f"{prompt}, {CHAR_STYLE}"
    # Generated large so the keyed cutout stays crisp when placed in the frame.
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full)}"
           f"?width=896&height=1344&model=flux&nologo=true&enhance=true&seed={seed}")
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

def chroma_key(img, soft=0.10, hard=0.24):
    """
    Key a green screen out of a portrait.

    Greenness is measured as a FRACTION of the pixel's own brightness, not as
    an absolute channel gap. That matters because the backdrop is not evenly
    lit: a shadowed corner of the screen has a small absolute green lead but is
    still obviously green, and an absolute threshold leaves it behind as a
    murky ghost. A ratio keys the bright screen and its shadows alike, while
    skin, hair and white clothing — where green sits level with red and blue —
    stay put.
    """
    img = img.convert("RGB")
    a   = np.array(img).astype(np.int16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]

    bright   = np.maximum(a.max(axis=2), 1)
    greenness = (g - np.maximum(r, b)) / bright          # 0..1, shadow-proof
    alpha = 1.0 - np.clip((greenness - soft) / (hard - soft), 0, 1)

    # Despill: green light bounces onto hair and shoulders, leaving a green
    # rim that betrays the cut. On every kept pixel, hold green no higher than
    # its neighbours — a real subject has no pixel where green runs ahead.
    keep    = alpha > 0
    ceiling = np.maximum(r, b)
    a[:, :, 1] = np.where(keep & (g > ceiling), ceiling, g)

    am = np.array(Image.fromarray((alpha * 255).astype(np.uint8))
                  .filter(ImageFilter.MinFilter(3))       # bite off the fringe
                  .filter(ImageFilter.GaussianBlur(1.2))) # soften the edge
    # Anything left faint is green haze, not subject — cut it rather than let
    # it composite as a translucent ghost.
    am[am < 40] = 0

    out = Image.fromarray(a.astype(np.uint8)).convert("RGBA")
    out.putalpha(Image.fromarray(am))
    return out

def largest_blob(img):
    """
    Keep only the biggest connected piece of the cutout.

    Keying leaves specks where the backdrop was mottled. The subject is always
    the largest connected region, so everything else is noise.
    """
    from collections import deque
    alpha = np.array(img.getchannel("A"))
    solid = alpha > 40
    h, w = solid.shape
    if not solid.any():
        return img

    seen = np.zeros_like(solid)
    best, best_size = None, 0
    for sy in range(0, h, 4):                     # coarse seeding is plenty
        for sx in range(0, w, 4):
            if not solid[sy, sx] or seen[sy, sx]:
                continue
            q = deque([(sy, sx)]); seen[sy, sx] = True
            blob, size = [], 0
            while q:
                y, x = q.popleft()
                blob.append((y, x)); size += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and solid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if size > best_size:
                best, best_size = blob, size

    if best is None:
        return img
    keep = np.zeros_like(solid)
    for y, x in best:
        keep[y, x] = True
    out = np.array(img)
    out[:, :, 3] = np.where(keep, out[:, :, 3], 0)
    return Image.fromarray(out)

def prepare_character(path):
    """Key the figure out and size it as a medium shot for one side of the frame."""
    if not path or not os.path.exists(path):
        return None
    cut = largest_blob(chroma_key(Image.open(path)))

    # Trim to what actually survived, so sizing is driven by the person and
    # not by empty margin that used to be backdrop.
    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)

    # Size on height so a waist-up medium shot stands nearly full-frame, the
    # way the narrator sits large in the reference. Uniform scale — never
    # stretched or squeezed.
    target_h = int(H * 0.97)
    scale    = target_h / cut.height
    cut = cut.resize((max(1, round(cut.width * scale)), target_h), Image.LANCZOS)

    # A medium shot is roughly torso-wide and would spill past its band. Crop
    # the sides to the band, keeping the subject's centre of mass framed — the
    # same call a camera operator makes framing a guest.
    band = int(W * CHAR_BAND)
    if cut.width > band:
        alpha = np.array(cut.getchannel("A")) > 40
        cols  = np.where(alpha.any(axis=0))[0]
        centre = int(cols.mean()) if len(cols) else cut.width // 2
        left   = int(np.clip(centre - band // 2, 0, cut.width - band))
        cut = cut.crop((left, 0, left + band, cut.height))

        # A straight crop leaves a hard vertical edge that reads as a torn
        # photo. Feather the INNER edge — the one facing the captions — so the
        # figure melts into the scene. Which edge that is depends on the side.
        arr  = np.array(cut).astype(np.float32)
        ramp = int(band * 0.12)
        if CHAR_SIDE == "right":
            arr[:, :ramp, 3] *= np.linspace(0, 1, ramp)[None, :]
        else:
            arr[:, -ramp:, 3] *= np.linspace(1, 0, ramp)[None, :]
        cut  = Image.fromarray(arr.astype(np.uint8))
    return cut

# ── cutting the character out of its backdrop ─────────────────────────
def remove_backdrop(img, tol=30, feather=2):
    """
    Drop the studio backdrop behind a portrait, keeping the person.

    Background is what is BOTH close to the border colour and reachable from
    the border. Two conditions, because either alone fails: matching colour
    only would punch holes through anything in the subject that happens to be
    pale, while a plain flood fill walks the backdrop's own soft gradient
    straight into the person's face.
    """
    from collections import deque
    img = img.convert("RGB")
    a   = np.array(img).astype(np.int16)
    h, w = a.shape[:2]

    ring = np.concatenate([
        a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3),
        a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3),
    ])
    ref       = np.median(ring, axis=0)
    in_band   = np.abs(a - ref).max(axis=2) <= tol * 3   # generous global band
    local_tol = max(6, tol // 3)                          # tight local step

    seen = np.zeros((h, w), dtype=bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if in_band[y, x] and not seen[y, x]:
                seen[y, x] = True; q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if in_band[y, x] and not seen[y, x]:
                seen[y, x] = True; q.append((y, x))

    while q:
        y, x = q.popleft()
        c = a[y, x]
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if (0 <= ny < h and 0 <= nx < w and not seen[ny, nx]
                    and in_band[ny, nx]
                    and np.abs(a[ny, nx] - c).max() <= local_tol):
                seen[ny, nx] = True
                q.append((ny, nx))

    alpha = np.where(seen, 0, 255).astype(np.uint8)
    am = Image.fromarray(alpha).filter(ImageFilter.MinFilter(3))  # eat the fringe
    am = am.filter(ImageFilter.GaussianBlur(feather))             # soften the edge

    out = img.convert("RGBA")
    out.putalpha(am)
    return out

# ── output integrity ──────────────────────────────────────────────────
def verify_video(path, expect_audio=True, min_seconds=0.5):
    """
    Is this a complete, playable MP4? -> (ok, reason)

    An MP4's index (the `moov` atom) is written last, so a file truncated by a
    crash, a full disk, or a reader arriving mid-write still looks like a video
    by name and size. Probing is the only way to tell the difference.
    """
    if not os.path.exists(path):
        return False, "file does not exist"
    if os.path.getsize(path) < 1024:
        return False, f"file is only {os.path.getsize(path)} bytes"

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        return False, f"ffmpeg unavailable: {e}"

    probe = subprocess.run([ffmpeg, "-v", "error", "-i", path,
                            "-f", "null", "-"],
                           capture_output=True, text=True)
    err = (probe.stderr or "").strip()
    if "moov atom not found" in err:
        return False, "truncated (no moov atom) — the encode never finished"
    if probe.returncode != 0:
        return False, f"unplayable: {err.splitlines()[0] if err else 'unknown'}"

    meta = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True).stderr
    if "Video:" not in meta:
        return False, "no video stream"
    if expect_audio and "Audio:" not in meta:
        return False, "no audio stream"

    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", meta)
    if not m:
        return False, "no duration reported"
    hrs, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
    total = hrs * 3600 + mins * 60 + secs
    if total < min_seconds:
        return False, f"only {total:.2f}s long"

    return True, f"{total:.1f}s"

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

def card_windows(cards, dur):
    """
    How long each card stays on screen -> [(show_from, show_until, card)].

    A card holds until the next one takes over, rather than vanishing when its
    last word finishes. Speakers pause between sentences, and timing cards to
    speech alone leaves the frame bare for a quarter of the run.
    """
    out = []
    for i, card in enumerate(cards):
        start = 0.0 if i == 0 else card[0][0]
        end   = cards[i + 1][0][0] if i + 1 < len(cards) else dur
        out.append((start, end, card))
    return out

# ── caption drawing ───────────────────────────────────────────────────
_font = load_font(CAP_SIZE)

def draw_caption(frame, card, t, band_left=0, band_right=None):
    """
    Draw one caption card, pill-highlighting the word being spoken.

    `band_left`/`band_right` bound the usable width. When the narrator stands
    on one side, captions centre in the clear space beside them instead of the
    whole frame, so text never lands on the figure. Every measurement derives
    from CAP_SIZE, so the whole caption scales with one number.
    """
    if band_right is None:
        band_right = W
    d = ImageDraw.Draw(frame)
    words = [w[2].upper().strip() for w in card]

    # Everything sized off the cap height, so 720p and 1080p look identical.
    gap    = int(CAP_SIZE * 0.62)      # clears the pill padding + outline
    stroke = max(3, int(CAP_SIZE * 0.14))

    band_w = band_right - band_left
    max_w  = band_w * 0.92

    widths = [d.textlength(w, font=_font) for w in words]
    total  = sum(widths) + gap * (len(words) - 1)

    # shrink to fit if a card runs wide
    fnt, scale = _font, 1.0
    if total > max_w:
        scale  = max_w / total
        fnt    = load_font(max(24, int(CAP_SIZE * scale)))
        widths = [d.textlength(w, font=fnt) for w in words]
        gap    = max(10, int(gap * scale))
        stroke = max(3, int(stroke * scale))
        total  = sum(widths) + gap * (len(words) - 1)

    x = band_left + (band_w - total) / 2
    y = H * CAP_Y

    # Pill bounds come from the actual cap-height of the drawn glyphs, not the
    # font's full ascent+descent — otherwise it hangs well below the text.
    ref_bb   = d.textbbox((0, y), "AWJ", font=fnt)
    top, bot = ref_bb[1], ref_bb[3]

    # Two passes: every pill first, then every word. Drawing pill-then-word
    # per item lets the next word's black stroke bite into the pill's edge.
    xs = []
    for wdt in widths:
        xs.append(x); x += wdt + gap

    pad_x  = int(CAP_SIZE * 0.26 * scale)
    pad_y  = int(CAP_SIZE * 0.16 * scale)
    radius = int(CAP_SIZE * 0.26 * scale)
    for i, (start, dur, _) in enumerate(card):
        if start <= t < start + max(dur, 0.08):
            d.rounded_rectangle(
                [xs[i] - pad_x, top - pad_y, xs[i] + widths[i] + pad_x, bot + pad_y],
                radius=radius, fill=(*HILITE, 255))

    for i, w in enumerate(words):
        d.text((xs[i], y), w, font=fnt, fill=(255, 255, 255, 255),
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))

# ── frame composition ─────────────────────────────────────────────────
def ken_burns(plate, t, dur):
    """
    Slow push-in so a still plate never reads as frozen. The zoom is kept
    shallow because every crop is upscaled back to the frame, and a big zoom
    means a big upscale — which is the soft, compressed look we are avoiding.
    """
    z = 1.02 + 0.05 * (t / max(dur, 1e-3))
    cw, ch = int(W / z), int(H / z)
    ox = int((W - cw) * (0.5 + 0.18 * np.sin(t * 0.13)))
    oy = int((H - ch) * (0.5 + 0.14 * np.cos(t * 0.10)))
    return plate.crop((ox, oy, ox + cw, oy + ch)).resize((W, H), Image.LANCZOS)

# A few dozen soft out-of-focus motes that drift slowly, matching the gentle
# bokeh in the reference. Seeded once so the pattern is stable within a video.
_prng = np.random.default_rng(7)
_MOTES = [dict(x=_prng.random(), y=_prng.random(),
               r=_prng.uniform(2, 7) * (H / 720),
               a=_prng.uniform(0.05, 0.18),
               sx=_prng.uniform(-0.006, 0.006),
               sy=_prng.uniform(-0.010, -0.003),
               ph=_prng.uniform(0, 6.28)) for _ in range(26)]

def _draw_motes(frame, t):
    # Drawn on their own layer and blurred so they read as soft out-of-focus
    # bokeh rather than hard white dots.
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for m in _MOTES:
        x = ((m["x"] + m["sx"] * t) % 1.0) * W
        y = ((m["y"] + m["sy"] * t) % 1.0) * H
        a = int(255 * m["a"] * (0.6 + 0.4 * np.sin(t * 0.8 + m["ph"])))
        r = m["r"]
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, max(0, a)))
    frame.alpha_composite(layer.filter(ImageFilter.GaussianBlur(3)))

def compose(plate, windows, t, dur, fade, character=None):
    frame = ken_burns(plate, t, dur).convert("RGBA")

    # A light, even dim rather than the heavy darkening that used to flatten
    # detail; plus a soft edge vignette so the frame has depth without murk.
    a = np.array(frame).astype(np.float32)
    a[:, :, :3] *= PLATE_DARKEN
    yy, xx = np.meshgrid(np.linspace(-1, 1, H), np.linspace(-1, 1, W), indexing="ij")
    vig = np.clip(1 - (yy ** 2 * 0.20 + xx ** 2 * 0.14), 0.70, 1.0)
    a[:, :, :3] *= vig[:, :, None]
    frame = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))

    _draw_motes(frame, t)

    band_left, band_right = 0, W
    if character is not None:
        on_right = (CHAR_SIDE == "right")
        cx = W - character.width if on_right else 0

        # Soften the plate behind the figure so a cut edge doesn't read as a
        # sticker pasted onto a busy photograph — gradient fading in from the
        # side the narrator stands on.
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scrim)
        reach = int(character.width * 1.15)
        for i in range(reach):
            col = (8, 9, 14, int(130 * (1 - i / reach) ** 1.5))
            xi = W - 1 - i if on_right else i
            sd.line([(xi, 0), (xi, H)], fill=col)
        frame.alpha_composite(scrim)

        # Bottom-anchored: the frame edge crops the figure, the way a real
        # lower-third does. No fade — that reads as a dissolve, not a cutout.
        frame.alpha_composite(character, (cx, H - character.height))

        # Captions keep to the clear side.
        if on_right:
            band_right = int(W - character.width * 0.92)
        else:
            band_left = int(character.width * 0.92)

    for start, end, card in windows:
        if start <= t < end:
            draw_caption(frame, card, t, band_left, band_right)
            break

    out = np.array(frame.convert("RGB"))
    if fade < 1:
        out = (out.astype(np.float32) * fade).astype(np.uint8)
    return out

# ── thumbnail ─────────────────────────────────────────────────────────
THUMB_W, THUMB_H = 1280, 720
THUMB_FONT = os.path.join(HERE, "fonts", "Baloo2-ExtraBold.ttf")

# The reference palette: white body copy with bright accents on the beats.
T_WHITE  = (255, 255, 255)
T_GREEN  = (128, 246, 120)
T_YELLOW = (255, 224, 64)
T_RED    = (255, 72, 66)
T_PINK   = (255, 95, 205)

def _thumb_font(size):
    try:
        f = ImageFont.truetype(THUMB_FONT, size)
        try:    f.set_variation_by_name("ExtraBold")
        except Exception: pass
        return f
    except Exception:
        return load_font(size)

def hook_from_scenes(scenes, max_words=42):
    """The opening of the story, trimmed to a punchy thumbnail hook."""
    words = []
    for sc in scenes:
        words += sc["narration"].split()
        if len(words) >= max_words:
            break
    clipped = len(words) > max_words
    text = " ".join(words[:max_words]).rstrip(",;:-—– ")
    return text + ("…" if clipped else "")

def colorize_hook(hook):
    """
    -> [(word, colour)]. Quoted speech rotates through the bright accents,
    anything with money or a number turns red, and every so often a clause
    goes green — the lively, uneven emphasis the reference thumbnail uses.
    """
    accents = [T_PINK, T_YELLOW, T_GREEN]
    qi = ci = 0
    out = []
    for part in re.split(r'("[^"]*"|“[^”]*”|\'[^\']*\')', hook):
        if not part:
            continue
        if part[0] in "\"'“":                       # a quoted span
            col = accents[qi % len(accents)]; qi += 1
            for w in part.split():
                out.append((w, col))
        else:
            for clause in re.split(r'(?<=[,.;:—–])\s+', part):
                clause = clause.strip()
                if not clause:
                    continue
                if re.search(r"[\$]|\d", clause):        # money or a number
                    col = T_RED
                elif ci % 3 == 1:
                    col = T_GREEN
                else:
                    col = T_WHITE
                ci += 1
                for w in clause.split():
                    out.append((w, col))
    return out

def _wrap_colored(tokens, font, max_w):
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    space = d.textlength(" ", font=font)
    lines, cur, curw = [], [], 0.0
    for word, col in tokens:
        ww = d.textlength(word, font=font)
        if cur and curw + space + ww > max_w:
            lines.append(cur); cur, curw = [], 0.0
        if cur:
            curw += space
        cur.append((word, col, ww)); curw += ww
    if cur:
        lines.append(cur)
    return lines, space

def make_thumbnail(hook, narrator_cut, out_path):
    """A YouTube-style thumbnail: coloured hook on the left, narrator on the right."""
    W2, H2 = THUMB_W, THUMB_H
    canvas = Image.new("RGB", (W2, H2), (20, 21, 26))

    # subtle vignette so the flat dark ground has a little depth
    a = np.array(canvas).astype(np.float32)
    yy, xx = np.meshgrid(np.linspace(-1, 1, H2), np.linspace(-1, 1, W2), indexing="ij")
    a *= np.clip(1 - (yy ** 2 * 0.25 + xx ** 2 * 0.18), 0.6, 1.0)[:, :, None]
    canvas = Image.fromarray(a.astype(np.uint8)).convert("RGBA")

    # narrator on the right, full height, feathered inner edge already present
    person_w = 0
    if narrator_cut is not None:
        scale = H2 / narrator_cut.height
        person = narrator_cut.resize(
            (max(1, round(narrator_cut.width * scale)), H2), Image.LANCZOS)
        person_w = person.width
        canvas.alpha_composite(person, (W2 - person_w, 0))

    # text area: everything left of the narrator, with margins
    margin = 46
    area_x0, area_y0 = margin, margin
    area_x1 = W2 - person_w + int(person_w * 0.18)   # may tuck under the feather
    area_x1 = min(area_x1, W2 - margin)
    area_w  = area_x1 - area_x0
    area_h  = H2 - margin * 2

    tokens = colorize_hook(hook)

    # largest font whose wrapped height fits the area
    font = _thumb_font(64)
    lines, space = _wrap_colored(tokens, font, area_w)
    for size in range(78, 30, -3):
        font = _thumb_font(size)
        lines, space = _wrap_colored(tokens, font, area_w)
        lh = size * 1.22
        if len(lines) * lh <= area_h:
            break
    lh = font.size * 1.22

    d = ImageDraw.Draw(canvas)
    stroke = max(3, int(font.size * 0.09))
    y = area_y0 + (area_h - len(lines) * lh) / 2         # vertically centred
    for line in lines:
        x = area_x0
        for word, col, ww in line:
            d.text((x, y), word, font=font, fill=(*col, 255),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += ww + space
        y += lh

    canvas.convert("RGB").save(out_path, quality=92)
    return out_path

# ── build ─────────────────────────────────────────────────────────────
def build(script_path, out_path, work=None, keep_plates=False):
    from moviepy import VideoClip, AudioFileClip
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    title, narrator, scenes = parse_script(script_path)
    run = random.randint(1, 10**6)
    work = work or os.path.join(HERE, "out", str(run))
    os.makedirs(work, exist_ok=True)

    print(f"▶ {title}")
    print(f"  {len(scenes)} scenes · seed {run}")
    print(f"  narrator: {narrator}\n")

    print("── voice ──")
    for i, sc in enumerate(scenes):
        mp3 = os.path.join(work, f"vo_{i}.mp3")
        sc["words"] = speak(sc["narration"], mp3)
        sc["cards"] = group_words(sc["words"], sc["narration"])
        sc["mp3"]   = mp3
        sc["dur"]   = AudioFileClip(mp3).duration
        sc["windows"] = card_windows(sc["cards"], sc["dur"])
        print(f"   {i+1}. {sc['dur']:5.1f}s · {len(sc['words'])} words "
              f"· {len(sc['cards'])} cards")

    print("\n── plates ──")
    for i, sc in enumerate(scenes):
        p = fetch_plate(sc["image"], os.path.join(work, f"bg_{i}.jpg"),
                        random.randint(1, 10**6))
        sc["plate"] = (cover(Image.open(p).convert("RGB")) if p
                       else Image.new("RGB", (W, H), (20, 18, 16)))
        print(f"   {i+1}. {'ok' if p else 'FALLBACK'}  {sc['image'][:56]}")

    print("\n── narrator ──")
    # One narrator for the whole video: generate and cut them once, then reuse
    # the exact same cutout on every scene so the face never changes.
    char_path = fetch_character(narrator, os.path.join(work, "narrator.jpg"),
                                random.randint(1, 10**6))
    narrator_cut = prepare_character(char_path) if char_path else None
    print(f"   {'cut ' + str(narrator_cut.size) if narrator_cut else 'FAILED — playing full-bleed'}")

    # A thumbnail written next to the video, sharing its name with a .jpg suffix.
    thumb_path = os.path.splitext(out_path)[0] + ".jpg"
    os.makedirs(os.path.dirname(os.path.abspath(thumb_path)), exist_ok=True)
    make_thumbnail(hook_from_scenes(scenes), narrator_cut, thumb_path)
    print(f"── thumbnail ──\n   {thumb_path}")

    print("\n── render ──")
    FADE, parts = 0.35, []
    for i, sc in enumerate(scenes):
        dur, windows, plate = sc["dur"], sc["windows"], sc["plate"]

        def make(t, dur=dur, windows=windows, plate=plate):
            fade = min(1.0, t / FADE, (dur - t) / FADE)
            return compose(plate, windows, t, dur, fade, narrator_cut)

        clip = VideoClip(make, duration=dur).with_audio(AudioFileClip(sc["mp3"]))
        part = os.path.join(work, f"scene_{i}.mp4")
        # High per-scene bitrate so no detail is lost before the final encode —
        # at 1080p a low bitrate is itself a source of the compressed look.
        clip.write_videofile(part, fps=FPS, codec="libx264", audio_codec="aac",
                             bitrate="14000k", audio_bitrate="192k",
                             logger="bar", threads=4)
        clip.close()
        # A scene that failed to finalise would poison the stitch with a
        # confusing error much later, so catch it here where the cause is plain.
        ok, why = verify_video(part, expect_audio=True)
        if not ok:
            raise SystemExit(f"scene {i + 1} did not encode cleanly: {why}")
        parts.append(part)

    print("\n── stitch ──")
    ins = [x for p in parts for x in ("-i", p)]
    n   = len(parts)
    fc  = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Write to a sibling temp file, then rename into place. A reader at
    # out_path therefore sees either nothing or a complete, verified video —
    # never the half-written file that an interrupted encode leaves behind.
    # The .mp4 suffix stays on the end: ffmpeg picks its muxer from the
    # extension, and a bare ".part" leaves it unable to choose one.
    tmp = f"{out_path}.part.mp4"
    try:
        r = subprocess.run([FFMPEG, "-y", *ins, "-filter_complex", fc,
                            "-map", "[v]", "-map", "[a]",
                            "-c:v", "libx264", "-preset", "medium", "-crf", "17",
                            "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", tmp],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-2500:])
            raise SystemExit("stitch failed")

        ok, why = verify_video(tmp, expect_audio=True)
        if not ok:
            raise SystemExit(f"stitched file failed verification: {why}")

        os.replace(tmp, out_path)          # atomic within the same filesystem
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

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
