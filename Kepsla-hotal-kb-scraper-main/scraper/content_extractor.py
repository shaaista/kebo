"""Extract clean, structured content from raw HTML.

Produces a rich dict with title, main text, markdown, tables,
images, contact info, JSON-LD structured data, and meta tags.
"""

import json
import logging
import re
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

logger = logging.getLogger(__name__)

# ── Regex Patterns ───────────────────────────────────────────────────────────

# Phone numbers: international formats, US/India formats, with optional ext
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,4}[\s\-.]?)?"          # country code
    r"(?:\(?\d{1,5}\)?[\s\-.]?)?"        # area code
    r"\d{2,5}[\s\-.]?\d{2,5}"            # main digits
    r"(?:[\s\-.]?\d{1,5})?"              # optional extra group
    r"(?:\s*(?:ext|x|extension)\.?\s*\d{1,5})?",  # extension
    re.IGNORECASE,
)

# Email addresses
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)


# ── Helper Utilities ─────────────────────────────────────────────────────────


def make_absolute_url(url: str, base_url: str) -> str:
    """Convert a relative URL to absolute using the base URL."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return ""
    try:
        return urljoin(base_url, url)
    except Exception:
        return url


def _clean_text(text: str) -> str:
    """Remove excess whitespace, normalize unicode, strip control characters."""
    if not text:
        return ""
    # Normalize unicode (NFC form)
    text = unicodedata.normalize("NFC", text)
    # Remove control characters except newlines and tabs
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse multiple spaces/tabs within a line
    text = re.sub(r"[^\S\n]+", " ", text)
    # Collapse 3+ consecutive newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Table Extraction ─────────────────────────────────────────────────────────


def extract_tables(soup: BeautifulSoup) -> list[dict]:
    """Extract HTML tables as structured data.

    Returns a list of dicts, each with:
      - headers: list[str]
      - rows: list[list[str]]
    """
    tables: list[dict] = []

    for table in soup.find_all("table"):
        try:
            headers: list[str] = []
            rows: list[list[str]] = []

            # Extract headers from <thead> or first <tr> with <th>
            thead = table.find("thead")
            if thead:
                th_elements = thead.find_all("th")
                headers = [th.get_text(strip=True) for th in th_elements]
            else:
                first_row = table.find("tr")
                if first_row:
                    ths = first_row.find_all("th")
                    if ths:
                        headers = [th.get_text(strip=True) for th in ths]

            # Extract body rows
            tbody = table.find("tbody")
            row_elements = tbody.find_all("tr") if tbody else table.find_all("tr")

            for tr in row_elements:
                cells = tr.find_all(["td", "th"])
                row_data = [cell.get_text(strip=True) for cell in cells]
                # Skip header row if it matches what we already extracted
                if row_data and row_data != headers:
                    rows.append(row_data)

            # Only include non-empty tables
            if rows:
                tables.append({"headers": headers, "rows": rows})

        except Exception as exc:
            logger.debug("Error extracting table: %s", exc)
            continue

    return tables


# ── Contact Info Extraction ──────────────────────────────────────────────────


def extract_contact_info(text: str, soup: BeautifulSoup) -> dict:
    """Extract phone numbers, email addresses, and address information.

    Returns:
        dict with keys: phones, emails, address
    """
    phones: list[str] = []
    emails: list[str] = []
    address: str = ""

    # ── Emails ──
    email_matches = _EMAIL_RE.findall(text)
    # Also check href="mailto:" links
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip()
            if email:
                email_matches.append(email)

    # Deduplicate and filter out obvious non-emails
    seen_emails: set[str] = set()
    for email in email_matches:
        email_lower = email.lower().strip()
        if email_lower not in seen_emails and not email_lower.endswith((".png", ".jpg", ".css", ".js")):
            seen_emails.add(email_lower)
            emails.append(email_lower)

    # ── Phone numbers ──
    # Check tel: links first (most reliable)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.startswith("tel:"):
            phone = href.replace("tel:", "").strip()
            if phone and len(phone) >= 7:
                phones.append(phone)

    # Regex-based phone extraction from text
    phone_matches = _PHONE_RE.findall(text)
    seen_phones: set[str] = set(phones)
    for phone in phone_matches:
        cleaned = re.sub(r"[\s\-.]", "", phone)
        # Only keep phone-like strings with enough digits
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) >= 7 and cleaned not in seen_phones:
            seen_phones.add(cleaned)
            phones.append(phone.strip())

    # Limit to avoid false positives
    phones = phones[:10]
    emails = emails[:10]

    # ── Address ──
    # Look for common address markup
    address_selectors = [
        {"itemprop": "address"},
        {"class_": re.compile(r"address", re.I)},
        {"itemtype": re.compile(r"schema.org/PostalAddress")},
    ]
    for selector in address_selectors:
        addr_el = soup.find(attrs=selector)
        if addr_el:
            address = addr_el.get_text(separator=", ", strip=True)
            break

    # Fallback: look for <address> tag
    if not address:
        addr_tag = soup.find("address")
        if addr_tag:
            address = addr_tag.get_text(separator=", ", strip=True)

    return {
        "phones": phones,
        "emails": emails,
        "address": _clean_text(address),
    }


# ── Structured Data (JSON-LD) ───────────────────────────────────────────────


def extract_structured_data(soup: BeautifulSoup) -> list[dict]:
    """Extract JSON-LD structured data from <script type="application/ld+json"> tags."""
    structured: list[dict] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            data = json.loads(raw)
            if isinstance(data, list):
                structured.extend(data)
            elif isinstance(data, dict):
                structured.append(data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("Failed to parse JSON-LD: %s", exc)
            continue

    return structured


# ── Meta Tags ────────────────────────────────────────────────────────────────


def _extract_meta(soup: BeautifulSoup) -> dict:
    """Extract common meta tags (description, og:title, og:description, etc.)."""
    meta: dict[str, str] = {}

    # Standard meta description
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag and isinstance(desc_tag, Tag):
        meta["description"] = desc_tag.get("content", "")

    # Open Graph tags
    for og_prop in ("og:title", "og:description", "og:image", "og:url", "og:type", "og:site_name"):
        tag = soup.find("meta", attrs={"property": og_prop})
        if tag and isinstance(tag, Tag):
            meta[og_prop] = tag.get("content", "")

    # Twitter card tags
    for tw_name in ("twitter:title", "twitter:description", "twitter:image"):
        tag = soup.find("meta", attrs={"name": tw_name})
        if tag and isinstance(tag, Tag):
            meta[tw_name] = tag.get("content", "")

    # Canonical URL
    canonical = soup.find("link", attrs={"rel": "canonical"})
    if canonical and isinstance(canonical, Tag):
        meta["canonical"] = canonical.get("href", "")

    return meta


# ── Image Extraction ─────────────────────────────────────────────────────────


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract all images with src, alt, title, and surrounding context."""
    images: list[dict] = []
    seen_srcs: set[str] = set()

    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "") or img.get("data-lazy-src", "")
        if not src:
            continue

        absolute_src = make_absolute_url(src, base_url)
        if not absolute_src or absolute_src in seen_srcs:
            continue

        seen_srcs.add(absolute_src)
        alt = img.get("alt", "").strip()
        title = img.get("title", "").strip()

        # Try to get description from nearby context
        description = ""
        # Check for <figcaption> if inside a <figure>
        figure = img.find_parent("figure")
        if figure:
            figcaption = figure.find("figcaption")
            if figcaption:
                description = figcaption.get_text(strip=True)
        # Fall back to parent element's text or nearby sibling
        if not description:
            parent = img.parent
            if parent and parent.name not in ("body", "html", "head"):
                siblings_text = parent.get_text(strip=True)
                # Only use if it's not too long (avoid grabbing entire page sections)
                if siblings_text and len(siblings_text) < 300:
                    description = siblings_text

        images.append({
            "src": absolute_src,
            "alt": alt,
            "title": title or alt,
            "description": description,
            "source_page": base_url,
        })

    return images


