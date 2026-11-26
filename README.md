# CrayonJapan Auto-Maker

Automated making-of video pipeline for **CrayonJapan** (<https://crayonjapan.base.shop/>).
Converts raw hand-drawn **Sakura Cray-Pas Specialist** process videos into 9:16 vertical
YouTube Shorts, and re-tunes the way they are made from how much of each Short people
actually watch.

## What it does

1. **Opening hook (0-3 s)** – by default a 0.4 s flash of the *finished* artwork, then the
   last seconds of the drawing at natural speed, then the blank page the time-lapse starts
   from: "this is where it ends" before "this is where it begins". The drawing sound under
   the hook is boosted (+6 dB) because that paper-and-pastel noise is the strongest reason to
   stay. `hook_style` switches it to the previous finish preview or to text only.
2. **Vertical time-lapse, 20-32 s by default (28 s)** – the whole recording is sped up and
   fitted to 1080x1920 (9:16). A project of several recordings is rendered clip by clip: each
   raw clip becomes its own short 9:16 segment proportional to its length, and only those
   small segments are joined. `tempo` plays the body faster than the target implies when the
   reflection asks for it.
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
   description and tags as JSON, plus the Japanese title / hook / narration for the check and
   the on-screen Japanese. The voice is budgeted from the target length (about 62 % of the
   Short: 40-54 words at 28 s) instead of running the whole way through, so the drawing sound
   keeps a share of it; the prompt is grounded in the artist's own channel in `config.yaml`
   (`brand.artist_notes`). The client is pinned to **`v1beta`** (the `gemini-3` models are not
   served on `v1`, which answers `404 NOT_FOUND`), and if a model is retired, out of free-tier
   quota or overloaded the next one in `FALLBACK_MODELS` is tried instead.
6. **Voice + telop** – `edge-tts` narrates: en-US-JennyNeural normally, ja-JP-NanamiNeural
   when the run's `language_mode` is `ja`. The word timings it reports become the on-screen
   telop (see *Telop* below); the SRT cues are still written for the caption track. When a
   Japanese voice answers with no usable word boundaries the captions are cut into short
   sentence cues and spread over the measured voice. The narration is measured after it is
   synthesised: if it runs longer than the planned cut, the closing zoom grows (3-8 s) so
   `-shortest` never cuts the last line, and the whole Short stays under the 58 s guard.
   The voice can also be switched off (`tts_mode: none`): then a silent track keeps the mux
   valid and the drawing sound carries the Short.
7. **Shop call to action + upload** – the description is closed with the same two-language
   shop block every time (`Original artwork available at: <shop url>` and
   `1点ものの原画はこちらで販売中です：<shop url>`, in front of the hashtags), so the link to the
   BASE shop where the original is sold cannot go missing from a Short; then the Short is
   uploaded (`public` by default, see `config.yaml`) and one row is appended to
   `analytics/performance.csv`.
   A plain-text upload pack and an `art_metadata.json` are written next to the video. The pack
   also carries a paste-ready **TikTok / Instagram Reels / Pinterest** caption block, built
   locally from the same script (see *Overseas captions* below).
8. **Learning loop (PDCA)** – after the 48 h gate, a run restores the history from Drive,
   collects the numbers of the Shorts that are at least a day old, asks Gemini what to change
   for the next one, renders with those parameters, and saves the history back to Drive. See
   *Learning loop* below. `choose_best_style()` still picks the narrative style from the same
   data, with a 25 % exploration rate so the other styles keep getting a chance.
9. **Google Drive hub (phase 2)** – `drive_sync.py` watches a Drive folder, renders each
   waiting artwork (a `GO_` project folder or a loose recording) and drops the finished
   package into a second folder, then moves the source into an archive folder. A run only
   makes **one** Short and only when the last one is at least **48 h** old. See
   *Google Drive hub* below.
10. **Windows local mode** – the same hub with three folders on the artist's own PC instead
    of Drive: `01_RAW_VIDEO` → render → `02_READY_TO_POST` → upload → `03_ARCHIVE`, with no
    Google Drive call anywhere. `setup_local.bat` / `run_local.bat` and a Task Scheduler
    entry make it a double-click (or a daily slot), `--doctor` checks the PC and `--dry-run`
    shows what a run would do. See *Windows local mode* below and
    `docs/SETUP_WINDOWS_LOCAL.md` (Japanese walk-through).
11. **Never needs Gemini** – if `GEMINI_API_KEY` is absent, out of quota or refused (403 /
    429 / 5xx / billing stopped), the script falls back to templates built from
    `config.yaml` and the artwork's own name: title, hook, narration (sized to the run's
    word budget), its Japanese translation, description, tags and the overseas captions.
    The Short is still voiced, teloped, packed and uploaded.

