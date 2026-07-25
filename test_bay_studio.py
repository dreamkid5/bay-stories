#!/usr/bin/env python3
"""
Test suite for bay_studio.

    python3 test_bay_studio.py           # fast checks only (no network)
    python3 test_bay_studio.py --full    # adds a real end-to-end render

The fast pass is pure logic and pixels — no network, no TTS, runs in seconds.
The full pass renders an actual short video, so it needs network access for
the voice engine and the image API.
"""
import os, subprocess, sys, tempfile, traceback
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bay_studio as B

PASS = FAIL = 0
FAILURES = []

def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}")
    except Exception as e:
        FAIL += 1
        FAILURES.append((name, traceback.format_exc()))
        print(f"  \033[31mFAIL\033[0m  {name}  →  {e}")

def eq(got, want, what=""):
    assert got == want, f"{what}expected {want!r}, got {got!r}"

def true(cond, msg):
    assert cond, msg

# ── script parsing ────────────────────────────────────────────────────
def t_parse_basic():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# My Title\n## a quiet room\nHello world. Second line.\n"
                "## a busy street\nMore narration here.\n")
        p = f.name
    try:
        title, narrator, scenes = B.parse_script(p)
        eq(title, "My Title", "title: ")
        eq(len(scenes), 2, "scene count: ")
        eq(scenes[0]["image"], "a quiet room")
        eq(scenes[0]["narration"], "Hello world. Second line.")
        eq(scenes[1]["narration"], "More narration here.")
        true(narrator, "a narrator should always be chosen")
    finally:
        os.unlink(p)

def t_parse_multiline_narration():
    """Consecutive narration lines join into one block."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n## img\nLine one.\nLine two.\nLine three.\n")
        p = f.name
    try:
        _, _, scenes = B.parse_script(p)
        eq(len(scenes), 1)
        eq(scenes[0]["narration"], "Line one. Line two. Line three.")
    finally:
        os.unlink(p)

def t_parse_narration_before_scene():
    """Narration with no ## above it still yields a scene, with a default plate."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\nOrphan narration.\n")
        p = f.name
    try:
        _, _, scenes = B.parse_script(p)
        eq(len(scenes), 1)
        true(scenes[0]["image"], "expected a default image prompt")
    finally:
        os.unlink(p)

def t_parse_blank_lines_ignored():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n\n\n## img\n\nNarration.\n\n\n")
        p = f.name
    try:
        _, _, scenes = B.parse_script(p)
        eq(len(scenes), 1)
        eq(scenes[0]["narration"], "Narration.")
    finally:
        os.unlink(p)

def t_parse_empty_script_rejected():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# Title only\n")
        p = f.name
    try:
        try:
            B.parse_script(p)
            raise AssertionError("empty script should have been rejected")
        except SystemExit:
            pass
    finally:
        os.unlink(p)

def t_parse_real_scripts():
    """Every script shipped in the repo must parse."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    files = [f for f in os.listdir(d) if f.endswith(".txt")] if os.path.isdir(d) else []
    true(files, "no scripts found to check")
    for f in files:
        title, _, scenes = B.parse_script(os.path.join(d, f))
        true(title, f"{f}: no title")
        true(scenes, f"{f}: no scenes")
        for i, s in enumerate(scenes):
            true(s["narration"].strip(), f"{f}: scene {i+1} has no narration")
            true(s["image"].strip(), f"{f}: scene {i+1} has no image prompt")

# ── auto scene segmentation ───────────────────────────────────────────
def t_long_narration_is_split():
    """A raw story with no ## breaks must not become one giant scene."""
    body = " ".join(f"Sentence number {i} tells a small part of the story."
                    for i in range(60))          # ~540 words
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n" + body + "\n")
        p = f.name
    try:
        _, _, scenes = B.parse_script(p)
        true(len(scenes) > 5, f"long story split into only {len(scenes)} scene(s)")
        for s in scenes:
            true(len(s["narration"].split()) <= B.SCENE_MAX_WORDS + 30,
                 "a segmented scene is still too long")
            true(s["image"].strip(), "a segmented scene has no image prompt")
    finally:
        os.unlink(p)

