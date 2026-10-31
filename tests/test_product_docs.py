"""Validate product and acceptance documentation without dependencies."""

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_ROOTS = [
    REPO_ROOT / "docs",
    REPO_ROOT / "tests" / "fixtures",
    REPO_ROOT / "tests" / "acceptance",
]


class ProductDocsTests(unittest.TestCase):
    def setUp(self):
        self.documents = sorted(
            document for root in DOCUMENT_ROOTS for document in root.rglob("*.md")
        )
        self.assertTrue(self.documents, "No documentation found")

    def test_documents_are_utf8_text_with_final_newline(self):
        for document in self.documents:
            with self.subTest(document=document.name):
                raw = document.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
                content = raw.decode("utf-8")
                self.assertTrue(content.strip(), "Document is empty")
                self.assertNotIn("\x00", content)
                self.assertTrue(content.endswith("\n"))
                self.assertFalse(
                    any(line.rstrip() != line for line in content.splitlines()),
                    "Document contains trailing whitespace",
                )

    def test_fenced_code_blocks_are_closed(self):
        for document in self.documents:
            with self.subTest(document=document.name):
                opening = None
                for line_number, line in enumerate(
                    document.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if opening is None:
                        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
                        if match:
                            opening = (match[1][0], len(match[1]), line_number)
                    else:
                        character, length, _ = opening
                        closing = rf"^ {{0,3}}{re.escape(character)}{{{length},}}\s*$"
                        if re.fullmatch(closing, line):
                            opening = None
                self.assertIsNone(opening, f"Unclosed code fence: {opening}")

    def test_relative_links_resolve_inside_repository(self):
        for document in self.documents:
            content = document.read_text(encoding="utf-8")
            for link in re.findall(r"!?\[[^\]\n]*\]\(([^\s)]+)\)", content):
                with self.subTest(document=document.name, link=link):
                    parsed = urlsplit(link)
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        continue
                    target = (document.parent / unquote(parsed.path)).resolve()
                    self.assertTrue(
                        target.is_relative_to(REPO_ROOT),
                        f"Local link escapes repository: {link}",
                    )
                    self.assertTrue(target.exists(), f"Broken local link: {link}")


if __name__ == "__main__":
    unittest.main()
