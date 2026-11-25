# Windows PC ローカルモード はじめかた

このページは、**自分の Windows PC の中だけで** 動画の自動編集から YouTube 投稿までを回すための手順書です。
Google ドライブも GitHub Actions も使いません。PC のフォルダに素材を置いておけば、あとは自動で 1 本ずつ仕上がって投稿されます。

> むずかしい設定は `setup_local.bat` が全部やります。このページは「何がどこにあるか」と「詰まったときの直し方」だけ書いています。

---

## 1. しくみ（3つのフォルダ）

`C:\CrayonJapan`（変更できます）の中に、この 3 つのフォルダができます。

```
C:\CrayonJapan\
    01_RAW_VIDEO\              ここに素材を置く（GO_名前 のフォルダ、または動画1本）
        GO_20261121_sushi\       1作品ぶん：part1.mp4, part2.mp4, finish.mp4, scan.jpg
        GO_20261122_cat\         次の作品（順番待ち）
    02_READY_TO_POST\          完成した Short（1本 = 1フォルダ）
        20261121_sushi_20261121_1830\
            20261121_sushi_final.mp4        投稿された動画
            20261121_sushi_artwork.jpg      完成画（サムネ・販売用）
            20261121_sushi_metadata.txt     タイトル・説明・タグ・TikTok/Instagram/Pinterest 用キャプション
            20261121_sushi_art_metadata.json
    03_ARCHIVE\                投稿が終わった GO_ フォルダの移動先
    _WORK\                     作業用（消しても大丈夫）
    logs\                      実行ログ
    local_state.json           どこまで処理したかの記録
```

流れは **`01_RAW_VIDEO` → 編集 → `02_READY_TO_POST` → YouTube 投稿 → `03_ARCHIVE`** です。
投稿の間隔（**1回に1本・48時間あき**）は今までと同じで、設定は `config.yaml` の `schedule` にあります。
48時間たっていない日は、フォルダを見に行くだけで何も作りません（素材はそのまま残ります）。

---

## 2. 用意するもの

| もの | 説明 |
| --- | --- |
| Windows 10 / 11 の PC | 電源が入っていて、サインインしている必要があります（スリープは解除しておくと安心） |
| Python 3.10 以上 | <https://www.python.org/downloads/> から。インストール画面の **「Add python.exe to PATH」にチェック** |
| FFmpeg（5 以上） | コマンドプロンプトで `winget install Gyan.FFmpeg` を実行するのが一番簡単です。入れたあと、**ウィンドウを開き直す**と認識されます |
| YouTube チャンネルの Google アカウント | 投稿したいチャンネル（`@CrayonJapan`）の持ち主でサインインします |
| このリポジトリ | `git clone`、または GitHub の「Code → Download ZIP」を解凍して、`C:\crayonjapan-auto-maker` などに置きます |
| （任意）Gemini API キー | 台本を AI に書かせたい場合だけ。**なくても動きます**（下の 8 章） |

---

## 3. セットアップ（最初の 1 回だけ）

### 3-1. `setup_local.bat` をダブルクリック

順番に次をやります。黒い画面が開いて英語で進みます（日本語だと文字化けする PC があるため、バッチの表示は英語にしています）。

1. Python を見つける
2. `.venv`（Python の作業部屋）を作る
3. 必要なライブラリを入れる（初回は数分）
4. `.env` を `.env.example` から作る
5. `01_RAW_VIDEO` などのフォルダを作り、`--doctor` で PC を点検する

最後に `[OK]` / `[WARN]` / `[FAIL]` の一覧が出ます。`[FAIL]` が残っていると投稿まで行けないので、4 章の対処をしてから、もう一度 `setup_local.bat`（または `run_local.bat --doctor`）を実行してください。`[WARN]` は「このままでも動くけれど知っておいて」の意味です。

### 3-2. `client_secret.json` を置く（YouTube の入場券）

1. <https://console.cloud.google.com/> を開き、チャンネルと同じアカウントでプロジェクトを作る（名前は何でもOK）
2. **API とサービス → ライブラリ** で次の 2 つを「有効にする」
   - **YouTube Data API v3**
   - **YouTube Analytics API**
3. **API とサービス → OAuth 同意画面** を一度だけ設定（外部 / アプリ名は `CrayonJapan` など / 自分をテストユーザーに追加）
4. **API とサービス → 認証情報 → 認証情報を作成 → OAuth クライアント ID**、種類は **「デスクトップ アプリ」**
5. 表示された JSON をダウンロードして、リポジトリのフォルダ（`setup_local.bat` と同じ場所）に **`client_secret.json`** という名前で保存