def t_short_scenes_untouched():
    """A tidy hand-written script keeps exactly the scenes it declares."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n## a room\nShort one.\n## a street\nShort two.\n")
        p = f.name
    try:
        _, _, scenes = B.parse_script(p)
        eq(len(scenes), 2, "short scenes should pass through untouched: ")
        eq(scenes[0]["image"], "a room")
    finally:
        os.unlink(p)

def t_segment_keeps_all_narration():
    text = " ".join(f"Word{i} here is part of it." for i in range(80))
    segs = B.auto_segment(text)
    joined = " ".join(s["narration"] for s in segs)
    eq(len(joined.split()), len(text.split()), "segmentation lost or added words: ")

def t_visual_prompt_prefers_concrete():
    sents = ["I want you to understand something deep.",
             "She sat alone at the kitchen table by the window."]
    p = B.auto_segment(" ".join(sents))
    # the concrete sentence should drive the image, not the meta one
    img = p[0]["image"].lower()
    true("kitchen" in img or "window" in img or "table" in img,
         f"visual prompt ignored the concrete sentence: {img!r}")

# ── word grouping ─────────────────────────────────────────────────────
def fake_words(spec):
    """[(text, start, dur)] -> the engine's (start, dur, text) tuples."""
    return [(s, d, txt) for txt, s, d in spec]

def t_group_respects_word_limit():
    words = fake_words([(f"w{i}", i * 0.5, 0.4) for i in range(12)])
    cards = B.group_words(words, " ".join(f"w{i}" for i in range(12)))
    for c in cards:
        true(len(c) <= B.WORDS_PER_LINE,
             f"card has {len(c)} words, limit is {B.WORDS_PER_LINE}")

def t_group_breaks_on_sentence_end():
    narration = "Fifteen years. That is how long."
    words = fake_words([("Fifteen", 0.0, .4), ("years", .4, .4),
                        ("That", .9, .3), ("is", 1.2, .2),
                        ("how", 1.4, .2), ("long", 1.6, .4)])
    cards = B.group_words(words, narration)
    eq([w[2] for w in cards[0]], ["Fifteen", "years"],
       "first card should stop at the full stop: ")

def t_group_keeps_every_word():
    narration = "One two three four five six seven eight nine."
    words = fake_words([(w, i * .3, .25)
                        for i, w in enumerate(narration.rstrip(".").split())])
    cards = B.group_words(words, narration)
    flat = [w[2] for c in cards for w in c]
    eq(flat, [w[2] for w in words], "grouping must not drop or reorder words: ")

def t_group_handles_empty():
    eq(B.group_words([], ""), [])

def t_group_no_punctuation():
    """A narration with no full stops still chunks by word count."""
    narration = "one two three four five six"
    words = fake_words([(w, i * .3, .25) for i, w in enumerate(narration.split())])
    cards = B.group_words(words, narration)
    true(len(cards) >= 2, "expected chunking by word limit")

# ── caption timing ────────────────────────────────────────────────────
def t_windows_cover_everything():
    """The regression this suite exists for: no bare frames, ever."""
    words = fake_words([("a", 0.5, .3), ("b", 1.0, .3),
                        ("c", 4.0, .3), ("d", 4.5, .3)])   # 2.5s of silence
    cards = B.group_words(words, "a b. c d.")
    dur = 8.0
    win = B.card_windows(cards, dur)

    cursor = 0.0
    for start, end, _ in win:
        true(start <= cursor + 1e-9,
             f"gap in caption coverage at {cursor:.2f}s (next card at {start:.2f}s)")
        cursor = max(cursor, end)
    eq(round(cursor, 6), dur, "captions must run to the end of the scene: ")

def t_windows_start_at_zero():
    words = fake_words([("late", 2.0, .4)])
    win = B.card_windows(B.group_words(words, "late."), 5.0)
    eq(win[0][0], 0.0, "first card must be up from the first frame: ")

def t_windows_are_ordered():
    words = fake_words([(f"w{i}", i * 1.0, .5) for i in range(9)])
    win = B.card_windows(B.group_words(words, " ".join(f"w{i}" for i in range(9))), 12.0)
    for i in range(len(win) - 1):
        true(win[i][1] <= win[i + 1][0] + 1e-9, "windows overlap")
        true(win[i][0] < win[i][1], "window ends before it starts")

def t_windows_single_card():
    words = fake_words([("only", 0.2, .5)])
    win = B.card_windows(B.group_words(words, "only."), 3.0)
    eq(len(win), 1)
    eq(win[0][0], 0.0)
    eq(win[0][1], 3.0)

# ── image handling ────────────────────────────────────────────────────
def t_cover_exact_size():
    for size in [(1920, 1080), (800, 600), (500, 2000), (3000, 400), (1280, 720)]:
        out = B.cover(Image.new("RGB", size, (120, 90, 60)))
        eq(out.size, (B.W, B.H), f"cover{size}: ")

