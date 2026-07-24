# Bay Stories

Turns a written script into a narrated video: full-bleed cinematic plates,
neural voice-over, and karaoke captions where the word being spoken lights up.

Push a script → GitHub renders the video → download it from the release.

## Writing a script

A script is a plain text file. `#` is the title, `##` starts a new scene and
describes the picture for it, and the lines underneath are what gets narrated.

```
# I Raised My Daughters Alone For 15 Years

## a man sitting alone on a couch late at night, one warm lamp, melancholy
Fifteen years. That is how long I gave everything I had. No partner. No help.

## a tired father in a diner kitchen at night, worn uniform
I worked two jobs. Sometimes three. I packed lunches at five in the morning.
```

Keep sentences short — each one becomes its own run of caption cards, and short
sentences cut better than long ones.

The `##` line is an image prompt. Describe the shot, not the story: who is in
frame, where they are, what the light is doing. Cinematic styling is added for
you, so there's no need to write "4k, cinematic, photorealistic".

## Rendering on GitHub

1. Add your script to `scripts/` and push it.
2. The **Render story** workflow starts on its own.
3. When it finishes, the video is attached to a new release, and also available
   as a run artifact under the Actions tab.

Only the scripts changed by that push are rendered. To re-render something, go
to **Actions → Render story → Run workflow** and either name a script
(`scripts/my-story.txt`) or leave it blank to rebuild everything.

A run takes roughly 10–25 minutes depending on how long the story is — most of
it is waiting on image generation.

## Rendering locally

```bash
pip install -r requirements.txt
python bay_studio.py --script scripts/example-daughters.txt --out ~/Desktop/story.mp4
```

`--voice` picks a different narrator; `edge-tts --list-voices` shows the
options. `en-US-ChristopherNeural` is the default.

## How the captions stay in sync

Captions are never written by hand. The narration is the single source of
truth: it is sent to the voice engine, and the engine reports back the exact
start time and duration of **every individual word**. Captions are built from
those timings.

This is deliberate. There is no second copy of the text that could drift out of
step with the audio, so captions and voice cannot disagree — the failure mode
where subtitles slowly slide out of sync simply isn't reachable.

## Layout

| Setting | What it does |
|---|---|
| `CAP_SIZE` | Caption size |
| `CAP_Y` | How far down the frame captions sit (`0.585` ≈ just below centre) |
| `WORDS_PER_LINE` | Words visible at once |
| `HILITE` | Colour of the pill behind the spoken word |
| `STROKE` | Thickness of the black outline around the text |
| `VOICE` / `RATE` / `PITCH` | Narrator and delivery |

All are at the top of `bay_studio.py`.

## Cost

Runs on GitHub-hosted runners. Public repos render free; private repos draw
from the monthly Actions minutes on your plan.

Images come from [Pollinations](https://pollinations.ai) and the voice from
Microsoft Edge's neural TTS. Neither needs an API key.
