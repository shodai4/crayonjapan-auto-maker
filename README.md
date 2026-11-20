# CrayonJapan Auto-Maker

Automated making-of video pipeline for **CrayonJapan** (<https://crayonjapan.base.shop/>).
Converts raw hand-drawn **Sakura Cray-Pas Specialist** process videos into 9:16 vertical
English YouTube Shorts, and adapts the narrative style based on how much of each Short
viewers actually watch.

## What it does

1. **Finish-preview hook** – the last few seconds of the drawing (the finished postcard) are
   cut at natural speed into the first 2.5 s.
2. **Vertical time-lapse** – the whole recording is sped up and cropped to 1080x1920 (9:16).
3. **Gemini script** – four keyframes are sent to `gemini-2.5-flash`, which returns the title,
   on-screen hook text, English narration, description and tags as JSON.
4. **Voice + subtitles** – `edge-tts` (en-US-JennyNeural) narrates, and word-level subtitles
   are burned into the video.
5. **Upload + record** – the Short is uploaded (unlisted by default) and one row is appended
   to `analytics/performance.csv`.
6. **Learning loop** – `analytics_updater.py` refreshes views / likes / comments from the
   YouTube Data API and the average view percentage from the YouTube Analytics API.
   `choose_best_style()` then reuses the best-performing style, with a 25 % exploration rate
   so the other styles keep getting a chance.

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

1. Create `.env` in the project root:

   ```dotenv
   GEMINI_API_KEY=your_api_key
   ```

2. Download the OAuth client JSON from Google Cloud Console and save it as
   `client_secret.json` in the project root.
3. Enable **YouTube Data API v3** and **YouTube Analytics API** for that Cloud project.
4. Run `python main.py`. On the first run a browser opens for the OAuth consent screen; the
   analytics read scope is requested together with the upload scope, and the resulting
   `token.json` is reused afterwards.

## Usage

```bash
python main.py                       # watch ./input and process every dropped .mp4/.mov
python main.py --input clip.mp4      # process one file and exit (handy for testing)
python analytics_updater.py          # refresh views / likes / comments / avg view %
```

Output files land in `./output` (`*_vertical.mp4`, `*.mp3`, `*.srt`, `*_final.mp4`,
`*_frames/`).

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
the audio-mixing branch, JSON parsing of Gemini replies, upload body limits and project file
invariants. They need `pandas` and `pyyaml` only – no FFmpeg, API key or network access.

## Repository layout

```
config.yaml              brand, video, style-evolution and upload settings
main.py                  pipeline entry point and file watcher
video_processor.py       9:16 time-lapse, hook cut, ASMR audio retention
ai_writer.py             style selection + Gemini script generation
youtube_uploader.py      OAuth, Data API upload, Analytics API client
analytics_updater.py     performance.csv refresh
analytics/performance.csv one row per uploaded Short
tests/                   unittest suite
```

## Security

- `.env`, `client_secret.json` and `token.json` hold credentials and are git-ignored.
  Never commit them; if one leaks, revoke it in Google Cloud Console / AI Studio and re-issue.
- `input/` and `output/` (raw footage and renders) are git-ignored as well.
- The OAuth token is stored locally only; the scopes requested are
  `youtube.upload`, `youtube.readonly` and `yt-analytics.readonly`.

## Notes and limitations

- Videos are processed one at a time; the watcher keeps running if a single video fails.
- The mix uses `amix=...:normalize=0`, which needs FFmpeg 4.4 or newer.
- A freshly uploaded Short has no Analytics rows for a while; until then its
  `avg_view_percentage` stays at its previous (or 0) value and the update is only logged.
- `default_privacy` is `unlisted` in `config.yaml`; set it to `public` when you are ready for
  the Short to go live immediately.