> `client_secret.json` と `token.json` は絶対に GitHub に上げないでください。`.gitignore` で除外済みです。

### 3-3. 初回のサインイン

```
run_local.bat --auth
```

ブラウザが開くので、**チャンネルの持ち主のアカウント**を選び、「許可」を押します。
`Signed in as: チャンネル名` と出れば成功です。`token.json` が保存され、次からは無人のまま投稿できます。

### 3-4. 点検

```
run_local.bat --doctor
```

`[FAIL]` が 0 なら準備完了です。

---

## 4. 毎日の使い方

1. 撮った動画を 1 作品ぶん、`01_RAW_VIDEO\` の中にフォルダを作って入れる
   - 途中まで入れるときは **`GO_` を付けない**（`20261121_sushi` など）。`GO_` が付いていないフォルダは無視されます
   - クリップは何本でもOK。`part2` は `part10` より前に、`finish` / `final` / `完成` は必ず最後に並びます
   - 完成画のスキャン（`.jpg` / `.png`）を入れると、サムネと Gemini の参考画像に使われます（無い場合は最後のクリップの最後のコマ）
2. 入れ終わったらフォルダ名の頭に **`GO_`** を付ける（`GO_20261121_sushi`）。これが「この作品は完成、編集してOK」の合図です
3. `run_local.bat` をダブルクリック（または放っておけば 5 章の自動実行）

---

## 5. 自動で回す（タスクスケジューラ）

```
scripts\register_local_task.bat 22:00
```

毎日 22:00 に `run_local.bat` が動くようになります（GitHub Actions のときと同じ 22:00 JST）。
48時間あいていない日は何も投稿しません。時間を変えるときは `register_local_task.bat 07:30` のように引数を付けて実行し直してください。

- いますぐ試す：`schtasks /Run /TN "CrayonJapan Auto-Maker"`
- やめる：`scripts\register_local_task.bat 22:00 /delete`

> PC の電源が入っていて、サインインしている時間に設定してください。管理者権限は不要です。
> スリープ中に起動させたい場合は「タスク スケジューラ → プロパティ → 条件 → このタスクを実行するためにスリープを解除する」にチェックを入れてください。

---

## 6. コマンド一覧

| コマンド | すること |
| --- | --- |
| `run_local.bat` | 投稿スロットを 1 回実行（48時間ゲート → 1本だけ生成 → 投稿 → アーカイブ） |
| `run_local.bat --dry-run` | **何も作らず・投稿せず**、待っている素材・ゲートの状態・何本処理するかだけ表示 |
| `run_local.bat --doctor` | FFmpeg・Python・フォルダ・認証の点検 |
| `run_local.bat --auth` | YouTube の初回サインイン（ブラウザ） |
| `run_local.bat --no-upload` | 生成だけして投稿しない（`02_READY_TO_POST` に確認用として置く） |
| `run_local.bat --local-watch` | 60秒ごとにフォルダを見続ける（テスト用。48時間ゲートは効きません） |
| `run_local.bat --ignore-schedule` | 48時間以内でも投稿する（動作確認用。本番では使わないでください） |
| `run_local.bat --ignore-pdca` | 学習（数値収集と振り返り）を飛ばして既定パラメータで作る |

フォルダの場所を変えたいときは `--local-base "D:\CrayonJapan"`、`.env` の `CRAYONJAPAN_HOME`、`config.yaml` の `local.base_dir` のどれかで指定できます（左が優先）。

---

## 7. フォルダの中身を消さないでいいの？

- `03_ARCHIVE` は**投稿が終わった素材の置き場所**です。容量が気になったら古いものから消してOK（`02_READY_TO_POST` に完成品が残っています）
- `_WORK` は作業用です。残っていても、次に実行したときに作り直されます
- 素材を `01_RAW_VIDEO` に残しておきたい場合は `config.yaml` の `local.archive_after_render` を `false` に。`local_state.json` が「処理済み」を覚えているので二重には処理しません

---

## 8. Gemini の課金を止めても動きます

`GEMINI_API_KEY` が空・期限切れ・403/429（上限）・停止中でも、**動画は最後まで作られて投稿されます**。
その場合は台本だけが「テンプレート台本」に切り替わります。

- タイトル・フック・英語ナレーション・日本語訳・説明文・タグ・TikTok/Instagram/Pinterest 用キャプションは、`config.yaml`（画材名・シリーズ名・ハッシュタグ）と**作品フォルダ名**から組み立てます（例：`GO_20261121_sushi` → motif は `Sushi`）
- ナレーションの長さは、その回の目標秒数に合わせた語数レンジに収まるように作ります
- 学習（PDCA）の振り返りも Gemini を使いますが、失敗したときは `config.yaml` の既定パラメータで作られます

ログに次のように出ていたら、テンプレート台本で動いたという意味です。

```
no script from Gemini (...); using the offline template script built from config.yaml and the artwork name
```

AI に書かせたいときだけ、`.env` に `GEMINI_API_KEY=...` を入れてください。

---

## 9. うまくいかないとき

| 症状 | 原因と対処 |
| --- | --- |
| `[FAIL] FFmpeg ffmpeg is not on PATH` | FFmpeg 未インストール、または PATH 未反映。`winget install Gyan.FFmpeg` → **コマンドプロンプトとエクスプローラーを閉じて開き直す** |
| `[FAIL] FFmpeg filters missing: ass` | libass の無い軽量ビルド。Gyan.FFmpeg（full）を入れてください |
| `[FAIL] OAuth client` | `client_secret.json` が無い／名前が違う。3-2 をやり直す |
| 投稿で `invalid_grant` / `401` | `token.json` が失効（パスワード変更・長期間未使用など）。`run_local.bat --auth` で入り直す |
| `403 quotaExceeded`（YouTube） | その日の API 上限。翌日（太平洋時間の日付が変わるころ）に自動で回復します |
| `[schedule] ... 48h ... nothing is rendered` | 正常です。前回投稿から48時間たっていません。`--dry-run` で残り時間を確認できます |
| 何も起きない／`nothing is waiting` | フォルダ名に `GO_` が付いているか、動画が入っているか、`--local-base` の場所が合っているかを確認 |
| 生成が途中で止まる | `_WORK` か `02_READY_TO_POST` のドライブの空き容量を確認（長い素材は 6GB 以上あける） |
| 画面がすぐ閉じる | エクスプローラーから `run_local.bat` をダブルクリックすると最後に一時停止します。ログは `<base>\logs\run_local.log` |

ログの見方：`logs\run_local.log` に実行の全出力、`logs\run_YYYY-MM-DD.log` にその日の結果一覧が残ります。

---

## 10. Google ドライブ運用（GitHub Actions）との関係

- これまでの `--drive-once` / `--drive-watch`（ドライブ基準）と、`gas/` + `.github/workflows/auto-edit.yml`（CI 投稿）は**そのまま残しています**。壊れていません
- ローカルモードとドライブモードは**どちらか一方だけ**を使ってください。両方を同じチャンネルに向けると、48時間ゲートを別々に判定して 2 本続けて投稿する可能性があります
- ドライブ基準をやめる場合は、GitHub Actions の `schedule` を止める（`.github/workflows/auto-edit.yml` の cron を消す）か、Apps Script のトリガーを削除してください

---

## 11. 中身の話（開発者向け）

| ファイル | 役割 |
| --- | --- |
| `local_hub.py` | ローカル 3 フォルダの読み取り・公開・アーカイブ・処理済み記録（ドライブ版 `drive_sync.py` と同じルール） |
| `doctor.py` | `--doctor` の点検項目 |
| `main.py` | `--local-once` / `--local-watch` / `--local-init` / `--dry-run` / `--auth` / `--doctor` |
| `setup_local.bat` / `run_local.bat` / `scripts/register_local_task.bat` | 上の 3 つの入口 |
| `tests/test_local_hub.py` / `tests/test_doctor.py` / `tests/test_local_mode.py` | ローカルモードの自動テスト |
| `tests/test_offline_script.py` | Gemini 無しの台本（テンプレート）のテスト |
| `scripts/verify_local_mode.py` | FFmpeg だけで最後まで通す検証（鍵もネットも不要） |

クリップは `_WORK` に**ハードリンク**で渡されます（同じディスクならコピーしない＝容量も時間もかからない）。リンクできない場所（別ドライブ・FAT32）では自動でコピーに切り替わります。どちらでも元ファイルは消えません。

---

## 12. 安全のための約束

- `.env` / `client_secret.json` / `token.json` / `local_state.json` は `.gitignore` 済み。**GitHub に上げない**
- リポジトリは Public です。フォルダ ID・メールアドレス・トークンはコードや README に書かない（今までと同じ方針）
- 投稿は「48時間あき・1本だけ」。`--ignore-schedule` は確認用で、通常運用では使いません