def t_cover_does_not_squeeze():
    """
    A circle must stay a circle. Squeezing is the defect this guards: a
    non-uniform scale would turn it into an ellipse.
    """
    src = Image.new("RGB", (600, 600), (0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(src).ellipse([100, 100, 500, 500], fill=(255, 255, 255))

    a = np.array(B.cover(src).convert("L")) > 128
    rows = np.where(a.any(axis=1))[0]
    cols = np.where(a.any(axis=0))[0]
    # The square source is cropped to 16:9, so the circle is clipped top and
    # bottom; its full width must survive and match the uncropped diameter.
    height = rows[-1] - rows[0] + 1
    width  = cols[-1] - cols[0] + 1
    true(width >= height,
         f"aspect distorted: circle became {width}x{height}")
    # cover() scales by max(W/w, H/h) — the same factor on both axes
    scale = max(B.W / 600, B.H / 600)
    expected_w = 400 * scale
    true(abs(width - expected_w) <= 5,
         f"width {width}px, expected about {expected_w:.0f}px — image was scaled non-uniformly")

def t_fit_preserves_aspect():
    """The guard against a squeezed character: one scale factor, both axes."""
    for size in [(400, 900), (900, 400), (512, 768), (1000, 1000), (137, 641)]:
        out = B.fit(Image.new("RGBA", size), 435, 700)
        before = size[0] / size[1]
        after  = out.width / out.height
        true(abs(before - after) / before < 0.02,
             f"fit{size} -> {out.size}: aspect changed {before:.3f} to {after:.3f}")

def t_fit_stays_inside_box():
    for size in [(400, 900), (900, 400), (2000, 30)]:
        out = B.fit(Image.new("RGBA", size), 435, 700)
        true(out.width <= 435 and out.height <= 700,
             f"fit{size} -> {out.size} escaped the box")

def t_chroma_key_removes_green_keeps_subject():
    img = Image.new("RGB", (200, 200), (28, 190, 42))          # green screen
    from PIL import ImageDraw
    ImageDraw.Draw(img).rectangle([60, 60, 140, 140], fill=(232, 200, 178))  # skin
    a = np.array(B.chroma_key(img).getchannel("A"))
    true(a[10, 10] < 30, "green backdrop was not keyed out")
    true(a[100, 100] > 200, "the subject was keyed out along with the backdrop")

def t_chroma_key_keeps_white_clothing():
    """
    White on white is what broke the earlier cutout; on green it must survive.
    """
    img = Image.new("RGB", (200, 200), (30, 185, 45))
    from PIL import ImageDraw
    ImageDraw.Draw(img).rectangle([70, 70, 130, 130], fill=(248, 248, 246))
    a = np.array(B.chroma_key(img).getchannel("A"))
    true(a[100, 100] > 200, "white clothing was keyed out")

def t_chroma_key_despills():
    """No kept pixel may still have green running ahead of red and blue."""
    img = Image.new("RGB", (120, 120), (30, 185, 45))
    from PIL import ImageDraw
    ImageDraw.Draw(img).rectangle([40, 40, 80, 80], fill=(180, 205, 170))  # green-lit skin
    out = np.array(B.chroma_key(img))
    kept = out[:, :, 3] > 128
    if kept.any():
        r, g, b = out[:, :, 0].astype(int), out[:, :, 1].astype(int), out[:, :, 2].astype(int)
        over = kept & (g > np.maximum(r, b) + 1)
        true(not over.any(), f"{over.sum()} kept pixels still carry green spill")

def t_largest_blob_drops_specks():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 70, 80], fill=(200, 180, 160, 255))   # the subject
    d.rectangle([90, 5, 95, 10],  fill=(200, 180, 160, 255))   # a speck
    out = np.array(B.largest_blob(img).getchannel("A"))
    true(out[50, 40] > 200, "the subject was dropped")
    true(out[7, 92] < 30, "a stray speck survived")

def t_character_fits_its_band():
    img = Image.new("RGB", (512, 768), (30, 185, 45))
    from PIL import ImageDraw
    ImageDraw.Draw(img).ellipse([180, 120, 330, 700], fill=(210, 180, 150))
    import tempfile as _tf
    p = os.path.join(_tf.mkdtemp(), "c.jpg")
    img.save(p)
    ch = B.prepare_character(p)
    true(ch is not None, "no character produced")
    true(ch.width <= int(B.W * B.CHAR_BAND) + 1,
         f"character is {ch.width}px wide, band allows {int(B.W * B.CHAR_BAND)}px")
    true(ch.height <= B.H, f"character is {ch.height}px tall, frame is {B.H}px")

