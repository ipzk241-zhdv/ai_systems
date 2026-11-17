"""Bounded document extraction in a short-lived subprocess; never executes files."""
import json
from pathlib import Path
import sys
from xml.etree import ElementTree
from zipfile import ZipFile


MAX_TEXT = 100000


def extract(path, suffix):
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        with Path(path).open("r", encoding="utf-8-sig") as source:
            return source.read(MAX_TEXT)
    if suffix == ".docx":
        with ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 4 * 1024 * 1024:
                raise ValueError("DOCX document too large")
            root = ElementTree.fromstring(archive.read(info))
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        return "\n".join("".join(node.itertext()) for node in root.iter(namespace + "p"))[:MAX_TEXT]
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        parts, count = [], 0
        for page in reader.pages:
            text = (page.extract_text() or "")[:MAX_TEXT - count]
            parts.append(text)
            count += len(text)
            if count >= MAX_TEXT:
                break
        return "\n".join(parts)
    raise ValueError("Unsupported document format")


if __name__ == "__main__":
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        except ImportError:
            pass
        print(json.dumps({"text": extract(sys.argv[1], sys.argv[2])}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "Не вдалося прочитати документ. Для PDF потрібен pypdf; скани без тексту надішли як картинки."}, ensure_ascii=False))
        raise SystemExit(1)