_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".avi"}
_FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt",
}


def _extract_linked_assets(soup: BeautifulSoup, base_url: str) -> tuple[list[dict], list[dict]]:
    """Extract linked videos and files from anchors and video tags."""
    videos: list[dict] = []
    files: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        href = make_absolute_url(anchor.get("href", ""), base_url)
        if not href:
            continue

        suffix = Path(urlparse(href).path).suffix.lower()
        if suffix not in _VIDEO_EXTENSIONS and suffix not in _FILE_EXTENSIONS:
            continue

        title = anchor.get("title", "").strip() or anchor.get_text(" ", strip=True)
        description = anchor.get("aria-label", "").strip() or title
        kind = "video" if suffix in _VIDEO_EXTENSIONS else "file"
        key = (kind, href)
        if key in seen:
            continue
        seen.add(key)

        asset = {
            "url": href,
            "title": title or Path(urlparse(href).path).name,
            "description": description,
            "source_page": base_url,
        }
        if kind == "video":
            videos.append(asset)
        else:
            files.append(asset)

    for video_tag in soup.find_all("video"):
        sources = []
        if video_tag.get("src"):
            sources.append(video_tag.get("src"))
        for source_tag in video_tag.find_all("source", src=True):
            sources.append(source_tag.get("src"))

        for source in sources:
            href = make_absolute_url(source or "", base_url)
            if not href:
                continue

            key = ("video", href)
            if key in seen:
                continue
            seen.add(key)

            videos.append({
                "url": href,
                "title": video_tag.get("title", "").strip() or Path(urlparse(href).path).name,
                "description": video_tag.get("aria-label", "").strip(),
                "source_page": base_url,
            })

    return videos, files