def t_captions_clear_the_character():
    """Text must never be drawn on top of the figure, whichever side it is on."""
    cw = 500
    plate = Image.new("RGB", (B.W, B.H), (0, 0, 0))
    char  = Image.new("RGBA", (cw, B.H), (255, 0, 0, 255))
    words = fake_words([("alpha", 0.0, .5), ("bravo", .5, .5), ("charlie", 1.0, .5)])
    win = B.card_windows(B.group_words(words, "alpha bravo charlie"), 3.0)
    frame = B.compose(plate, win, 0.2, 3.0, 1.0, char)

    y0 = int(B.H * B.CAP_Y) - 20
    y1 = int(B.H * B.CAP_Y) + B.CAP_SIZE + 20
    # the character's own columns, on whichever side it stands
    if B.CHAR_SIDE == "right":
        strip = frame[y0:y1, B.W - cw:]
    else:
        strip = frame[y0:y1, :cw]
    whitish = (strip[:, :, 0] > 200) & (strip[:, :, 1] > 200) & (strip[:, :, 2] > 200)
    true(whitish.sum() < 60,
         f"{whitish.sum()} caption pixels landed on the character")

def t_compose_without_character_still_works():
    plate = Image.new("RGB", (B.W, B.H), (60, 60, 60))
    words = fake_words([("solo", 0.0, .5)])
    win = B.card_windows(B.group_words(words, "solo."), 2.0)
    frame = B.compose(plate, win, 0.2, 2.0, 1.0, None)
    eq(frame.shape, (B.H, B.W, 3))

def t_hook_from_scenes_trims():
    scenes = [dict(narration="One two three four five six seven eight."),
              dict(narration="Nine ten eleven twelve.")]
    hook = B.hook_from_scenes(scenes, max_words=5)
    eq(len(hook.rstrip("…").split()), 5, "hook word count: ")
    true(hook.endswith("…"), "a clipped hook should end with an ellipsis")

def t_hook_short_story_not_clipped():
    scenes = [dict(narration="Just three words.")]
    hook = B.hook_from_scenes(scenes, max_words=40)
    true(not hook.endswith("…"), "a short story should not be clipped")

def t_hook_prefers_the_dramatic_line():
    """The hook picker should choose the shocking beat over flat set-up."""
    scenes = [dict(narration=(
        "It was a normal Tuesday. I made coffee and read the news. "
        "The weather was mild and the traffic was light. "
        "Then my husband looked at our newborn and said: "
        '"I need a DNA test before I sign the birth certificate." '
        "The room went cold."))]
    hook = B.best_hook(scenes)
    true("DNA test" in hook, f"hook missed the dramatic line: {hook!r}")

def t_drama_score_ranks_shock_above_calm():
    calm  = B._drama_score("We ate dinner and talked about the garden.")
    shock = B._drama_score('He said: "The affair is over." She was exposed.')
    true(shock > calm, f"shock {shock} should outscore calm {calm}")

def t_colorize_keeps_all_words():
    hook = '"You have been replaced," my husband said. She owed $580,000.'
    toks = B.colorize_hook(hook)
    got = " ".join(w for w, _ in toks)
    for word in ["You", "husband", "said.", "$580,000."]:
        true(word in got, f"colorize dropped {word!r}")

def t_colorize_money_is_red():
    toks = B.colorize_hook("She was left with $580,000 in debt.")
    reds = [w for w, c in toks if c == B.T_RED]
    true(any("580" in w or "$" in w for w in reds),
         "the money clause should be red")

def t_colorize_quotes_get_accent():
    toks = B.colorize_hook('He said "get out" to me.')
    quoted = [c for w, c in toks if w.strip('"').lower() in ("get", "out") or '"' in w]
    true(any(c != B.T_WHITE for c in quoted), "quoted speech should be accented")

def t_thumbnail_is_written_and_sized():
    import tempfile as _tf
    # a stand-in narrator cutout
    ch = Image.new("RGBA", (400, 700), (200, 180, 150, 255))
    out = os.path.join(_tf.mkdtemp(), "t.jpg")
    B.make_thumbnail('"Hello there," she said. It cost $5.', ch, out)
    true(os.path.exists(out), "thumbnail file was not written")
    im = Image.open(out)
    eq(im.size, (B.THUMB_W, B.THUMB_H), "thumbnail size: ")

def t_thumbnail_without_narrator():
    import tempfile as _tf
    out = os.path.join(_tf.mkdtemp(), "t.jpg")
    B.make_thumbnail("A story with no face.", None, out)
    true(os.path.exists(out), "thumbnail must still render with no narrator")

