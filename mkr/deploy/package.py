"""Run: python deploy/package.py. Package code only, without keys or chat history."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def package():
    root = Path(__file__).resolve().parents[1]
    output = root / "news-bot.zip"
    files = [root / "start_bot.py", root / "check_ai.py"]
    solution = root / "solution"
    files.extend(solution.glob("*.py"))
    files.extend(solution / name for name in ("requirements.txt", "importance.txt", "README.md", ".env.example", ".gitignore"))
    files.append(solution / "requirements-files.txt")
    files.append(solution / "news_examples.json")
    files.append(solution / "FILES.md")
    files.extend((solution / "file_web_assets").glob("*.*"))
    files.extend((root / "deploy").glob("*.md"))
    files.append(root / "deploy" / "news-bot.service")
    files.append(root / "deploy" / "direct_files_https.py")
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(root).as_posix())
    print("Created: " + str(output))
    return output


if __name__ == "__main__":
    package()