# ── Title Extraction ─────────────────────────────────────────────────────────


def _extract_title(soup: BeautifulSoup) -> str:
    """Extract page title from <title> or <h1>."""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        if title:
            return title

    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)

    return ""


# ── Main Text Extraction ────────────────────────────────────────────────────


def _extract_main_text(html: str, url: str) -> str:
    """Extract main readable text using trafilatura with BS4 fallback."""
    # Primary: trafilatura
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_recall=True,
        )
        if text and len(text.strip()) > 50:
            return _clean_text(text)
    except Exception as exc:
        logger.debug("trafilatura failed for %s: %s", url, exc)

    # Fallback: readability-lxml
    try:
        from readability import Document as ReadabilityDocument
        doc = ReadabilityDocument(html, url=url)
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        if text and len(text.strip()) > 50:
            return _clean_text(text)
    except Exception as exc:
        logger.debug("readability failed for %s: %s", url, exc)

    # Last resort: BeautifulSoup text extraction
    try:
        soup = BeautifulSoup(html, "lxml")
        for el in soup(["script", "style", "nav", "footer"]):
            el.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return _clean_text(text)
    except Exception:
        return ""


# ── Public API ───────────────────────────────────────────────────────────────


def extract_content(html: str, url: str) -> dict:
    """Extract clean, structured content from raw HTML.

    Returns:
        dict with keys:
          - title: str
          - main_text: str (cleaned readable text)
          - markdown: str (full HTML converted to markdown)
          - tables: list[dict] (structured table data)
          - images: list[dict] (image src + alt)
          - contacts: dict (phones, emails, address)
          - structured_data: list[dict] (JSON-LD)
          - meta: dict (description, og tags, etc.)
    """
    if not html or not html.strip():
        logger.warning("Empty HTML provided for %s", url)
        return {
            "title": "",
            "main_text": "",
            "markdown": "",
            "tables": [],
            "images": [],
            "videos": [],
            "files": [],
            "contacts": {"phones": [], "emails": [], "address": ""},
            "structured_data": [],
            "meta": {},
        }

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        logger.error("Failed to parse HTML for %s: %s", url, exc)
        return {
            "title": "",
            "main_text": "",
            "markdown": "",
            "tables": [],
            "images": [],
            "videos": [],
            "files": [],
            "contacts": {"phones": [], "emails": [], "address": ""},
            "structured_data": [],
            "meta": {},
        }

    # Title
    title = _extract_title(soup)

    # Main text (trafilatura -> readability -> bs4)
    main_text = _extract_main_text(html, url)

    # Markdown conversion
    try:
        md = markdownify(html, heading_style="ATX", strip=["script", "style"])
        md = _clean_text(md)
    except Exception as exc:
        logger.debug("markdownify failed for %s: %s", url, exc)
        md = ""

    # Tables
    tables = extract_tables(soup)

    # Images
    images = _extract_images(soup, url)
    videos, files = _extract_linked_assets(soup, url)

    # Contact information
    full_text = soup.get_text(separator=" ", strip=True)
    contacts = extract_contact_info(full_text, soup)

    # JSON-LD structured data
    structured_data = extract_structured_data(soup)

    # Meta tags
    meta = _extract_meta(soup)

    logger.debug(
        "Extracted content for %s: title=%r, text_len=%d, tables=%d, images=%d, contacts=%s",
        url,
        title[:50],
        len(main_text),
        len(tables),
        len(images),
        {k: len(v) if isinstance(v, list) else bool(v) for k, v in contacts.items()},
    )

    return {
        "title": title,
        "main_text": main_text,
        "markdown": md,
        "tables": tables,
        "images": images,
        "videos": videos,
        "files": files,
        "contacts": contacts,
        "structured_data": structured_data,
        "meta": meta,
    }