def t_thumbnail_has_colour():
    """The thumbnail must actually contain the accent colours, not just white."""
    import tempfile as _tf
    out = os.path.join(_tf.mkdtemp(), "t.jpg")
    hook = ("He left without a word. She sold the house, took the car, "
            "and vanished. Then the debt arrived: $580,000 owed.")
    B.make_thumbnail(hook, None, out)
    a = np.array(Image.open(out).convert("RGB")).reshape(-1, 3)
    # a pixel is "coloured" if one channel clearly leads the others
    r, g, b = a[:, 0].astype(int), a[:, 1].astype(int), a[:, 2].astype(int)
    reddish   = ((r > 150) & (r - g > 60) & (r - b > 60)).sum()
    greenish  = ((g > 150) & (g - r > 40) & (g - b > 30)).sum()
    true(reddish > 200, "no red accent found on the money clause")
    true(greenish > 200, "no green accent found in the thumbnail")

def t_output_is_high_definition():
    true(B.H >= 1080, f"frame is only {B.H}px tall; want at least 1080 for HD")
    true(B.W / B.H > 1.7, "frame is not 16:9 widescreen")

def t_narrator_descriptions_read_young():
    for g in ("male", "female"):
        desc = B.describe_narrator(g).lower()
        true("young" in desc or "twenty" in desc,
             f"{g} narrator description does not read young: {desc!r}")

def t_plate_keeps_detail():
    """Compose must not crush the plate to mud — bright input stays bright."""
    plate = Image.new("RGB", (B.W, B.H), (200, 200, 200))
    frame = B.compose(plate, [], 5.0, 10.0, 1.0, None)
    # centre of frame, away from the vignette edges
    cy, cx = B.H // 2, B.W // 2
    true(frame[cy, cx].mean() > 150,
         f"a bright plate came out dim ({frame[cy, cx].mean():.0f}); detail is being crushed")

def t_explicit_narrator_wins():
    """An @ line sets the narrator verbatim and skips gender inference."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# A Mother's Story\n@ a stern man with a grey beard\n"
                "## a kitchen\nAs a mother I did everything. My husband left.\n")
        p = f.name
    try:
        _, narrator, _ = B.parse_script(p)
        eq(narrator, "a stern man with a grey beard",
           "explicit @ must win over the female cues in the text: ")
    finally:
        os.unlink(p)

def t_first_narrator_line_wins():
    """One host for the whole video: only the first @ counts."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n@ the first host\n## a\nOne.\n@ a different host\n## b\nTwo.\n")
        p = f.name
    try:
        _, narrator, _ = B.parse_script(p)
        eq(narrator, "the first host")
    finally:
        os.unlink(p)

def t_narrator_inferred_when_absent():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# T\n## a room\nNarration only.\n")
        p = f.name
    try:
        _, narrator, _ = B.parse_script(p)
        true(narrator, "a narrator must be chosen even with no @ line")
    finally:
        os.unlink(p)

def t_gender_inference():
    female = [
        "As a mother I raised them alone.",
        "My husband walked out on us.",
        "I was a single mom for years.",
        "They called me mom every night.",
        "When I was pregnant with my first child.",
    ]
    male = [
        "As a father I gave them everything.",
        "My wife passed away last spring.",
        "I was a single dad for years.",
        "They called me dad every night.",
        "Being a dad was all I ever wanted.",
    ]
    for txt in female:
        eq(B.infer_gender(txt), "female", f"{txt!r} -> ")
    for txt in male:
        eq(B.infer_gender(txt), "male", f"{txt!r} -> ")

def t_gender_default_when_ambiguous():
    # With no self-reference and no romantic-pronoun skew, these stories skew
    # female-narrated, so that is the safer default.
    eq(B.infer_gender("The house was empty. Nobody came home."), "female",
       "ambiguous text should fall back to the default: ")

def t_gender_from_partner_pronouns():
    """No 'my husband', but a story steeped in 'he/his' is told by a woman."""
    she_leaves_him = ("I found the texts on his phone. His mother watched as I "
                      "read them. He begged me. I took his ring off and left him.")
    eq(B.infer_gender(she_leaves_him), "female",
       "a story about 'him' should read as a female narrator: ")
    he_leaves_her = ("She had been lying for months. Her sister knew. I packed "
                     "her things and told her to go. She cried but I was done.")
    eq(B.infer_gender(he_leaves_her), "male",
       "a story about 'her' should read as a male narrator: ")

def t_daughters_father_reads_male_by_role():
    """Address forms ('the only father', 'Goodnight Dad') must read as male."""
    text = ("I raised my daughters alone. I was the only father who also showed "
            "up as the mother. Goodnight Dad, they said every night.")
    eq(B.infer_gender(text), "male")