## Requirements

- Python 3.10+
- FFmpeg 5+ with `libx264`, the `subtitles` and `ass` (libass) filters, and `ffprobe` on `PATH`
- A bold sans font the caption can rely on: `fonts-dejavu-core` is installed by the workflow
  (DejaVu Sans Bold); Liberation Sans is used when that is the face present
- **No Japanese font to install**: `assets/fonts/NotoSansJP-Bold.otf` (a bold Noto Sans CJK JP
  subset to the JIS X 0208 character set, ~4 MB, SIL OFL - see `assets/fonts/README.md`) ships
  with the repository and is handed to libass through `fontsdir=`, because the workflow may
  not be edited to install one. Kana and kanji render on the runner because of it.
- A Gemini API key (Google AI Studio) – **optional**: without one the script is written
  from templates (see *Never needs Gemini* above), so a render and upload still complete
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
| Closing line | `cta_text` (and `cta_text_ja`) from `config.yaml` over the outro (`Follow for more oil pastel art!` / `フォローしてね！`), in the caption band for at least 2.4 s; when the narration runs so late that the captions are still on screen it stacks just above them instead of drawing gold over white. |
| Question | `cta_question_ja` / `cta_question_en` (`次は何を描いてほしい？` / `What should I draw next?`) rides 300 px above the closing line for the same stretch, asking for the next motif in the comment column. |
| Japanese | kana and kanji are drawn with the bundled **Noto Sans CJK JP** (`\fn` per run, `fontsdir=` on the `ass` filter), measured with the same file so a Japanese line wraps by character, never mid-frame. A line never starts with `。` `、`. |
| Bilingual | `language_mode: bilingual` puts the Japanese hook large with the English line under it at 62 % and anchors the block under the top bar; the title follows with the Japanese title and the English one under it, starting after the hook so the two never overlap. |

`telop.enabled: false` in `config.yaml` puts the plain SRT burn back; `telop.font` pins a font
family instead of the auto-detect. The SRT is written either way and stays in the upload pack.

## Overseas captions

TikTok, Instagram Reels and Pinterest are served in English, so `{stem}_metadata.txt` ends with
a block that can be pasted into each app as it is:

```
=== OVERSEAS SNS CAPTIONS (TikTok / Instagram Reels / Pinterest) ===
KEYWORDS : Oil Pastel / Sakura Cray-Pas
HASHTAGS : #oilpastel #sakuracraypas #crayonart #artprocess #oilpastelart #arttok ...

--- TikTok ---
Oil Pastel on paper with Sakura Cray-Pas Specialist: A sakura petal pressed into creamy pink layers.
Layered until the colour glows.

#oilpastel #sakuracraypas #crayonart #artprocess #oilpastelart #arttok
```

The same block in Japanese follows it under `=== OVERSEAS SNS CAPTIONS (JA / 日本語訳) ===`,
for the artist's check before posting.

