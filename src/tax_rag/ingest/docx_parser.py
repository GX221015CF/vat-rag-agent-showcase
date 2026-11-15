from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
NS = {"w": W_NS, "w14": W14_NS}


@dataclass
class SourceBlock:
    block_index: int
    block_type: str
    paragraph_index: int | None
    text: str
    style_name: str | None
    style_id: str | None
    alignment: str | None
    left_indent: str | None
    first_line_indent: str | None
    xml_para_id: str | None
    role: str = "body"
    matched_rule: str | None = None
    number: int | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text.replace("\u3000", ""))


def title_hint_from_filename(path: Path) -> str:
    return re.sub(r"_\d{8}$", "", path.stem)


def chinese_number_to_int(value: str) -> int:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    current = 0
    section = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section = (section + current) * unit
                total += section
                section = 0
                current = 0
            else:
                if current == 0:
                    current = 1
                section += current * unit
                current = 0
        else:
            raise ValueError(f"Unsupported Chinese numeral: {value}")
    return total + section + current


def load_rules(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["compiled_patterns"] = {
        name: re.compile(pattern) for name, pattern in data["patterns"].items()
    }
    return data


def iter_body_blocks(document: Any) -> Iterable[Paragraph | Table]:
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document._body)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document._body)


def read_source_blocks(path: Path) -> tuple[list[SourceBlock], dict[str, Any]]:
    document = Document(path)
    blocks: list[SourceBlock] = []
    paragraph_index = -1
    for block_index, element in enumerate(iter_body_blocks(document)):
        if isinstance(element, Paragraph):
            paragraph_index += 1
            para_id = element._p.get(f"{{{W14_NS}}}paraId")
            blocks.append(
                SourceBlock(
                    block_index=block_index,
                    block_type="paragraph",
                    paragraph_index=paragraph_index,
                    text=element.text.strip(),
                    style_name=element.style.name if element.style else None,
                    style_id=element.style.style_id if element.style else None,
                    alignment=str(element.alignment) if element.alignment is not None else None,
                    left_indent=str(element.paragraph_format.left_indent)
                    if element.paragraph_format.left_indent is not None
                    else None,
                    first_line_indent=str(element.paragraph_format.first_line_indent)
                    if element.paragraph_format.first_line_indent is not None
                    else None,
                    xml_para_id=para_id,
                )
            )
        else:
            rows = []
            for row in element.rows:
                rows.append(" | ".join(cell.text.strip() for cell in row.cells))
            blocks.append(
                SourceBlock(
                    block_index=block_index,
                    block_type="table",
                    paragraph_index=None,
                    text="\n".join(rows).strip(),
                    style_name=None,
                    style_id=None,
                    alignment=None,
                    left_indent=None,
                    first_line_indent=None,
                    xml_para_id=None,
                    role="table",
                )
            )

    features = inspect_ooxml(path)
    features["python_docx_paragraph_count"] = len(document.paragraphs)
    features["python_docx_table_count"] = len(document.tables)
    features["body_block_count"] = len(blocks)
    features["nonempty_paragraph_count"] = sum(
        1 for block in blocks if block.block_type == "paragraph" and block.text
    )
    return blocks, features