def t_describe_narrator_matches_gender():
    true("woman" in B.describe_narrator("female").lower(), "female description")
    true("man" in B.describe_narrator("male").lower(), "male description")

def t_daughters_story_reads_male():
    """The sacrificial-father story must not be voiced by a woman on screen."""
    text = ("I Raised My Daughters Alone For 15 Years. I was the only father "
            "who also showed up as the mother. Goodnight Dad, they said.")
    eq(B.infer_gender(text), "male")

def t_cover_centres_crop():
    src = Image.new("RGB", (2000, 500), (10, 10, 10))
    from PIL import ImageDraw
    ImageDraw.Draw(src).rectangle([980, 0, 1020, 500], fill=(255, 0, 0))
    out = np.array(B.cover(src))
    reds = np.where((out[:, :, 0] > 200) & (out[:, :, 1] < 60))[1]
    true(len(reds) > 0, "centre marker was cropped away")
    mid = (reds.min() + reds.max()) / 2
    true(abs(mid - B.W / 2) < B.W * 0.06,
         f"crop is off-centre: marker centred at {mid:.0f}, frame centre {B.W/2:.0f}")

# ── frame rendering ───────────────────────────────────────────────────
def t_compose_shape_and_type():
    plate = Image.new("RGB", (B.W, B.H), (70, 70, 90))
    words = fake_words([("hello", 0.0, .5), ("world", .5, .5)])
    win = B.card_windows(B.group_words(words, "hello world."), 3.0)
    frame = B.compose(plate, win, 1.0, 3.0, 1.0)
    eq(frame.shape, (B.H, B.W, 3), "frame shape: ")
    eq(frame.dtype, np.uint8, "frame dtype: ")

def t_caption_actually_drawn():
    """A frame with a caption must differ from the same frame without one."""
    plate = Image.new("RGB", (B.W, B.H), (70, 70, 90))
    words = fake_words([("hello", 0.0, .5)])
    win = B.card_windows(B.group_words(words, "hello."), 3.0)
    with_cap = B.compose(plate, win, 0.2, 3.0, 1.0)
    without   = B.compose(plate, [], 0.2, 3.0, 1.0)
    true(not np.array_equal(with_cap, without), "no caption was drawn")

def t_highlight_moves_with_speech():
    """The pill must be on a different word at different times."""
    plate = Image.new("RGB", (B.W, B.H), (30, 30, 30))
    words = fake_words([("alpha", 0.0, 1.0), ("bravo", 1.0, 1.0)])
    win = B.card_windows(B.group_words(words, "alpha bravo"), 2.0)
    early = B.compose(plate, win, 0.3, 2.0, 1.0)
    late  = B.compose(plate, win, 1.3, 2.0, 1.0)
    true(not np.array_equal(early, late),
         "highlight did not move between the two words")

    def pill_x(frame):
        r, g, b = frame[:, :, 0].astype(int), frame[:, :, 1].astype(int), frame[:, :, 2].astype(int)
        mask = (b > 120) & (r > 60) & (r < 190) & (g < 90)
        xs = np.where(mask.any(axis=0))[0]
        return xs.mean() if len(xs) else None

    a, z = pill_x(early), pill_x(late)
    true(a is not None and z is not None, "highlight pill not found")
    true(z > a, f"pill should advance rightward: first at {a:.0f}, then {z:.0f}")

def t_fade_darkens():
    plate = Image.new("RGB", (B.W, B.H), (200, 200, 200))
    full = B.compose(plate, [], 1.0, 3.0, 1.0)
    dark = B.compose(plate, [], 1.0, 3.0, 0.2)
    true(dark.mean() < full.mean() * 0.5, "fade did not darken the frame")

def t_long_card_fits_width():
    """An overlong card must shrink to fit, not run off the frame."""
    plate = Image.new("RGB", (B.W, B.H), (0, 0, 0))
    words = fake_words([("EXTRAORDINARILY", 0.0, .5), ("INCOMPREHENSIBLE", .5, .5),
                        ("UNCHARACTERISTIC", 1.0, .5), ("DISPROPORTIONATE", 1.5, .5)])
    win = B.card_windows(B.group_words(words, " ".join(w[2] for w in words)), 3.0)
    frame = B.compose(plate, win, 0.2, 3.0, 1.0)
    ink = np.where((frame.max(axis=2) > 40).any(axis=0))[0]
    true(len(ink) > 0, "nothing was drawn")
    true(ink.min() >= 0 and ink.max() < B.W,
         f"caption spills outside the frame: x {ink.min()}..{ink.max()}")