| what | how |
| --- | --- |
| Search keywords | **Oil Pastel** is the phrase overseas viewers search for, so it opens every caption; **Sakura Cray-Pas** names the Japanese maker beside it. Both are in `KEYWORDS`. |
| Hashtags | `#oilpastel` `#sakuracraypas` `#crayonart` `#artprocess` lead every platform's tag line; TikTok, Instagram and Pinterest each get their own extras after those four. |
| Subject | one English sentence from Gemini (`overseas_caption`) names the artwork; the artist's hook, then the title, stands in when the reply has no sentence. Emoji, hashtags, links and Japanese are stripped, and a long line is cut on a word boundary. |
| Platform shape | TikTok stays short, Instagram Reels adds the 1-of-1 / link-in-bio line, Pinterest adds the BASE shop link (it is the one that allows a link in the caption). |
| Without Gemini | the block is assembled by `metadata_writer.build_overseas_captions` from the script and `config.yaml` alone, so an outage or an offline run still writes every keyword and hashtag. Nothing is posted automatically: the block is text in the pack. |

## Setup

```bash
git clone https://github.com/shodai4/crayonjapan-auto-maker.git
cd crayonjapan-auto-maker
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

1. Create `.env` in the project root (`cp .env.example .env`) and fill in:

   ```dotenv
   GEMINI_API_KEY=your_api_key     # optional: the template script covers a missing key
   DRIVE_RAW_FOLDER_ID=...       # only needed for --drive-once / --drive-watch
   DRIVE_READY_FOLDER_ID=...
   DRIVE_ARCHIVE_FOLDER_ID=...
   # in CI only: the content of token.json as one JSON string (YOUTUBE_TOKEN_JSON)
   ```

2. Download the OAuth client JSON from Google Cloud Console and save it as
   `client_secret.json` in the project root.
3. Enable **YouTube Data API v3**, **YouTube Analytics API** and **Google Drive API** for that
   Cloud project.
4. Run `python main.py`. On the first run a browser opens for the OAuth consent screen; the
   analytics read scope is requested together with the upload scope, and the resulting
   `token.json` is reused afterwards. Drive keeps its own `drive_token.json` (same client
   JSON, Drive scope only), created the first time Drive mode runs.

On Windows, `setup_local.bat` does steps 0-4 for the local mode (virtual environment, packages,
`.env`, the three folders) and then checks the PC; see *Windows local mode* below.

## Usage

```bash
python main.py                       # watch ./input and process every dropped .mp4/.mov/.avi
python main.py --input clip.mp4      # process one file and exit (handy for testing)
python main.py --drive-once          # scan the Drive raw folder once, then exit
python main.py --drive-watch         # keep scanning the Drive raw folder
python main.py --drive-once --no-upload   # render + metadata only, no YouTube upload
python main.py --drive-once --ignore-schedule   # post even inside the 48 h window (test runs)
python analytics_updater.py          # refresh views / likes / comments / avg view %

# Windows local mode (no Drive)
python main.py --local-init          # create 01_RAW_VIDEO / 02_READY_TO_POST / 03_ARCHIVE
python main.py --local-once          # the posting slot: render one, upload, archive
python main.py --local-watch         # keep scanning the local raw folder
python main.py --dry-run             # show the gate and the queue, change nothing
python main.py --doctor              # check FFmpeg, folders, tokens before a real run
python main.py --auth                # the one browser sign-in (saves token.json)
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
| `*_metadata.txt` | paste-ready title, hook text, description, tags, plus the TikTok / Instagram Reels / Pinterest captions |
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

With `schedule.enabled: true` in `config.yaml` a publishing `--drive-once` run is a **posting
slot**, not a queue drain: it first reads the newest video on the channel and only then touches
Drive. (A `--no-upload` run publishes nothing and is left ungated.)

0. **interval gate** – `channels.list(part="contentDetails", mine=True)` gives the uploads
   playlist, `playlistItems.list(maxResults=1)` its newest entry, and that
   `snippet.publishedAt` is the last posting moment. Younger than `min_interval_hours` (48 h):
   the run logs how long is left and exits 0 without downloading, rendering or uploading
   anything. The channel is the only source used: a local file time or an old
   `02_READY_TO_POST` package from a test run would say the wrong thing. If the channel cannot
   be read (expired token, quota, network) the run is held and exits 1, because posting
   without knowing the interval could break it. No video on the channel at all: the first
   Short may go out right away.
