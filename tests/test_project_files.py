import os
import py_compile
import re
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRIVE_ENV_VARS = ["DRIVE_RAW_FOLDER_ID", "DRIVE_READY_FOLDER_ID", "DRIVE_ARCHIVE_FOLDER_ID"]
DRIVE_FOLDER_URL = re.compile(r"drive\.google\.com/drive/folders/[A-Za-z0-9_-]{10,}")
EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SCANNED_EXTENSIONS = (".py", ".md", ".yaml", ".yml", ".txt", ".example")

CSV_HEADER = (
    "video_id,date,title,style_used,hook_type,views,"
    "avg_view_percentage,likes,comments_count,top_feedback"
)

GITIGNORE_ENTRIES = [
    ".env",
    "client_secret.json",
    "token.json",
    "drive_token.json",
    "drive_state.json",
    "input/",
    "output/",
    "*.mp4",
    "*.mov",
    "*.avi",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
]

REQUIRED_PACKAGES = [
    "opencv-python",
    "ffmpeg-python",
    "google-genai",
    "edge-tts",
    "google-api-python-client",
    "google-auth-oauthlib",
    "pyyaml",
    "watchdog",
    "python-dotenv",
]


def read(path, *parts):
    with open(os.path.join(ROOT, path, *parts), "r", encoding="utf-8") as f:
        return f.read()


class ProjectFilesTests(unittest.TestCase):
    def test_performance_csv_header(self):
        first_line = read("analytics", "performance.csv").splitlines()[0]
        self.assertEqual(first_line, CSV_HEADER)

    def test_gitignore_covers_every_secret_and_media_entry(self):
        lines = [line.strip() for line in read(".gitignore").splitlines()]
        for entry in GITIGNORE_ENTRIES:
            self.assertIn(entry, lines)

    def test_requirements_list_core_dependencies(self):
        requirements = read("requirements.txt")
        for package in REQUIRED_PACKAGES:
            self.assertIn(package, requirements)
        self.assertIn("Pillow", requirements)

    def test_no_credential_files_in_repository(self):
        forbidden = {".env", "client_secret.json", "token.json", "drive_token.json"}
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
            for name in filenames:
                if name in forbidden:
                    found.append(os.path.join(dirpath, name))
        self.assertEqual(found, [])

    def test_env_example_documents_the_drive_folders(self):
        example = read(".env.example")
        for name in DRIVE_ENV_VARS:
            self.assertIn(name, example)
        self.assertIn("GEMINI_API_KEY", example)
        self.assertNotIn("AIza", example)

    def test_readme_documents_drive_mode(self):
        readme = read("README.md")
        for name in DRIVE_ENV_VARS:
            self.assertIn(name, readme)
        self.assertIn("--drive-once", readme)
        self.assertIn("--drive-watch", readme)
        self.assertIn("01_RAW_VIDEO", readme)
        self.assertIn("02_READY_TO_POST", readme)
        self.assertIn("03_ARCHIVE", readme)

    def test_no_personal_drive_ids_or_addresses_in_sources(self):
        """The repository is public: folder IDs belong in .env only."""
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
            for name in filenames:
                if not name.lower().endswith(SCANNED_EXTENSIONS):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                if DRIVE_FOLDER_URL.search(text) or EMAIL_ADDRESS.search(text):
                    offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])

    def test_no_media_or_renders_tracked_in_tree(self):
        forbidden_ext = (".mp4", ".mov", ".avi")
        offenders = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT)):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
            for name in filenames:
                if name.lower().endswith(forbidden_ext):
                    offenders.append(os.path.join(dirpath, name))
        self.assertEqual(offenders, [])

    def test_all_python_files_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            for dirpath, dirnames, filenames in os.walk(ROOT):
                dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
                for name in filenames:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    cfile = os.path.join(tmp, name + ".pyc")
                    py_compile.compile(path, cfile=cfile, doraise=True)


if __name__ == "__main__":
    unittest.main()