def t_ken_burns_moves():
    plate = Image.new("RGB", (B.W, B.H), (0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(plate).rectangle([600, 300, 680, 380], fill=(255, 255, 255))
    a = B.ken_burns(plate, 0.0, 10.0)
    b = B.ken_burns(plate, 9.0, 10.0)
    true(not np.array_equal(np.array(a), np.array(b)), "plate is static")

# ── output integrity ──────────────────────────────────────────────────
def t_verify_rejects_missing():
    ok, why = B.verify_video("/nonexistent/nope.mp4")
    true(not ok, "missing file reported as valid")

def t_verify_rejects_empty():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        p = f.name
    try:
        ok, _ = B.verify_video(p)
        true(not ok, "empty file reported as valid")
    finally:
        os.unlink(p)

def t_verify_rejects_garbage():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"this is not a video" * 500)
        p = f.name
    try:
        ok, _ = B.verify_video(p)
        true(not ok, "garbage reported as valid")
    finally:
        os.unlink(p)

def t_verify_rejects_truncated():
    """
    The exact failure mode that kept surfacing: a real MP4 cut short before its
    index was written. It must be rejected, not accepted as finished.
    """
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    d = tempfile.mkdtemp()
    good = os.path.join(d, "good.mp4")
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "2", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", good],
                   capture_output=True)
    ok, why = B.verify_video(good)
    true(ok, f"a genuinely good file was rejected: {why}")

    truncated = os.path.join(d, "cut.mp4")
    data = open(good, "rb").read()
    open(truncated, "wb").write(data[: len(data) // 3])      # chop off the tail
    ok, why = B.verify_video(truncated)
    true(not ok, "a truncated video passed verification")

def t_verify_detects_missing_audio():
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    d = tempfile.mkdtemp()
    silent = os.path.join(d, "silent.mp4")
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", silent],
                   capture_output=True)
    ok, _ = B.verify_video(silent, expect_audio=True)
    true(not ok, "a video with no audio passed an audio-required check")
    ok, _ = B.verify_video(silent, expect_audio=False)
    true(ok, "a silent video was rejected when audio was not required")

# ── configuration sanity ──────────────────────────────────────────────
def t_font_loads():
    f = B.load_font(B.CAP_SIZE)
    true(f is not None, "no font loaded")
    from PIL import ImageDraw
    w = ImageDraw.Draw(Image.new("RGB", (10, 10))).textlength("TEST", font=f)
    true(w > 0, "font renders zero-width text")

def t_caption_sits_inside_frame():
    true(0.0 < B.CAP_Y < 1.0, f"CAP_Y {B.CAP_Y} is outside the frame")
    true(B.CAP_Y * B.H + B.CAP_SIZE < B.H,
         "captions would be drawn past the bottom edge")

def t_settings_sane():
    true(B.W > 0 and B.H > 0, "bad frame size")
    true(B.FPS >= 24, f"fps {B.FPS} is too low")
    true(B.WORDS_PER_LINE >= 1, "words per line must be at least 1")
    true(len(B.HILITE) == 3 and all(0 <= c <= 255 for c in B.HILITE),
         "highlight colour is not a valid RGB triple")

# ── end-to-end ────────────────────────────────────────────────────────
def t_end_to_end():
    """Render a genuinely short video and verify the artefact."""
    d = tempfile.mkdtemp()
    script = os.path.join(d, "tiny.txt")
    open(script, "w").write(
        "# Test\n## a plain wooden table in soft daylight\n"
        "This is a test. It is very short.\n")
    out = os.path.join(d, "tiny.mp4")
    B.build(script, out, work=os.path.join(d, "work"))
    ok, why = B.verify_video(out, expect_audio=True, min_seconds=1.0)
    true(ok, f"rendered video failed verification: {why}")
    print(f"        rendered {os.path.getsize(out)/1e6:.1f}MB · {why}")

# ── runner ────────────────────────────────────────────────────────────
def main():
    full = "--full" in sys.argv

    groups = [
        ("script parsing", [
            ("parses title, scenes and narration", t_parse_basic),
            ("joins consecutive narration lines", t_parse_multiline_narration),
            ("handles narration before any scene", t_parse_narration_before_scene),
            ("ignores blank lines", t_parse_blank_lines_ignored),
            ("rejects a script with no narration", t_parse_empty_script_rejected),
            ("every shipped script parses", t_parse_real_scripts),
        ]),
        ("auto scene split", [
            ("a long story splits into scenes", t_long_narration_is_split),
            ("short scenes pass through", t_short_scenes_untouched),
            ("segmentation keeps every word", t_segment_keeps_all_narration),
            ("image prompt prefers concrete detail", t_visual_prompt_prefers_concrete),
        ]),
        ("narrator", [
            ("an explicit @ narrator wins", t_explicit_narrator_wins),
            ("only the first @ line counts", t_first_narrator_line_wins),
            ("a narrator is inferred when absent", t_narrator_inferred_when_absent),
            ("gender is inferred from the text", t_gender_inference),
            ("gender from partner pronouns", t_gender_from_partner_pronouns),
            ("a father story reads male by role", t_daughters_father_reads_male_by_role),
            ("gender defaults when ambiguous", t_gender_default_when_ambiguous),
            ("the description matches the gender", t_describe_narrator_matches_gender),
            ("the daughters story reads male", t_daughters_story_reads_male),
            ("narrator descriptions read young", t_narrator_descriptions_read_young),
        ]),
        ("word grouping", [
            ("respects the words-per-card limit", t_group_respects_word_limit),
            ("breaks cards at sentence ends", t_group_breaks_on_sentence_end),
            ("never drops or reorders a word", t_group_keeps_every_word),
            ("handles empty input", t_group_handles_empty),
            ("chunks text with no punctuation", t_group_no_punctuation),
        ]),
        ("caption timing", [
            ("leaves no uncovered time", t_windows_cover_everything),
            ("first card is up from frame one", t_windows_start_at_zero),
            ("windows are ordered and non-overlapping", t_windows_are_ordered),
            ("a single card spans the whole scene", t_windows_single_card),
        ]),
        ("image handling", [
            ("cover fills the frame exactly", t_cover_exact_size),
            ("cover never squeezes the image", t_cover_does_not_squeeze),
            ("cover crops from the centre", t_cover_centres_crop),
            ("fit never distorts aspect", t_fit_preserves_aspect),
            ("fit stays inside its box", t_fit_stays_inside_box),
        ]),
        ("character cutout", [
            ("keys out green, keeps the subject", t_chroma_key_removes_green_keeps_subject),
            ("keeps white clothing", t_chroma_key_keeps_white_clothing),
            ("removes green spill", t_chroma_key_despills),
            ("drops stray specks", t_largest_blob_drops_specks),
            ("character fits its band", t_character_fits_its_band),
        ]),
        ("frame rendering", [
            ("frame has the right shape and type", t_compose_shape_and_type),
            ("caption is actually drawn", t_caption_actually_drawn),
            ("highlight advances with speech", t_highlight_moves_with_speech),
            ("fade darkens the frame", t_fade_darkens),
            ("an overlong card stays inside the frame", t_long_card_fits_width),
            ("ken burns keeps the plate moving", t_ken_burns_moves),
            ("captions clear the character", t_captions_clear_the_character),
            ("renders fine with no character", t_compose_without_character_still_works),
            ("output is high definition", t_output_is_high_definition),
            ("the plate keeps its detail", t_plate_keeps_detail),
        ]),
        ("thumbnail", [
            ("hook trims a long opening", t_hook_from_scenes_trims),
            ("a short story is not clipped", t_hook_short_story_not_clipped),
            ("hook prefers the dramatic line", t_hook_prefers_the_dramatic_line),
            ("drama score ranks shock above calm", t_drama_score_ranks_shock_above_calm),
            ("colorize keeps every word", t_colorize_keeps_all_words),
            ("money turns red", t_colorize_money_is_red),
            ("quotes get an accent colour", t_colorize_quotes_get_accent),
            ("thumbnail is written at the right size", t_thumbnail_is_written_and_sized),
            ("thumbnail renders without a narrator", t_thumbnail_without_narrator),
            ("thumbnail actually has colour", t_thumbnail_has_colour),
        ]),
        ("output integrity", [
            ("rejects a missing file", t_verify_rejects_missing),
            ("rejects an empty file", t_verify_rejects_empty),
            ("rejects garbage", t_verify_rejects_garbage),
            ("rejects a truncated video", t_verify_rejects_truncated),
            ("detects a missing audio track", t_verify_detects_missing_audio),
        ]),
        ("configuration", [
            ("a usable font is available", t_font_loads),
            ("captions sit inside the frame", t_caption_sits_inside_frame),
            ("settings are sane", t_settings_sane),
        ]),
    ]

    if full:
        groups.append(("end to end (network)", [
            ("renders and verifies a real video", t_end_to_end),
        ]))

    for title, tests in groups:
        print(f"\n{title}")
        for name, fn in tests:
            check(name, fn)

    print("\n" + "─" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("─" * 60)
        for name, tb in FAILURES:
            print(f"\n{name}:\n{tb}")
    if not full:
        print("\n  (fast pass — run with --full to include a real render)")
    print()
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