1. lists `01_RAW_VIDEO` and picks up `GO_` folders plus any loose video that is not already
   processed, oldest name first;
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

The slot is worth **`max_videos_per_run` (1)** artwork: only the oldest waiting item is rendered
and published, and everything else stays in `01_RAW_VIDEO` for the next slot. So the artist can
keep dropping `GO_` folders all day and the channel still gets one Short every 48 h, in the
order the drawings were made.

`--drive-interval` (or `DRIVE_WATCH_INTERVAL_SECONDS`) sets the scan interval of
`--drive-watch`; the default is 60 s. The interval gate belongs to `--drive-once` (the CI path);
the local watcher is for testing and renders whatever is waiting. `--no-upload` leaves the
YouTube upload out: the package simply waits in `02_READY_TO_POST` for you to check it on your
phone, and since such a run posts nothing the 48 h gate and the one-per-run cap are not applied
to it - every waiting item is rendered. Without `--no-upload` the Short is uploaded with
`youtube.default_privacy` from `config.yaml`, which is `public`: a run that passes the gate
publishes by itself, and `--ignore-schedule` is the only way to post inside the 48 h window.

## Hand-off mode: Apps Script + GitHub Actions

No always-on PC. `gas/Code_drive_watch.gs` is pasted into the Apps Script project that already
runs the message-board relay; its 1-minute trigger watches `01_RAW_VIDEO` and posts a
`repository_dispatch` (`raw_video_uploaded`) to this repository. `.github/workflows/auto-edit.yml`
then runs the same `--drive-once` pipeline on a free `ubuntu-latest` runner (the posting
interval and the one-Short-per-run cap come from `config.yaml`, so nothing has to be passed on
the command line):

```
phone → 01_RAW_VIDEO (GO_ folder) → Apps Script (1 min) → repository_dispatch
      → Actions (FFmpeg/OpenCV/Gemini) → 02_READY_TO_POST + folder moved to 03_ARCHIVE
      → artifact kept on the run for 14 days
```

Apps Script script properties: `RAW_FOLDER_ID`, `GITHUB_REPO` (`owner/repo`) and `GITHUB_TOKEN`
(a fine-grained PAT for that one repository with *Contents: read and write* — GitHub only accepts
a `repository_dispatch` from a token that can write code — and no other permission).

Repository secrets: `GEMINI_API_KEY`, `DRIVE_RAW_FOLDER_ID`, `DRIVE_READY_FOLDER_ID`,
`DRIVE_ARCHIVE_FOLDER_ID`, `YOUTUBE_TOKEN_JSON`, plus one Drive credential:

- `YOUTUBE_TOKEN_JSON` – the content of `token.json` pasted as one JSON string. The runner has
  no `token.json`, so this is how the interval check reads the channel and how the upload
  authenticates; an expired token is refreshed from the `refresh_token` inside it.
- `GDRIVE_TOKEN_JSON` – the content of `drive_token.json`; long-lived, lets a scheduled or manual
  run work on its own.
- or nothing at all: each dispatch carries `GDRIVE_ACCESS_TOKEN`, a Drive access token minted by
  `ScriptApp.getOAuthToken()` that lives for about an hour. The workflow masks it before use.

`python scripts/verify_drive_access.py` checks that credential and prints how many files are
waiting, without printing the credential itself.

A double dispatch is harmless: `01_RAW_VIDEO` is scanned on every run and a `GO_` folder (or a
loose file) only leaves it once the package is uploaded, so a second run simply finds nothing to
render.

## Windows local mode (no Google Drive)

Drive's free 15 GB ran out, so the folder hub now also exists on the artist's own Windows PC:
three folders under one base folder, and not a single Google Drive call in the run.

```
C:\CrayonJapan\                  <- local.base_dir / CRAYONJAPAN_HOME / --local-base
    01_RAW_VIDEO\               <- a GO_<name> folder, or one loose recording
    02_READY_TO_POST\           <- {base_name}_{YYYYMMDD_HHMM}\ with the four artifacts
    03_ARCHIVE\                 <- the GO_ folder, once it has been rendered
    _WORK\                      temporary render files (emptied after each Short)
    logs\                       run_local.bat output + one summary per day
    local_state.json            what has already been rendered
```

