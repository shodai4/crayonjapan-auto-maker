# CrayonJapan Auto-Maker

Automated making-of video pipeline for **CrayonJapan** (<https://crayonjapan.base.shop/>).
Converts raw hand-drawn **Sakura Cray-Pas Specialist** process videos into 9:16 vertical
English YouTube Shorts, and adapts the narrative style based on how much of each Short
viewers actually watch.

## What it does

1. **Finish-preview hook** – the last few seconds of the drawing (the finished postcard) are
   cut at natural speed into the first 2.5 s.
2. **Vertical time-lapse** – the whole recording is sped up and cropped to 1080x1920 (9:16).
3. **Finished artwork still** – the completed illustration is pulled from the last frame of the
   raw recording (full frame, no 9:16 crop) and saved as a JPEG for the thumbnail and the shop
   listing.
4. **Gemini script** – four keyframes are sent to `gemini-3.8-flash`, which returns the title,
   on-screen hook text, English narration, description and tags as JSON.
5. **Voice + subtitles** – `edge-tts` (en-US-JennyNeural) narrates, and word-level subtitles
   are burned into the video.
6. **Upload + record** – the Short is uploaded (unlisted by default) and one row is appended
   to `analytics/performance.csv`. A plain-text upload pack and an `art_metadata.json` are
   written next to the video.
7. **Learning loop** – `analytics_updater.py` refreshes views / likes / comments from the
   YouTube Data API and the average view percentage from the YouTube Analytics API.
   `choose_best_style()` then reuses the best-performing style, with a 25 % exploration rate
   so the other styles keep getting a chance.
8. **Google Drive hub (phase 2)** – `drive_sync.py` watches a Drive folder, downloads new
   recordings, renders them and drops the finished package into a second folder, then moves
   the source into an archive folder. See *Google Drive hub* below.

## Requirements

- Python 3.10+
- FFmpeg 5+ with `libx264`, the `subtitles` (libass) filter, and `ffprobe` on `PATH`
- A Gemini API key (Google AI Studio)
- A Google Cloud OAuth client (type: *Desktop app*) with **YouTube Data API v3** and
  **YouTube Analytics API** enabled for the channel the videos are uploaded to (`@CrayonJapan`)

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

Output files land in `./output`:

| file | what it is |
| --- | --- |
| `*_vertical.mp4` | 9:16 time-lapse (intermediate) |
| `*.mp3`, `*.srt` | narration and word-level subtitles (intermediate) |
| `*_final.mp4` | the upload-ready Short |
| `*_artwork.jpg` | the finished drawing, full frame, for thumbnail / shop listing |
| `*_metadata.txt` | paste-ready title, hook text, description, tags |
| `*_art_metadata.json` | the same artwork data as structured metadata for the shop |

## Google Drive hub

Folder layout (create it once in your own Drive; nothing personal is stored in this repo):

```
AI秘書 / 12_動画自動作成/
    01_RAW_VIDEO/      drop the raw drawing recording here
    02_READY_TO_POST/  finished MP4 + artwork JPEG + English metadata appear here
    03_ARCHIVE/        the raw recording, moved here once it has been rendered
```

Copy each folder ID from its URL (`https://drive.google.com/drive/folders/<ID>`) into `.env` as
`DRIVE_RAW_FOLDER_ID`, `DRIVE_READY_FOLDER_ID` and `DRIVE_ARCHIVE_FOLDER_ID`.

Each `--drive-once` / `--drive-watch` cycle:

1. lists `01_RAW_VIDEO` for `.mp4` / `.mov` / `.avi` files that are not already processed;
2. skips files modified in the last 60 s (`--drive-min-age`), so a still-uploading video is
   never downloaded half-finished;
3. downloads the recording into `input/` and runs the full pipeline;
4. uploads `*_final.mp4`, `*_artwork.jpg`, `*_metadata.txt` and `*_art_metadata.json` into
   `02_READY_TO_POST` with resumable uploads;
5. moves the raw recording to `03_ARCHIVE` and records the file ID in `drive_state.json`,
   so an interrupted run repeats at most one video instead of re-rendering everything.

`--drive-interval` (or `DRIVE_WATCH_INTERVAL_SECONDS`) sets the scan interval; the default is
60 s. `--no-upload` leaves the YouTube upload to you: the package simply waits in
`02_READY_TO_POST` until you check it on your phone. Without it the Short is uploaded unlisted
(`config.yaml`), so nothing goes public by itself.

## Narrative styles

| style | intent |
| --- | --- |
| `style_A_material` | texture, pigment and colour layering of the oil pastels |
| `style_B_story` | warm story about the everyday moment behind the motif |
| `style_C_asmr` | minimal narration; the drawing sound itself carries the video |

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
the audio-mixing branch, JSON parsing of Gemini replies, upload body limits, finished-artwork
extraction (OpenCV path and FFmpeg fallback), the metadata bundle, the Drive sync orchestration
(list / download / upload / archive / state, mocked Drive client) and project file invariants.
They need `pandas`, `pyyaml` and `opencv-python` only – no FFmpeg run, API key or network
access.

## Repository layout

```
config.yaml              brand, video, style-evolution and upload settings
main.py                  pipeline entry point, local watcher and Drive modes
video_processor.py       9:16 time-lapse, hook cut, ASMR audio retention, artwork still
ai_writer.py             style selection + Gemini script generation
metadata_writer.py       English upload pack (.txt) + art metadata (.json)
drive_sync.py            Google Drive hub: list / download / upload / archive
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

## Notes and limitations

- Videos are processed one at a time; the watcher keeps running if a single video fails.
- Drive mode needs a video that is fully uploaded. Files touched in the last 60 s are skipped
  until a later scan, so the upload can finish first.
- A failed render or upload leaves the raw video in `01_RAW_VIDEO`; the next scan tries it again.
  Only successes go to `03_ARCHIVE`.
- The finished artwork is the last frame of the recording. If your hand still covers part of the
  paper at the end, pass `--artwork-offset 3` to grab a frame three seconds earlier.
- The mix uses `amix=...:normalize=0`, which needs FFmpeg 4.4 or newer.
- A freshly uploaded Short has no Analytics rows for a while; until then its
  `avg_view_percentage` stays at its previous (or 0) value and the update is only logged.
- `default_privacy` is `unlisted` in `config.yaml`; set it to `public` when you are ready for
  the Short to go live immediately.
