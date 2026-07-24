# Bay Stories

Turns a written script into a narrated video: full-bleed cinematic plates,
neural voice-over, and karaoke captions where the word being spoken lights up.

Push a script → GitHub renders the video → download it from the release.

## Writing a script

A script is a plain text file with four kinds of line:

| Line | Meaning |
|---|---|
| `#` | The title |
| `##` | The backdrop for this scene |
| `@` | Who stands on the left for this scene |
| anything else | The narration |

```
# I Raised My Daughters Alone For 15 Years

## a man sitting alone on a couch late at night, one warm lamp, melancholy
@ a weary father in his forties, plain shirt, quiet sadness
Fifteen years. That is how long I gave everything I had. No partner. No help.

## a tired father in a diner kitchen at night, worn uniform
I worked two jobs. Sometimes three. I packed lunches at five in the morning.
```

Keep sentences short — each one becomes its own run of caption cards, and short
sentences cut better than long ones.

The `##` line is an image prompt. Describe the shot, not the story: who is in
frame, where they are, what the light is doing. Cinematic styling is added for
you, so there's no need to write "4k, cinematic, photorealistic".

### The narrator

One narrator hosts the whole video — the same cut-out figure on the left of
every scene, the way a presenter stays on screen while the story cuts around
them. They are generated and keyed out once, then reused on every scene, so the
face never changes and never distorts.

You do not have to write the narrator. Leave `@` out and one is chosen to fit
the story: the script and title are scanned for how the storyteller refers to
themselves — "as a mother", "my wife", "they called me dad" — and the
narrator's gender follows. When a story gives no such cue it defaults to a male
host. To set the narrator yourself, write a single `@` line anywhere:

```
@ a woman in her fifties, grey cardigan, tired kind eyes
```

An explicit `@` always wins over the automatic guess, and only the first one
counts — there is a single host, not one per scene.

The figure is generated against a green screen and keyed out. A white studio
backdrop looks tidier but cannot be cut reliably: a white shirt against a white
wall is genuinely ambiguous, and the clothes get erased along with the wall.
Nothing on a person is green, which is why the technique has survived in
broadcast for seventy years.

## Length

There is no built-in limit — the video is as long as the script. Each scene is
rendered and verified on its own and then joined, so memory use stays flat
whether the story runs two minutes or fifty.

Two practical limits for very long videos rendered on GitHub:

- **Time.** A GitHub job may run up to 6 hours; the workflow is capped a little
  under that. Rendering runs at roughly real-time-ish per scene plus image
  generation, so a 50-minute story is a multi-hour run. It fits, but it is not
  quick.
- **Image generation.** A feature-length script is dozens of `##` scenes, each
  a call to the image API. Calls are retried three times, but more scenes mean
  more chances for a slow or failed one, which lengthens the run.

For long-form, render on a branch or trigger it by hand from the Actions tab
rather than on every push, and keep each scene's narration to a few sentences
so captions stay readable.

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