The rules are the Drive hub's rules, unchanged: only a `GO_` folder (or a loose video) is
picked up, clips sort naturally with `finish` / `final` / `完成` last, the run publishes at
most one artwork and only when the newest upload on the channel is at least
`min_interval_hours` old, the package is named `{base_name}_{YYYYMMDD_HHMM}`, and an item is
marked done only after its package exists *and* the source has moved to `03_ARCHIVE`.
`local_hub.py` reuses `drive_sync.py`'s naming and ordering functions, so the two hubs cannot
drift apart.

| command | what it does |
| --- | --- |
| `python main.py --local-init` | create the three folders (+ `_WORK`, `logs`) and print them |
| `python main.py --local-once` | one posting slot: gate → render one artwork → publish → archive |
| `python main.py --local-watch` | scan every `local.scan_interval_seconds` (no gate: for testing) |
| `python main.py --dry-run` | the layout, the queue and the gate decision; creates, renders and posts nothing |
| `python main.py --doctor` | Python, FFmpeg (+ `ass`, `subtitles`, `libx264`), the font, the base folder, free space, `client_secret.json`, `token.json`, the Gemini key, the queue, the posting rhythm |
| `python main.py --auth` | the one interactive step: browser sign-in, `token.json` saved |

Windows entry points:

| file | what it does |
| --- | --- |
| `setup_local.bat` | venv, `pip install -r requirements.txt`, `.env` from `.env.example`, folder layout, `--doctor` |
| `run_local.bat` | one slot (`--local-once`) with every argument passed through (`--dry-run`, `--auth`, `--no-upload`, `--local-watch`), logging to `<base>\logs\run_local.log` |
| `scripts/register_local_task.bat 22:00` | Task Scheduler entry, daily; `/delete` removes it |

A clip is not copied into `_WORK`: it is hard-linked (NTFS), so an hour-long recording costs
no extra disk, and the pipeline deleting its work copy never touches the original. On a file
system that refuses a link (another volume, FAT32) it falls back to a real copy.

The learning history (`analytics/performance.csv`, `analytics/improvement_state.json`) simply
stays on the PC in local mode - there is no Drive copy to restore or push back, so
`learning_prepare`/`learning_finish` skip the restore and the backup and log that they did.

Use **one** hub per channel: a Drive run and a local run would each judge the 48 h interval
from the same channel and could post twice. To retire the Drive path, remove the `schedule`
cron from `.github/workflows/auto-edit.yml` and delete the Apps Script trigger.

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

## Learning loop (PDCA)

Every run measures, reflects, re-renders and remembers. The order matters and is the same one
the Actions job runs:

```
restore (Drive) -> collect (YouTube) -> reflect (Gemini) -> render/publish -> save (Drive)
```

| step | what happens | code |
| --- | --- | --- |
| Restore | `performance.csv` and `improvement_state.json` are pulled from Drive over the local copy. The repository's CSV is only the starting point when Drive holds none. | `pdca_state.py` |
| Collect | The Shorts at least 24 h old (plus any row never measured, at most 8, newest first) are refreshed: views, likes, comments and the uploaded length from the Data API, average view percentage **and average view duration** in one Analytics request. `updated_at` stops the same row being asked twice within 6 h. | `analytics_updater.py` |
| Reflect | The recent rows (retention, seconds watched vs length, hook style, language, comments) and the current parameters go to Gemini, which answers with a diagnosis, the one thing the next Short tests, and the parameters to change. Out-of-range numbers are clamped, unknown keys dropped. | `improvement.py` |
| Render | The chosen parameters drive the hook, the length, the tempo, the audio balance, the voice, the on-screen language and the closing question. | `main.py` |
| Save | The parameters, the reflection and one history entry per run go into `improvement_state.json`, and both state files are written back to Drive (replacing the previous copy). | `pdca_state.py` |

The parameters and their allowed ranges:

| parameter | default | range | what it changes |
| --- | --- | --- | --- |
| `target_duration_sec` | 28 | 20-32 | the finished Short |
| `hook_style` | `finish_flash_to_blank` | `finish_preview`, `text_only` | the opening |
| `hook_duration_sec` / `hook_flash_seconds` | 2.4 / 0.4 | 1.2-3.0 / 0-0.9 | flash + tail split |
| `hook_audio_boost_db` | 6 | 0-12 | how loud the drawing sound is in the opening |
| `finish_hold_seconds` | 2.5 | 1-5 | the slow look at the finished piece |
| `drawing_audio_volume` | 0.35 | 0-1 | drawing sound under the voice (0 = voice only) |
| `narration_ratio` | 0.62 | 0.3-0.95 | share of the Short the voice fills |
| `tempo` | 1.0 | 0.8-2.0 | body speed beyond the target |
| `language_mode` | `bilingual` | `ja`, `en` | on-screen language (and the voice) |
| `tts_mode` | `short` | `full`, `none` | how much (or whether) a voice is used |
| `cta_text` / `cta_text_ja` | Follow line | - | the closing line |
| `cta_question_en` / `cta_question_ja` | What should I draw next? / 次は何を描いてほしい？ | - | the comment question |

Without enough data (fewer than `improvement.min_rows` measured Shorts), when Gemini fails,
or when its answer cannot be parsed, the run ships the defaults in `config.yaml`
(`improvement.defaults`) and logs why - the render never depends on the reflection.
`--ignore-pdca` renders with those defaults and skips collection, for a comparable manual run.

The state files live in `DRIVE_STATE_FOLDER_ID` when it is set, otherwise in the archive folder
(`03_ARCHIVE`), which nothing else reads; the workflow needs no change for it.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests cover style selection, CSV handling, FFmpeg command assembly, `atempo` chaining,
the audio-mixing branch, JSON parsing of Gemini replies, the hook/sensory/loop prompt rules,
upload body limits, finished-artwork
extraction (OpenCV path and FFmpeg fallback), scanned-artwork resizing, the per-clip project
plan and its FFmpeg commands, the metadata bundle (including the overseas caption block and its
no-Gemini fallback), the per-render package folder, the Drive
sync orchestration
(GO_ folder discovery, natural sorting, prefix stripping, download / upload / archive / state,
the 02_READY_TO_POST subfolder, mocked Drive client), the Windows local hub (folder resolution,
layout, queue, natural order, hard-link hand-off, publish, archive, state, cap, dry run),
`--doctor` and project file invariants. They need `pyyaml` and `opencv-python`
only – no FFmpeg run, API key or network access (one audio-mux path needs `ffmpeg` on the
PATH, which the workflow installs).

For a real end-to-end check with FFmpeg (synthetic clips, different resolutions and audio,
plus a scanned PNG), run:

```bash
python3 scripts/verify_project_pipeline.py --ffmpeg "$(command -v ffmpeg)"
```

The local hub has its own end-to-end check: it builds a throw-away base folder, renders one
synthetic `GO_` artwork through `local_hub.sync_once` with the real FFmpeg (canned script,
`tts_mode: none`, no upload), and verifies the package, the 1080x1920 length, the archive move
and the state file. No key, no network, no quota, and nothing outside the temporary folder:

```bash
python3 scripts/verify_local_mode.py --ffmpeg "$(command -v ffmpeg)"
```

To look at the current layout with your own eyes, render one sample from a local recording.
FFmpeg and edge-tts are the real ones; Gemini and YouTube are replaced by a canned script and a
fixed parameter set, so nothing is uploaded and no quota is spent:

```bash
python3 -m scripts.render_preview --input preview/sample.mp4 --language bilingual
python3 -m scripts.render_preview --input preview/sample.mp4 --language ja --hook-style text_only
```

The frames worth checking are 0.4 s (the flash), 1.5 s (the hook), the middle of the body, and
the last 2 s (the question and the follow line):

```bash
ffmpeg -ss 0.4 -i output_preview/*/*_final.mp4 -frames:v 1 -update 1 frame.png
```