def inspect_ooxml(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
        root = ET.fromstring(document_xml)
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError(f"DOCX has no w:body: {path}")
        body_types = [child.tag.rsplit("}", 1)[-1] for child in list(body)]
        unknown_body = [name for name in body_types if name not in {"p", "tbl", "sectPr"}]
        part_names = set(archive.namelist())
        header_footer_parts = sorted(
            name
            for name in part_names
            if re.match(r"word/(?:header|footer)\d+\.xml$", name)
        )
        header_footer_text = {}
        for name in header_footer_parts:
            part_root = ET.fromstring(archive.read(name))
            texts = [node.text or "" for node in part_root.findall(".//w:t", NS)]
            header_footer_text[name] = "".join(texts).strip()
        return {
            "body_element_types": dict(Counter(body_types)),
            "unhandled_body_elements": unknown_body,
            "xml_paragraph_count_all": len(root.findall(".//w:p", NS)),
            "xml_table_count_all": len(root.findall(".//w:tbl", NS)),
            "text_box_count": len(root.findall(".//w:txbxContent", NS)),
            "alt_chunk_count": len(root.findall(".//w:altChunk", NS)),
            "footnote_reference_count": len(root.findall(".//w:footnoteReference", NS)),
            "endnote_reference_count": len(root.findall(".//w:endnoteReference", NS)),
            "comment_reference_count": len(root.findall(".//w:commentReference", NS)),
            "field_instruction_count": len(root.findall(".//w:instrText", NS)),
            "header_footer_text": header_footer_text,
            "has_footnotes_part": "word/footnotes.xml" in part_names,
            "has_endnotes_part": "word/endnotes.xml" in part_names,
            "has_comments_part": "word/comments.xml" in part_names,
        }


def find_title_paragraphs(blocks: list[SourceBlock], title_hint: str) -> list[int]:
    candidates = [
        block
        for block in blocks
        if block.block_type == "paragraph" and block.text
    ][:10]
    target = normalize_for_match(title_hint)
    for start in range(min(3, len(candidates))):
        combined = ""
        indices: list[int] = []
        for block in candidates[start:7]:
            combined += normalize_for_match(block.text)
            if block.paragraph_index is not None:
                indices.append(block.paragraph_index)
            if combined == target:
                return indices
            if not target.startswith(combined):
                break
    raise ValueError(f"Could not reconstruct title [{title_hint}]")


def classify_blocks(
    blocks: list[SourceBlock], rules: dict[str, Any], title_hint: str
) -> dict[str, Any]:
    patterns = rules["compiled_patterns"]
    title_paragraphs = find_title_paragraphs(blocks, title_hint)
    title_set = set(title_paragraphs)

    for block in blocks:
        if block.block_type == "table":
            continue
        if not block.text:
            block.role = "empty"
            continue
        if block.paragraph_index in title_set:
            block.role = "title"
            block.matched_rule = "title_from_filename_hint"
            continue
        matched = False
        for name in (
            "toc_heading",
            "chapter",
            "section",
            "article",
            "legacy_section",
            "item_cn",
            "item_arabic",
            "date_metadata",
        ):
            match = patterns[name].match(block.text)
            if not match:
                continue
            block.role = name
            block.matched_rule = name
            number_text = match.groupdict().get("number")
            if number_text:
                block.number = (
                    int(number_text)
                    if number_text.isdigit()
                    else chinese_number_to_int(number_text)
                )
            matched = True
            break
        if not matched:
            block.role = "body"

    nonempty = [block for block in blocks if block.text]

    toc_headings = [block for block in nonempty if block.role == "toc_heading"]
    toc_entries: list[SourceBlock] = []
    if toc_headings:
        toc_heading = toc_headings[0]
        first_article = next((block for block in nonempty if block.role == "article"), None)
        chapters_before_article = [
            block
            for block in nonempty
            if block.role == "chapter"
            and first_article is not None
            and block.block_index < first_article.block_index
            and block.block_index > toc_heading.block_index
        ]
        if len(chapters_before_article) > 1:
            actual_first_chapter = chapters_before_article[-1]
            for block in chapters_before_article[:-1]:
                block.role = "toc_entry"
                block.matched_rule = "toc_duplicate_chapter"
                toc_entries.append(block)
            for block in nonempty:
                if (
                    block.block_index > toc_heading.block_index
                    and block.block_index < actual_first_chapter.block_index
                    and block.role == "body"
                ):
                    block.role = "toc_entry"
                    block.matched_rule = "toc_range"

    title_text = "".join(
        block.text
        for block in blocks
        if block.paragraph_index in title_set and block.text
    )
    return {
        "title": title_text,
        "title_paragraphs": title_paragraphs,
        "toc_entry_count": sum(1 for block in blocks if block.role == "toc_entry"),
    }


def choose_format_profile(blocks: list[SourceBlock]) -> str:
    roles = Counter(block.role for block in blocks if block.text)
    chapter_styles = {
        block.style_name for block in blocks if block.role == "chapter" and block.text
    }
    article_styles = {
        block.style_name for block in blocks if block.role == "article" and block.text
    }
    if roles["legacy_section"] and not roles["article"]:
        return "legacy_notice"
    if roles["toc_entry"]:
        return "standard_law_with_toc"
    if "Heading 2" in chapter_styles:
        return "heading_style_regulation"
    if chapter_styles != article_styles and article_styles:
        return "mixed_style_regulation"
    return "normal_style_regulation"


def finalize_chunk(
    current: dict[str, Any] | None,
    source_sha256: str,
    rule_version: str,
    document_id: str,
    version_id: str,
    title: str,
) -> dict[str, Any] | None:
    if not current:
        return None
    source_blocks: list[SourceBlock] = current.pop("_source_blocks")
    text = "\n".join(block.text for block in source_blocks if block.text)
    text_hash = sha256_text(text)
    ordinal = current["ordinal"]
    anchor = current["anchor"]
    chunk_id = sha256_text(
        f"{source_sha256}|{rule_version}|{anchor}|{ordinal}|{text_hash}"
    )
    paragraph_indices = [
        block.paragraph_index
        for block in source_blocks
        if block.paragraph_index is not None
    ]
    current.update(
        {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "version_id": version_id,
            "document_title": title,
            "text": text,
            "text_sha256": text_hash,
            "source": {
                "paragraph_start": min(paragraph_indices) if paragraph_indices else None,
                "paragraph_end": max(paragraph_indices) if paragraph_indices else None,
                "paragraph_indices": paragraph_indices,
                "block_indices": [block.block_index for block in source_blocks],
            },
            "source_roles": [block.role for block in source_blocks],
        }
    )
    return current


def build_chunks(
    blocks: list[SourceBlock],
    title: str,
    source_sha256: str,
    rule_version: str,
) -> list[dict[str, Any]]:
    document_id = "doc_" + sha256_text(normalize_for_match(title))[:24]
    version_id = "ver_" + source_sha256[:24]
    has_articles = any(block.role == "article" for block in blocks)
    anchor_role = "article" if has_articles else "legacy_section"
    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_chapter: str | None = None
    current_section: str | None = None
    ordinal = 0
    preamble_blocks: list[SourceBlock] = []

    for block in blocks:
        if not block.text:
            continue
        if block.role == "chapter":
            completed = finalize_chunk(
                current, source_sha256, rule_version, document_id, version_id, title
            )
            if completed:
                chunks.append(completed)
                current = None
            current_chapter = block.text
            current_section = None
            continue
        if block.role == "section":
            completed = finalize_chunk(
                current, source_sha256, rule_version, document_id, version_id, title
            )
            if completed:
                chunks.append(completed)
                current = None
            current_section = block.text
            continue
        if block.role == anchor_role:
            if not has_articles and preamble_blocks and current is None:
                ordinal += 1
                preamble = {
                    "ordinal": ordinal,
                    "anchor": "前言",
                    "anchor_role": "preamble",
                    "path": [title, "前言"],
                    "_source_blocks": list(preamble_blocks),
                }
                completed_preamble = finalize_chunk(
                    preamble,
                    source_sha256,
                    rule_version,
                    document_id,
                    version_id,
                    title,
                )
                if completed_preamble:
                    chunks.append(completed_preamble)
                preamble_blocks.clear()
            completed = finalize_chunk(
                current, source_sha256, rule_version, document_id, version_id, title
            )
            if completed:
                chunks.append(completed)
            ordinal += 1
            path = [title]
            if current_chapter:
                path.append(current_chapter)
            if current_section:
                path.append(current_section)
            path.append(block.text.split("\u3000", 1)[0])
            current = {
                "ordinal": ordinal,
                "anchor": block.text.split("\u3000", 1)[0],
                "anchor_role": anchor_role,
                "path": path,
                "_source_blocks": [block],
            }
            continue
        if block.role in {"title", "metadata", "toc_heading", "toc_entry"}:
            continue
        if current is not None:
            current["_source_blocks"].append(block)
        elif not has_articles and block.role == "body":
            preamble_blocks.append(block)

    completed = finalize_chunk(
        current, source_sha256, rule_version, document_id, version_id, title
    )
    if completed:
        chunks.append(completed)
    return chunks


def sequence_values(blocks: list[SourceBlock], role: str) -> list[int]:
    return [
        block.number
        for block in blocks
        if block.role == role and block.number is not None
    ]


def parse_document(path: Path, rules: dict[str, Any]) -> dict[str, Any]:
    source_sha256 = sha256_file(path)
    blocks, features = read_source_blocks(path)
    title_hint = title_hint_from_filename(path)
    title_info = classify_blocks(blocks, rules, title_hint)
    profile = choose_format_profile(blocks)
    chunks = build_chunks(
        blocks,
        title_info["title"],
        source_sha256,
        rules["rule_version"],
    )

    styles = Counter(
        block.style_name
        for block in blocks
        if block.text and block.style_name is not None
    )
    role_counts = Counter(block.role for block in blocks if block.text)
    chapter_values = sequence_values(blocks, "chapter")
    article_values = sequence_values(blocks, "article")
    legacy_values = sequence_values(blocks, "legacy_section")
    item_count = role_counts["item_cn"] + role_counts["item_arabic"]

    profile_data = {
        "file": path.name,
        "sha256": source_sha256,
        "title": title_info["title"],
        "title_hint": title_hint,
        "title_paragraphs": title_info["title_paragraphs"],
        "format_profile": profile,
        "rule_version": rules["rule_version"],
        "paragraphs": features["python_docx_paragraph_count"],
        "nonempty_paragraphs": features["nonempty_paragraph_count"],
        "tables": features["python_docx_table_count"],
        "styles": dict(sorted(styles.items())),
        "role_counts": dict(sorted(role_counts.items())),
        "chapters": len(chapter_values),
        "chapter_numbers": chapter_values,
        "articles": len(article_values),
        "article_numbers": article_values,
        "legacy_sections": len(legacy_values),
        "legacy_section_numbers": legacy_values,
        "items": item_count,
        "toc_entries": role_counts["toc_entry"],
        "chunks": len(chunks),
        "max_chunk_characters": max((len(chunk["text"]) for chunk in chunks), default=0),
        "ooxml": features,
        "profile_adjustments": rules["profile_adjustments"][profile],
    }
    return {
        "profile": profile_data,
        "blocks": [asdict(block) for block in blocks],
        "chunks": chunks,
    }


def validate_result(result: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    profile = result["profile"]
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, wanted: Any) -> None:
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": wanted,
                "passed": actual == wanted,
            }
        )

    for key in (
        "title",
        "sha256",
        "paragraphs",
        "nonempty_paragraphs",
        "tables",
        "chapters",
        "articles",
        "legacy_sections",
        "items",
        "toc_entries",
        "title_paragraphs",
        "chunks",
        "format_profile",
    ):
        check(key, profile[key], expected[key])

    article_numbers = profile["article_numbers"]
    first_article = article_numbers[0] if article_numbers else None
    last_article = article_numbers[-1] if article_numbers else None
    check("first_article_number", first_article, expected["first_article_number"])
    check("last_article_number", last_article, expected["last_article_number"])
    check(
        "article_sequence_complete",
        article_numbers,
        list(range(1, len(article_numbers) + 1)),
    )
    legacy_numbers = profile["legacy_section_numbers"]
    check(
        "legacy_sequence_complete",
        legacy_numbers,
        list(range(1, len(legacy_numbers) + 1)),
    )
    check("unhandled_body_elements", profile["ooxml"]["unhandled_body_elements"], [])
    check("text_box_count", profile["ooxml"]["text_box_count"], 0)
    check("alt_chunk_count", profile["ooxml"]["alt_chunk_count"], 0)
    nondecorative_header_footer = [
        name
        for name, text in profile["ooxml"]["header_footer_text"].items()
        if text and not re.fullmatch(r"[\s\-－—–\d]+", text)
    ]
    check("nondecorative_header_footer_text", nondecorative_header_footer, [])

    block_lookup = {
        block["paragraph_index"]: block["text"]
        for block in result["blocks"]
        if block["paragraph_index"] is not None
    }
    citation_roundtrip = True
    for chunk in result["chunks"]:
        rebuilt = "\n".join(
            block_lookup[index] for index in chunk["source"]["paragraph_indices"]
        )
        if rebuilt != chunk["text"]:
            citation_roundtrip = False
            break
    check("citation_roundtrip", citation_roundtrip, True)

    orphan_items = []
    chunk_paragraphs = {
        index
        for chunk in result["chunks"]
        for index in chunk["source"]["paragraph_indices"]
    }
    for block in result["blocks"]:
        if block["role"] in {"item_cn", "item_arabic"} and block["paragraph_index"] not in chunk_paragraphs:
            orphan_items.append(block["paragraph_index"])
    check("orphan_item_paragraphs", orphan_items, [])
    unchunked_substantive = [
        block["paragraph_index"]
        for block in result["blocks"]
        if block["paragraph_index"] is not None
        and block["text"]
        and block["role"]
        in {"article", "legacy_section", "item_cn", "item_arabic", "body"}
        and block["paragraph_index"] not in chunk_paragraphs
    ]
    check("unchunked_substantive_paragraphs", unchunked_substantive, [])
    return checks
