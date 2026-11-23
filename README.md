# CrayonJapan Auto-Maker

Automated making-of video pipeline for **CrayonJapan** (<https://crayonjapan.base.shop/>).
Converts raw hand-drawn **Sakura Cray-Pas Specialist** process videos into 9:16 vertical
English YouTube Shorts, and adapts the narrative style based on how much of each Short
viewers actually watch.

## What it does

1. **Finish-preview hook** – the last few seconds of the drawing (the finished postcard) are
   cut at natural speed into the first 2.5 s.
2. **Vertical time-lapse** – the whole recording is sped up and fitted to 1080x1920 (9:16).
   A project of several recordings is rendered clip by clip: each raw clip becomes its own
   short 9:16 segment proportional to its length, and only those small segments are joined.
3. **Slow finish + closing zoom (finale)** – the tail of the last clip is replayed at 0.75x
   so the finished piece is actually looked at, then the scanned artwork is pushed in slowly
   (Ken Burns, 1.0 → 1.3 over ~4 s, 1080x1920 30 fps). The scan is first fitted into the frame
   divided by the zoom, then padded, so even a landscape postcard keeps every edge: at the end
   of the pan it fills the width exactly and nothing is cropped. Both parts are ordinary
   project segments, so the `-c copy` concat and the ASMR pass layout are unchanged.
4. **Finished artwork still** – a scanned drawing placed in the project folder becomes the
   artwork JPEG (whole sheet, never cropped, shrunk to 1080 px wide) and is given to Gemini;
   without a scan, the completed illustration is pulled from the last frame of the last clip.
5. **Gemini script** – four keyframes (plus the scanned artwork when there is one) are sent to
   `gemini-3-flash-preview`, which returns the title, on-screen hook text, English narration,
   description and tags as JSON. The narration is written to run the whole Short (100-120
   words, ~40-45 s: hook in the first 3 s, continuous process description up to 35 s, then the
   call to action) instead of leaving the second half silent, and it is grounded in the
   artist's own channel in `config.yaml` (`brand.artist_notes`). The client is pinned to
   **`v1beta`** (the `gemini-3` models are not served on `v1`, which answers `404 NOT_FOUND`),
   and if a model is retired, out of free-tier quota or overloaded the next one in
   `FALLBACK_MODELS` is tried instead.
6. **Voice + telop** – `edge-tts` (en-US-JennyNeural) narrates, and the word timings it
   reports become the on-screen telop (see *Telop* below); the SRT cues are still written for
   the caption track. The narration is measured after it is synthesised: if it runs
   longer than the planned cut, the closing zoom grows (3-8 s) so `-shortest` never cuts the
   last line, and the whole Short stays under the 58 s guard.
7. **Shop call to action + upload** – the description is closed with the same two-language
   shop block every time (`Original artwork available at: <shop url>` and
   `1点ものの原画はこちらで販売中です：<shop url>`, in front of the hashtags), so the link to the
   BASE shop where the original is sold cannot go missing from a Short; then the Short is
   uploaded (unlisted by default) and one row is appended to `analytics/performance.csv`.
   A plain-text upload pack and an `art_metadata.json` are written next to the video.
8. **Learning loop** – `analytics_updater.py` refreshes views / likes / comments from the
   YouTube Data API and the average view percentage from the YouTube Analytics API.
   `choose_best_style()` then reuses the best-performing style, with a 25 % exploration rate
   so the other styles keep getting a chance.
9. **Google Drive hub (phase 2)** – `drive_sync.py` watches a Drive folder, renders each
   waiting artwork (a `GO_` project folder or a loose recording) and drops the finished
   package into a second folder, then moves the source into an archive folder. See
   *Google Drive hub* below.

## Requirements

- Python 3.10+
- FFmpeg 5+ with `libx264`, the `subtitles` and `ass` (libass) filters, and `ffprobe` on `PATH`
- A bold sans font the caption can rely on: `fonts-dejavu-core` is installed by the workflow
  (DejaVu Sans Bold); Liberation Sans is used when that is the face present
- A Gemini API key (Google AI Studio)
- A Google Cloud OAuth client (type: *Desktop app*) with **YouTube Data API v3** and
  **YouTube Analytics API** enabled for the channel the videos are uploaded to (`@CrayonJapan`)

## Telop

`telop.py` lays out the on-screen text as an ASS document built from the word timings
`edge-tts` reports (`*_words.json`); `main.py` burns it with FFmpeg's `ass` filter instead of
the plain SRT cues. Every rule below is covered by `tests/test_telop.py`.

| what | how |
| --- | --- |
| Reading comfort | at most **two lines** per card, **one message** per card, and cards close on a sentence end. Lines break on word boundaries only; a card's trailing `.` / `,` / `;` is dropped (`!` and `?` stay, they carry the tone). A card that would flash by in under 0.85 s is merged into a neighbour. |
| Placement | the caption band sits `MarginV = 380` from the bottom with 170 px side margins, so it clears the Shorts title/description, the like-comment-share column (140 px) and the top bar. The hook lives in the upper band (300 px) and the fixed title at 180 px; the closing line takes the caption band once the captions have left, so the top edge of the drawing the zoom outro shows stays clear. |
| Type | DejaVu Sans Bold (auto-detected; Liberation Sans as the fallback) with a black outline and a drop shadow, legible on white paper and on saturated colour. |
| Emphasis | a keyword or a number is gold and 106 % (gold `&H14C8FF&`); the script can also mark its own words with `**word**`, which are removed before synthesis and highlighted on screen. |
| Karaoke | one overlay per spoken word, on a layer above the card: that word turns gold, grows to 112 % (settling to 106 %) and gets an underline. Every word gap is drawn as a space plus a hard space, in the card and the overlay alike, because one space is not enough room for the grown word: at 124 % its outline ran into its neighbour (`creamyblue`). |
| Hook | `hook_text` pops in (45 % -> 112 % -> 100 %) over the first 3 s, and always shows its whole sentence: when it does not fit at 96 px the font steps down 2 px at a time (never below 58 px) into two lines, or three when it needs them. It is never cut with an ellipsis. |
| Fixed title | `CrayonJapan / <title>` (hashtags and emoji removed, accented letters like `é` kept, cut on a word boundary) at 40 px, 60 % transparent, for the whole Short. |
| Closing line | `telop.cta_text` from `config.yaml` over the outro (`Follow for more oil pastel art!`), in the caption band for at least 2.4 s; when the narration runs so late that the captions are still on screen it stacks just above them instead of drawing gold over white. |

`telop.enabled: false` in `config.yaml` puts the plain SRT burn back; `telop.font` pins a font
family instead of the auto-detect. The SRT is written either way and stays in the upload pack.

## Setup

```bash
git clone https://github.com/shodai4/crayonjapan-auto-maker.git
cd crayonjapan-auto-maker
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

1. Create `.env` in the project root (`cp .env.example .env`) and fill in:

   ```dotenv
   GEMINI_API_KEY=your_api_key
   DRIVE_RAW_FOLDER_ID=...       # only needed for --drive-once / --drive-watch
   DRIVE_READY_FOLDER_ID=...
   DRIVE_ARCHIVE_FOLDER_ID=...
   ```

2. Download the OAuth client JSON from Google Cloud Console and save it as
   `client_secret.json` in the project root.
3. Enable **YouTube Data API v3**, **YouTube Analytics API** and **Google Drive API** for that
   Cloud project.
4. Run `python main.py`. On the first run a browser opens for the OAuth consent screen; the
   analytics read scope is requested together with the upload scope, and the resulting
   `token.json` is reused afterwards. Drive keeps its own `drive_token.json` (same client
   JSON, Drive scope only), created the first time Drive mode runs.

## Usage

```bash
python main.py                       # watch ./input and process every dropped .mp4/.mov/.avi
python main.py --input clip.mp4      # process one file and exit (handy for testing)
python main.py --drive-once          # scan the Drive raw folder once, then exit
python main.py --drive-watch         # keep scanning the Drive raw folder
python main.py --drive-once --no-upload   # render + metadata only, no YouTube upload
python analytics_updater.py          # refresh views / likes / comments / avg view %
```

Output files land in one folder per drawing, `./output/{base_name}_{YYYYMMDD_HHMM}/`
(named in JST, the same name the package gets inside Drive's `02_READY_TO_POST`):

| file | what it is |
| --- | --- |
| `*_vertical.mp4` | 9:16 time-lapse (intermediate) |
| `*.mp3`, `*.srt` | narration and word-level SRT cues (intermediate) |
| `*_words.json`, `*.ass` | word timings and the telop document that is burned into the picture |
| `*_final.mp4` | the upload-ready Short |
| `*_artwork.jpg` | the finished drawing, full frame, for thumbnail / shop listing |
| `*_metadata.txt` | paste-ready title, hook text, description, tags |
| `*_art_metadata.json` | the same artwork data as structured metadata for the shop |

## Google Drive hub

Folder layout (create it once in your own Drive; nothing personal is stored in this repo):

```
AI秘書 / 12_動画自動作成/
    01_RAW_VIDEO/          drop the raw drawing recording(s) here
        20261121_sushi/        <- one artwork while you are still uploading
            part1.mp4
            part2.mp4
            finish.mp4
            scan.jpg
        GO_20261121_sushi/     <- renamed when the artwork is ready to render
        GO_20261122_cat/       <- another finished artwork, waiting
    02_READY_TO_POST/      one folder per rendered Short, named like the output folder
        20261121_sushi_20261121_1830/
            20261121_sushi_final.mp4
            20261121_sushi_artwork.jpg
            20261121_sushi_metadata.txt
            20261121_sushi_art_metadata.json
    03_ARCHIVE/            the GO_ folder (or loose recording) moved here once rendered
```

Copy each folder ID from its URL (`https://drive.google.com/drive/folders/<ID>`) into `.env` as
`DRIVE_RAW_FOLDER_ID`, `DRIVE_READY_FOLDER_ID` and `DRIVE_ARCHIVE_FOLDER_ID`.

### One artwork = one `GO_` folder

Put all the files of one artwork in a subfolder of `01_RAW_VIDEO`:

- **raw drawing clips** – `.mp4` / `.mov` / `.avi`, any number, any length. They are sorted
  naturally (`part2.mp4` before `part10.mp4`), and a clip named `finish`, `final`, `complete`,
  `end` or `完成` is always put last so the finish-preview hook comes from it.
- **scanned artwork** – `.jpg` / `.png` of the finished drawing. The first one is used as the
  artwork JPEG and as Gemini's reference image.

`GO_` is the signal: **while the folder has any other name it is ignored**, so you can upload
for an hour and rename the folder (or just type `GO_` in front of its name) when it is complete.
The prefix is stripped from the output names, so `GO_20261121_sushi` produces
`20261121_sushi_final.mp4`, `20261121_sushi_artwork.jpg`, `20261121_sushi_metadata.txt` and
`20261121_sushi_art_metadata.json`, all inside its own package folder
`20261121_sushi_{YYYYMMDD_HHMM}/`.

A loose `.mp4` / `.mov` / `.avi` dropped straight into `01_RAW_VIDEO` still works exactly as
before (one recording, artwork from its last frame).

### One scan cycle

1. lists `01_RAW_VIDEO` and picks up every `GO_` folder plus any loose video that is not
   already processed;
2. skips a loose video modified in the last 60 s (`--drive-min-age`) so a still-uploading file
   is never downloaded half-finished; a `GO_` folder only waits a few seconds after its rename,
   because the rename itself is the "done" signal;
3. downloads the clips **one at a time**, renders each into its short 9:16 segment and deletes
   the raw copy again, so an hour-long 6 GB recording never becomes a second 6 GB file on disk;
4. joins the hook (2.5 s from the end of the last clip) with the segments and finishes the
   Short (script, voice, telop);
5. uploads the four artifacts into their own subfolder of `02_READY_TO_POST`,
   named `{base_name}_{YYYYMMDD_HHMM}` (the same name as the local output folder;
   an existing folder of that name is reused, so a retry never leaves two);
6. moves the whole `GO_` folder (or the loose file) to `03_ARCHIVE` and records its id in
   `drive_state.json`, so an interrupted run repeats at most one artwork instead of
   re-rendering everything.

`--drive-interval` (or `DRIVE_WATCH_INTERVAL_SECONDS`) sets the scan interval; the default is
60 s. `--no-upload` leaves the YouTube upload to you: the package simply waits in
`02_READY_TO_POST` until you check it on your phone. Without it the Short is uploaded unlisted
(`config.yaml`), so nothing goes public by itself.

## Hand-off mode: Apps Script + GitHub Actions

No always-on PC. `gas/Code_drive_watch.gs` is pasted into the Apps Script project that already
runs the message-board relay; its 1-minute trigger watches `01_RAW_VIDEO` and posts a
`repository_dispatch` (`raw_video_uploaded`) to this repository. `.github/workflows/auto-edit.yml`
then runs the same `--drive-once --no-upload` pipeline on a free `ubuntu-latest` runner:

```
phone → 01_RAW_VIDEO (GO_ folder) → Apps Script (1 min) → repository_dispatch
      → Actions (FFmpeg/OpenCV/Gemini) → 02_READY_TO_POST + folder moved to 03_ARCHIVE
      → artifact kept on the run for 14 days
```

Apps Script script properties: `RAW_FOLDER_ID`, `GITHUB_REPO` (`owner/repo`) and `GITHUB_TOKEN`
(a fine-grained PAT for that one repository with *Contents: read and write* — GitHub only accepts
a `repository_dispatch` from a token that can write code — and no other permission).

Repository secrets: `GEMINI_API_KEY`, `DRIVE_RAW_FOLDER_ID`, `DRIVE_READY_FOLDER_ID`,
`DRIVE_ARCHIVE_FOLDER_ID`, plus one Drive credential:

- `GDRIVE_TOKEN_JSON` – the content of `drive_token.json`; long-lived, lets a scheduled or manual
  run work on its own.
- or nothing at all: each dispatch carries `GDRIVE_ACCESS_TOKEN`, a Drive access token minted by
  `ScriptApp.getOAuthToken()` that lives for about an hour. The workflow masks it before use.

`python scripts/verify_drive_access.py` checks that credential and prints how many files are
waiting, without printing the credential itself.

A double dispatch is harmless: `01_RAW_VIDEO` is scanned on every run and a `GO_` folder (or a
loose file) only leaves it once the package is uploaded, so a second run simply finds nothing to
render.

## Narrative styles

| style | intent |
| --- | --- |
| `style_A_material` | texture, pigment and colour layering of the oil pastels |
| `style_B_story` | warm story about the everyday moment behind the motif |
| `style_C_asmr` | minimal narration; the drawing sound itself carries the video |

Whichever style is chosen, the prompt asks for the three things a Short needs to travel,
woven into the narration rather than read as a list: a **curiosity-gap hook** in the first
sentence (0-3 s), **sensory language** for the material (`creamy`, `buttery`, `vibrant`,
`crisp`), and a **loop-friendly ending** that leads back to the opening or lands on a
punchline. The narration length, the JSON keys and the Japanese translation are unchanged.

`style_C_asmr` is the one style that keeps the original audio: `build_vertical_timelapse(...,
keep_audio=True)` keeps the pastel-on-paper sound, and `main.py` mixes that track under the
narration with `amix`. Because FFmpeg's `atempo` only accepts 0.5–2.0 per stage, the
time-lapse speed is applied as a chain (e.g. 10x becomes `atempo=2,atempo=2,atempo=2,atempo=1.25`).
If the source has no audio stream, the render simply stays muted instead of failing.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests cover style selection, CSV handling, FFmpeg command assembly, `atempo` chaining,
the audio-mixing branch, JSON parsing of Gemini replies, the hook/sensory/loop prompt rules,
upload body limits, finished-artwork
extraction (OpenCV path and FFmpeg fallback), scanned-artwork resizing, the per-clip project
plan and its FFmpeg commands, the metadata bundle, the per-render package folder, the Drive
sync orchestration
(GO_ folder discovery, natural sorting, prefix stripping, download / upload / archive / state,
the 02_READY_TO_POST subfolder, mocked Drive client) and project file invariants.
They need `pandas`, `pyyaml` and `opencv-python` only – no FFmpeg run, API key or network
access.

For a real end-to-end check with FFmpeg (synthetic clips, different resolutions and audio,
plus a scanned PNG), run:

```bash
python3 scripts/verify_project_pipeline.py --ffmpeg "$(command -v ffmpeg)"
```

## Repository layout

```
config.yaml              brand, video, style-evolution and upload settings
main.py                  pipeline entry point, local watcher and Drive modes
video_processor.py       9:16 time-lapse, hook cut, ASMR audio retention, artwork still
ai_writer.py             style selection + Gemini script generation
metadata_writer.py       English upload pack (.txt) + art metadata (.json)
drive_sync.py            Google Drive hub: list / download / upload / archive
scripts/verify_drive_access.py  CI pre-flight check for the Drive credential
.github/workflows/auto-edit.yml  Actions job: dispatch → render → Drive
gas/Code_drive_watch.gs  Apps Script half: 1-minute folder watch → repository_dispatch
youtube_uploader.py      OAuth, Data API upload, Analytics API client
analytics_updater.py     performance.csv refresh
analytics/performance.csv one row per uploaded Short
tests/                   unittest suite
```

## Security

- `.env`, `client_secret.json`, `token.json` and `drive_token.json` hold credentials and are
  git-ignored. Never commit them; if one leaks, revoke it in Google Cloud Console / AI Studio
  and re-issue.
- `input/` and `output/` (raw footage and renders) are git-ignored as well, and so is
  `drive_state.json` (it lists your personal Drive file IDs).
- This repository is public: the Drive folder IDs are read from `.env`
  (`DRIVE_*_FOLDER_ID`), never hard-coded in the code or committed.
- The OAuth tokens are stored locally only; the scopes requested are
  `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly` for YouTube and
  `drive` for Drive mode.
- In CI the credentials arrive as encrypted repository secrets or in the dispatch payload, are
  masked with `::add-mask::` before the first command, and are never written to a tracked file.
  A dispatched access token expires within the hour and grants only what the Apps Script project
  is already authorized for.

## Notes and limitations

- Artworks are processed one at a time; the watcher keeps running if a single one fails.
- Drive mode needs an item that is fully uploaded. A loose file touched in the last 60 s is
  skipped until a later scan; a `GO_` folder only waits about 15 s after the rename.
- A failed render or upload leaves the `GO_` folder in `01_RAW_VIDEO`; the next scan tries it
  again. Only successes go to `03_ARCHIVE`.
- Long recordings: each clip is downloaded, time-lapsed into its short segment and deleted
  before the next one is pulled, and a clip is only downloaded when at least 1.5 GB would stay
  free. Rendering is dominated by decoding the source (roughly a few minutes per hour of
  1080p), so several hour-long clips in one artwork can approach the free runner's 1-hour
  limit; the dispatch token also expires after about an hour, so keep one artwork per run for
  very long material.
- The scanned image is the finished artwork; if you would rather take it from the video, leave
  the image out of the folder and `--artwork-offset 3` grabs a frame three seconds earlier.
- The mix uses `amix=...:normalize=0`, which needs FFmpeg 4.4 or newer.
- A freshly uploaded Short has no Analytics rows for a while; until then its
  `avg_view_percentage` stays at its previous (or 0) value and the update is only logged.
- `default_privacy` is `unlisted` in `config.yaml`; set it to `public` when you are ready for
  the Short to go live immediately.