## Repository layout

```
config.yaml              brand, video, improvement (PDCA), local mode and upload settings
main.py                  pipeline entry point, local watcher, Drive mode, local mode (--local-*)
local_hub.py             Windows local hub: 01_RAW_VIDEO / 02_READY_TO_POST / 03_ARCHIVE, no Drive
doctor.py                --doctor: FFmpeg, font, folders, credentials, queue, posting rhythm
improvement.py           parameter defaults / validation, the reflection prompt and the state
pdca_state.py            performance.csv + improvement_state.json in Drive
video_processor.py       9:16 time-lapse, opening flash/hook, ASMR audio retention, artwork still
ai_writer.py             style selection, the Gemini script and the offline template script
metadata_writer.py       upload pack (.txt) + art metadata (.json), incl. the overseas SNS captions
drive_sync.py            Google Drive hub: list / download / upload / archive / state files
setup_local.bat          one-time Windows local setup (venv, packages, .env, folders, doctor)
run_local.bat            one local slot, logging to <base>\logs\run_local.log
scripts/register_local_task.bat  Task Scheduler entry for the daily local slot
scripts/verify_drive_access.py   CI pre-flight check for the Drive credential
scripts/render_preview.py        local sample render for the visual check (no Gemini, no upload)
scripts/verify_local_mode.py     end-to-end check of the local hub (real FFmpeg, no key)
docs/SETUP_WINDOWS_LOCAL.md      Japanese walk-through of the Windows local mode
assets/fonts/            bundled Noto Sans CJK JP (JIS X 0208 subset) + its OFL licence
.github/workflows/auto-edit.yml  Actions job: dispatch → render → Drive
.github/workflows/tests.yml      unit tests on every push and pull request
gas/Code_drive_watch.gs  Apps Script half: 1-minute folder watch → repository_dispatch
youtube_uploader.py      OAuth, Data API upload, Analytics API client
analytics_updater.py     performance.csv refresh (views, length, retention, seconds watched)
analytics/performance.csv one row per uploaded Short, with the parameters it was made with
analytics/improvement_state.json  last parameters, reflection and history (git-ignored)
tests/                   unittest suite
```

## Security

- `.env`, `client_secret.json`, `token.json` and `drive_token.json` hold credentials and are
  git-ignored. Never commit them; if one leaks, revoke it in Google Cloud Console / AI Studio
  and re-issue.
- `input/` and `output/` (raw footage and renders) are git-ignored as well, and so is
  `drive_state.json` (it lists your personal Drive file IDs) and `local_state.json` (it lists
  the folder paths of the artist's own PC).
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

- The YouTube Analytics API lags: a fresh upload can answer with no row for a day or more,
  which is why a row is only collected once it is 24 h old and why a row with no numbers yet is
  retried on later runs. The Shorts-specific "viewed vs swiped away" figure is not exposed by
  the API; the loop reads `averageViewPercentage` and `averageViewDuration` instead.
- The Analytics scope (`yt-analytics.readonly`) only works when the **YouTube Analytics API** is
  enabled in the same Google Cloud project as the OAuth client, and for the channel the token
  belongs to (`channel==MINE`).
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
- `default_privacy` is `public` in `config.yaml`, so a run that passes the 48 h gate publishes
  the Short immediately; set it back to `unlisted` if you want to look at it first. Nothing is
  published by a run that is inside the interval, and `--ignore-schedule` is the only way to
  post early.
- Local mode: a clip is hard-linked into `_WORK`, so deleting the work copy never touches the
  recording. A run that fails before publishing leaves the `GO_` folder in `01_RAW_VIDEO` and
  renders it again next slot; a run that publishes but cannot archive it is not marked done, so
  the same artwork is rendered once more rather than silently lost. The `command` window the
  Task Scheduler opens closes when the run ends; the log stays in `<base>\logs\`.
- Local mode keeps the learning history in `analytics/` on that PC. It is not committed back to
  the repository: copy the two files to another machine if the channel moves again.
