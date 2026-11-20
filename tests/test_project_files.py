import os
import py_compile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSV_HEADER = (
    "video_id,date,title,style_used,hook_type,views,"
    "avg_view_percentage,likes,comments_count,top_feedback"
)

GITIGNORE_ENTRIES = [
    ".env",
    "client_secret.json",
    "token.json",
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
    "pandas",
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
        forbidden = {".env", "client_secret.json", "token.json"}
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv"}]
            for name in filenames:
                if name in forbidden:
                    found.append(os.path.join(dirpath, name))
        self.assertEqual(found, [])

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
