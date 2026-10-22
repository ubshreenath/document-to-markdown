#!/usr/bin/env python3
"""Convert documents to self-contained Markdown with Docling and keep a fingerprint log.

Standard library only; Docling is called as a command (`docling`, or `uvx --from docling docling`).
Exit codes: 0 ok, 1 error, 3 vision choice needed, 4 vision API key missing,
5 image-only PDF rendered to page images that need transcribing (then `record`).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"}
NATIVE = {".md", ".txt", ".csv", ".json"}
LOG_NAME = ".doc-to-markdown-log.json"
LEGACY_LOG_NAME = ".markitdown-extraction-log.json"  # old name from the retired pdf-to-markdown skill
CONFIG_PATH = Path(os.environ.get("DOC_TO_MD_CONFIG") or Path.home() / ".config" / "doc-to-markdown" / "config.json")
FIGURE_MIN_PAGE_SHARE = 0.10  # pictures smaller than this share of a page are decorative
FIGURE_MIN_PIXELS = 150       # fallback when the page size is unknown (docx, html)
CONVERT_TIMEOUT_SECONDS = 900
VISION_TIMEOUT_SECONDS = 180
VISION_ATTEMPTS = 3  # per page, for timeouts and dropped connections
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# Any OpenAI-compatible /chat/completions endpoint works; these are only shortcuts.
PRESETS = {
    "openai": ("https://api.openai.com/v1", ["OPENAI_API_KEY"]),
    "anthropic": ("https://api.anthropic.com/v1", ["ANTHROPIC_API_KEY"]),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ["GEMINI_API_KEY", "GOOGLE_API_KEY"]),
    "mistral": ("https://api.mistral.ai/v1", ["MISTRAL_API_KEY"]),
    "kimi": ("https://api.moonshot.ai/v1", ["MOONSHOT_API_KEY", "KIMI_API_KEY"]),  # mainland China: api.moonshot.cn
    "openrouter": ("https://openrouter.ai/api/v1", ["OPENROUTER_API_KEY"]),
    "groq": ("https://api.groq.com/openai/v1", ["GROQ_API_KEY"]),
    "together": ("https://api.together.xyz/v1", ["TOGETHER_API_KEY"]),
    "xai": ("https://api.x.ai/v1", ["XAI_API_KEY"]),
    # Local servers: documents never leave the machine, and no key is needed.
    "ollama": ("http://localhost:11434/v1", []),
    "lmstudio": ("http://localhost:1234/v1", []),
    "llamacpp": ("http://localhost:8080/v1", []),
    "vllm": ("http://localhost:8000/v1", []),
    "jan": ("http://localhost:1337/v1", []),
}
LOCAL_PRESETS = ("ollama", "lmstudio", "llamacpp", "vllm", "jan")
LOCAL_COMMANDS = {"ollama": "ollama", "lmstudio": "lms", "llamacpp": "llama-server", "vllm": "vllm", "jan": "jan"}
VISION_PROMPT = (
    "This image comes from a document. Describe it so a reader who cannot see it gets all of its "
    "information: the kind of figure, every label and number, and how the parts relate. Copy visible "
    "text verbatim. For a chart or table, give the data as a Markdown table. Do not speculate or add "
    "interpretation. If the image is only decoration (a logo, icon, divider or background) reply with "
    "exactly: DECORATIVE"
)
TRANSCRIBE_PROMPT = (
    "This image is one page of a document. Transcribe it to Markdown: every heading, paragraph, list, "
    "table and label, verbatim and in reading order, using Markdown headings, lists and tables. Keep "
    "side-by-side boxes or columns as separate sections, under the label printed on the page if there is "
    "one; never invent labels. Keep section numbers exactly as printed. Describe any chart or photo in one "
    "short paragraph. Output only the Markdown."
)
# Pages are sent one at a time, so the model is told where the page sits to handle headers and footers.
TRANSCRIBE_FIRST_PAGE = (
    " This is page 1 of {total}. Keep the document's title, version, date and classification once, as "
    "plain text at the top, but leave out the page footer and page number."
)
TRANSCRIBE_LATER_PAGE = (
    " This is page {number} of {total}. Leave out the running header and footer (logo, document or section "
    "name, version, classification, page number): they were already taken from page 1."
)
# Docling before 2.141 breaks on rapidocr 3.10 (a removed constant) and then reports "No OCR engine found".
OCR_MIN_DOCLING = (2, 141)
OCR_FIX = 'uv tool install --force --upgrade "docling[rapidocr]"'
OCR_CHECK_SCRIPT = "import onnxruntime, rapidocr; print('ok')"
# A tiny PNG reading "Falcon 7319", for `vision-test` (no document content is sent).
PROBE_TEXT = "7319"
PROBE_PNG = (
    "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"
)
SIDE_BY_SIDE_MIN_PAIRS = 2  # pages with this many side-by-side text blocks go to a vision model, not OCR
PAGE_RENDER_SCALE = 2  # 144 dpi: legible for OCR and vision without huge files
RENDER_SCRIPT = (
    "import sys, pypdfium2 as pdfium\n"
    "doc = pdfium.PdfDocument(sys.argv[1])\n"
    "for i in range(len(doc)):\n"
    "    doc[i].render(scale=float(sys.argv[3])).to_pil().save(f'{sys.argv[2]}/page-{i + 1:02d}.png')\n"
)


class NeedsVision(Exception):
    def __init__(self, figures, image_only=None, why="", ocr_ok=False):
        self.figures, self.image_only = figures, image_only  # image_only: the PDF waiting for transcription
        self.why, self.ocr_ok = why, ocr_ok


class PageImagesNeeded(Exception):
    def __init__(self, src, pages_dir, output, count, reason):
        self.src, self.pages_dir, self.output, self.count, self.reason = src, pages_dir, output, count, reason


class KeyMissing(Exception):
    pass


class Conflict(Exception):
    pass


def say(message):
    print(f"doc_to_md: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- log and index

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path, root):
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def log_path_for(folder, override=None):
    if override:
        return Path(override)
    current, legacy = folder / LOG_NAME, folder / LEGACY_LOG_NAME
    if legacy.exists() and not current.exists():
        legacy.replace(current)  # one-time rename; the history inside is kept as is
        say(f"renamed {LEGACY_LOG_NAME} to {LOG_NAME}")
    return current


def load_log(path):
    if not path.exists():
        return {"version": 1, "entries": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"Invalid log: {path}")
    return data


def atomic_write(path, text):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    temporary.replace(path)


def entry_matches(entry, src, root):
    return (root / entry.get("source", "")).resolve() == src.resolve()


def append_log(log, log_file, src, digest, output, root, converter, summary):
    log["version"] = 1
    log["entries"].append({"source": rel(src, root), "sha256": digest, "output": rel(output, root),
                           "converted_at": datetime.now(timezone.utc).isoformat(),
                           "converter": converter, "figures": summary})
    atomic_write(log_file, json.dumps(log, indent=2) + "\n")


def write_index(out_dir, log, root):
    latest = {}
    for entry in log["entries"]:
        latest[entry["source"]] = entry
    rows = ["| Markdown | Source | Converted (UTC) | Figures |", "|---|---|---|---|"]
    for entry in sorted(latest.values(), key=lambda e: e["output"].lower()):
        output = (root / entry["output"]).resolve()
        if output.parent != out_dir.resolve():
            continue
        link = quote(output.name)
        rows.append(f"| [{output.name}]({link}) | {Path(entry['source']).name} | "
                    f"{entry['converted_at'][:19]} | {entry.get('figures', '')} |")
    text = ("# Extracted documents\n\nSelf-contained Markdown copies of source documents. "
            "Read these instead of re-converting the originals. Generated; do not edit.\n\n"
            + "\n".join(rows) + "\n")
    atomic_write(out_dir / "INDEX.md", text)


# ---------------------------------------------------------------- vision config

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_PATH, json.dumps(config, indent=2) + "\n")


def get_json(url, body=None, timeout=1.5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def local_servers():
    """Local model servers that are installed or running. Only localhost is probed."""
    found = []
    for name in LOCAL_PRESETS:
        base_url = PRESETS[name][0]
        entry = {"preset": name, "base_url": base_url, "installed": bool(shutil.which(LOCAL_COMMANDS[name])),
                 "running": False, "models": []}
        try:
            entry["models"] = sorted(m["id"] for m in get_json(base_url + "/models").get("data", []))
            entry["running"] = True
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
            pass
        if name == "ollama" and entry["running"]:
            # Ollama reports which models can read images; other servers do not say.
            vision = []
            for model in entry["models"]:
                try:
                    if "vision" in get_json("http://localhost:11434/api/show", {"model": model}).get("capabilities", []):
                        vision.append(model)
                except (urllib.error.URLError, OSError, ValueError):
                    pass
            entry["vision_models"] = vision
        if entry["installed"] or entry["running"]:
            found.append(entry)
    return found


def vision_status():
    config = load_config().get("vision")
    known = sorted({name for _, names in PRESETS.values() for name in names})
    present = [name for name in known if os.environ.get(name)]
    status = {
        "configured": bool(config),
        "config": config,
        "key_present": None,
        "env_vars_present": present,
        "cloud_presets_with_key": [p for p, (_, names) in PRESETS.items() if any(n in present for n in names)],
        "local_servers": local_servers(),
        "presets": {name: {"base_url": url, "key_env_candidates": names} for name, (url, names) in PRESETS.items()},
    }
    if config and config.get("mode") == "api":
        status["key_present"] = (not config.get("key_env")) or bool(os.environ.get(config["key_env"]))
    return status


def describe_image(vision, image_path, prompt=VISION_PROMPT):
    key = ""
    if vision.get("key_env"):
        key = os.environ.get(vision["key_env"], "")
        if not key:
            raise KeyMissing(vision["key_env"])
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = json.dumps({
        "model": vision["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]}],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        # Most providers take "Authorization: Bearer"; some (such as Azure OpenAI) want the bare key in a header.
        header = vision.get("auth_header") or "Authorization"
        headers[header] = f"Bearer {key}" if header == "Authorization" else key
    base, _, query = vision["base_url"].partition("?")  # keep a query such as ?api-version=... at the end
    url = base.rstrip("/") + "/chat/completions" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, body, headers)
    with urllib.request.urlopen(request, timeout=VISION_TIMEOUT_SECONDS) as response:
        reply = json.load(response)
    content = reply["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content.strip()


# ---------------------------------------------------------------- Docling

def run_docling(src, work_dir, extra=()):
    """Run Docling; returns its log output (stderr), which reports problems such as missing OCR."""
    arguments = [str(src), "--to", "md", "--to", "json", "--image-export-mode", "referenced",
                 "--output", str(work_dir), *extra]
    error = None
    for command in (["docling"], ["uvx", "--from", "docling", "docling"]):
        if shutil.which(command[0]) is None:
            continue
        done = subprocess.run(command + arguments, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CONVERT_TIMEOUT_SECONDS)
        if done.returncode == 0:
            return done.stderr or ""
        error = (done.stderr or done.stdout).strip().splitlines()[-1:]
    if error is None:
        raise RuntimeError("Docling not found; install it once per machine with: uv tool install docling")
    raise RuntimeError("Docling failed: " + " ".join(error))


def ocr_problem(docling_log):
    """A short reason when Docling's log shows OCR could not run, else None."""
    if "No OCR engine found" in docling_log:
        return f"has no working OCR engine (run `doctor`; fix: {OCR_FIX})"
    match = re.search(r"(?m)^.*(?:ImportError|ModuleNotFoundError|FileNotFoundError).*$", docling_log)
    return f"failed ({match.group(0).strip()[:160]})" if match else None


# ---------------------------------------------------------------- image-only PDFs

def pdf_has_text_layer(src):
    """True if the PDF has fonts, False if it has none (every page is a picture), None if unsure.

    Font dictionaries may sit in compressed object streams, so streams are inflated too."""
    try:
        data = src.read_bytes()
    except OSError:
        return None
    if b"/Encrypt" in data:
        return None  # compressed parts are encrypted and cannot be checked
    if b"/Font" in data:
        return True
    for match in re.finditer(rb"(?<!end)stream\r?\n", data):
        end = data.find(b"endstream", match.end())
        if end < 0:
            break
        try:
            chunk = zlib.decompressobj().decompress(data[match.end():end])
        except zlib.error:
            continue
        if b"/Font" in chunk:
            return True
    return False


def docling_python():
    """The Python inside Docling's tool environment, which has pypdfium2 for rendering pages."""
    if shutil.which("uv"):
        done = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True)
        if done.returncode == 0:
            base = Path(done.stdout.strip()) / "docling"
            for candidate in (base / "Scripts" / "python.exe", base / "bin" / "python"):
                if candidate.exists():
                    return [str(candidate)]
    if shutil.which("uvx"):
        return ["uvx", "--from", "docling", "python"]
    return None


def render_pages(src, target):
    python = docling_python()
    if python is None:
        raise RuntimeError("cannot render page images: Docling's Python environment was not found")
    target.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(python + ["-I", "-c", RENDER_SCRIPT, str(src), str(target), str(PAGE_RENDER_SCALE)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=CONVERT_TIMEOUT_SECONDS)
    pages = sorted(target.glob("page-*.png"))
    if done.returncode != 0 or not pages:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:]
        raise RuntimeError("cannot render page images: " + " ".join(detail))
    return pages


def side_by_side_pages(document):
    """Pages whose layout OCR is likely to mangle: text blocks sitting next to each other.

    Counts pairs of real text blocks (3+ words, not headers or footers) that overlap vertically but not
    horizontally. Single-column pages, tables included, score 0; card rows and two-column boxes score 4+."""
    pages = document.get("pages", {})
    blocks = {}
    for item in document.get("texts", []):
        provenance = (item.get("prov") or [{}])[0]
        box = provenance.get("bbox")
        if (not box or item.get("label") in ("page_header", "page_footer")
                or len(item.get("text", "").split()) < 3):
            continue
        blocks.setdefault(provenance.get("page_no"), []).append(
            (min(box["l"], box["r"]), max(box["l"], box["r"]), min(box["t"], box["b"]), max(box["t"], box["b"])))
    flagged = []
    for page, items in sorted(blocks.items()):
        width = pages.get(str(page), {}).get("size", {}).get("width") or 1
        pairs = 0
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                gap = max(a[0], b[0]) - min(a[1], b[1])
                overlap = min(a[3], b[3]) - max(a[2], b[2])
                if gap > 0.01 * width and overlap > 0.5 * min(a[3] - a[2], b[3] - b[2]):
                    pairs += 1
        if pairs >= SIDE_BY_SIDE_MIN_PAIRS:
            flagged.append(page)
    return flagged


def api_vision(options):
    """The saved vision API config if this run may use it, else None."""
    config = load_config().get("vision")
    return config if options.vision != "skip" and config and config.get("mode") == "api" else None


def transcribe_pages(src, vision):
    """Transcribe every page with the vision API. Returns Markdown, or None if the API call failed."""
    staged = Path(tempfile.mkdtemp(prefix="dlp"))
    try:
        pages = render_pages(src, staged)
        say(f"transcribing {len(pages)} page image(s) with {vision.get('provider')} ({vision.get('model')})")
        texts = []
        for number, page in enumerate(pages, 1):
            where = TRANSCRIBE_FIRST_PAGE if number == 1 else TRANSCRIBE_LATER_PAGE
            prompt = TRANSCRIBE_PROMPT + where.format(number=number, total=len(pages))
            for attempt in range(1, VISION_ATTEMPTS + 1):
                try:
                    texts.append(describe_image(vision, page, prompt))
                    break
                except (urllib.error.URLError, OSError) as error:
                    # Timeouts and dropped connections are usually transient; a bad key or URL fails every time.
                    if attempt == VISION_ATTEMPTS or getattr(error, "code", 0) in (400, 401, 403, 404):
                        raise
                    say(f"page {number}: vision call failed ({error}); retrying ({attempt + 1}/{VISION_ATTEMPTS})")
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as error:
        say(f"WARNING vision transcription failed ({error})")
        return None
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    body = "\n\n".join(f"<!-- page {n} -->\n\n{text.strip()}" for n, text in enumerate(texts, 1))
    return (f"<!-- Image-only PDF (no text layer); transcribed from page images by "
            f"{vision.get('model')}. Check key numbers against the source. -->\n\n{body}\n")


def image_only_route(src, options, ocr_ok, columns, reason):
    """Pick how an image-only PDF is read: "ocr" (local, fine for single-column pages), "vision" (a
    model transcribes the page images; better for side-by-side blocks, or when OCR found nothing),
    or "pages" (the agent transcribes the page images itself)."""
    if ocr_ok and not columns:
        say(f"IMAGE-ONLY {src.name}: single-column layout; using the OCR text")
        return "ocr"
    why = (f"pages {', '.join(map(str, columns))} have side-by-side text blocks" if ocr_ok
           else f"Docling OCR {reason}")
    if api_vision(options):
        say(f"IMAGE-ONLY {src.name}: {why}; using the vision model instead of OCR")
        return "vision"
    if options.vision == "auto" and load_config().get("vision") is None and not options.unattended:
        raise NeedsVision([], image_only=src, why=why, ocr_ok=ocr_ok)
    if ocr_ok:
        say(f"IMAGE-ONLY {src.name}: {why}, and no vision model is configured; using OCR (needs a deeper review)")
        return "ocr"
    say(f"IMAGE-ONLY {src.name}: {why}, and no vision model is configured; falling back to page images")
    return "pages"


def leave_page_images(src, output, reason):
    """No OCR text and no vision API: render the pages for the agent to transcribe, then `record`."""
    staged = Path(tempfile.mkdtemp(prefix="dlp"))
    pages = render_pages(src, staged)
    pages_dir = output.parent / f"{src.stem}_pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), pages_dir)
    raise PageImagesNeeded(src, pages_dir, output, len(pages), reason)


def png_size(path):
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


def analyse_pictures(document, artifacts):
    """Classify every picture Docling found.

    Docling also labels vector graphics (timelines, card rows) as pictures and nests their text
    inside them, hiding it from the Markdown. A picture that holds text is therefore text, not an
    image: its text is recovered from the JSON, and it is exported or described only on request."""
    pages = document.get("pages", {})
    texts = document.get("texts", [])
    files = {}
    if artifacts is not None and artifacts.is_dir():
        for path in artifacts.glob("image_*.png"):
            match = re.match(r"image_(\d+)_", path.name)
            if match:
                files[int(match.group(1))] = path
    found = []
    for index, picture in enumerate(document.get("pictures", [])):
        provenance = (picture.get("prov") or [{}])[0]
        page, box = provenance.get("page_no"), provenance.get("bbox")
        page_size = pages.get(str(page), {}).get("size")
        share = None
        if box and page_size:
            share = abs((box["r"] - box["l"]) * (box["t"] - box["b"])) / (page_size["width"] * page_size["height"])

        lines, child_ids = [], set()
        for child in picture.get("children", []):
            reference = child.get("$ref", "")
            if reference.startswith("#/texts/"):
                child_id = int(reference.rsplit("/", 1)[1])
                child_ids.add(child_id)
                text = " ".join(texts[child_id].get("text", "").split())
                if text:
                    lines.append(text)
        # Text that sits inside the picture's box but was kept in the normal flow.
        overlapping = 0
        if box:
            for text_id, item in enumerate(texts):
                item_prov = (item.get("prov") or [{}])[0]
                item_box = item_prov.get("bbox")
                if (text_id in child_ids or item_prov.get("page_no") != page or not item_box
                        or item_box.get("coord_origin") != box.get("coord_origin")):
                    continue
                centre_x = (item_box["l"] + item_box["r"]) / 2
                centre_y = (item_box["t"] + item_box["b"]) / 2
                if (box["l"] <= centre_x <= box["r"]
                        and min(box["t"], box["b"]) <= centre_y <= max(box["t"], box["b"])):
                    overlapping += 1

        image = files.get(index)
        dims = png_size(image) if image else None
        if share is not None:
            large = share >= FIGURE_MIN_PAGE_SHARE
        elif dims:
            large = min(dims) >= FIGURE_MIN_PIXELS and dims[0] * dims[1] >= 40000
        else:
            large = True
        found.append({"index": index, "page": page, "large": large, "lines": lines,
                      "overlapping": overlapping, "image": image})
    return found


def blockquote(text):
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def build_markdown(markdown, figures, stem, force_pages, vision, report):
    """Replace Docling's image links so the Markdown stands alone. Returns (markdown, staged_assets)."""
    links = IMAGE_LINK.findall(markdown)
    if len(links) != len(figures):
        report["warnings"].append(f"{len(links)} image links but {len(figures)} pictures; treating all as figures")
        figures = [{"index": i, "page": None, "large": True, "lines": [], "overlapping": 0,
                    "image": figures[i]["image"] if i < len(figures) else None} for i in range(len(links))]
    assets_name = f"{stem}_assets"
    staged = Path(tempfile.mkdtemp(prefix="dla")) / assets_name
    staged.mkdir(parents=True)
    queue = iter(figures)
    exported = 0

    def replace(_match):
        nonlocal exported
        figure = next(queue)
        page = figure["page"]
        where = f" (page {page})" if page else ""
        forced = page in force_pages
        has_text = bool(figure["lines"] or figure["overlapping"])
        parts = []

        if figure["lines"]:
            if figure["large"] or forced:
                parts.append(f"> **Figure{where}, text extracted from the graphic:**\n"
                             + "\n".join(f"> - {line}" for line in figure["lines"]))
                if figure["large"]:
                    report["text_graphics"].append((page, len(figure["lines"])))
            else:
                parts.append(" ".join(figure["lines"]))
                report["inline_text"] += 1
        if has_text and not forced:
            if not figure["lines"] and figure["large"]:
                report["warnings"].append(f"picture{where} overlaps page text that is already in the flow; dropped")
            return "\n\n".join(parts)
        if not figure["large"] and not forced:
            report["decorative"] += 1
            return "\n\n".join(parts)

        # A genuine large image (or a graphic the user forced): describe it or export it.
        image = figure["image"]
        if image is None:
            report["warnings"].append(f"figure{where} could not be exported by Docling")
            parts.append(f"> **Figure{where}:** [image could not be extracted]")
            return "\n\n".join(parts)
        if vision:
            try:
                description = describe_image(vision, image)
            except KeyMissing:
                raise
            except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as error:
                report["warnings"].append(f"vision call failed for figure{where} ({error}); exported instead")
            else:
                if description.strip().upper().rstrip(".") == "DECORATIVE":
                    report["decorative"] += 1
                    return "\n\n".join(parts)
                report["described"] += 1
                parts.append(f"> **Figure{where}:**\n" + blockquote(description))
                return "\n\n".join(parts)
        exported += 1
        name = f"figure-{exported:02d}" + (f"-p{page}" if page else "") + ".png"
        shutil.copyfile(image, staged / name)
        parts.append(f"![Figure{where}]({quote(assets_name)}/{name})")
        return "\n\n".join(parts)

    markdown = IMAGE_LINK.sub(replace, markdown)
    report["exported"] = exported
    if exported:
        markdown = (f"> **Note:** {exported} figure(s) from the original document are stored as images in "
                    f"[`{assets_name}/`]({quote(assets_name)}/) next to this file.\n\n" + markdown.lstrip())
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"
    if not exported:
        shutil.rmtree(staged.parent, ignore_errors=True)
        staged = None
    return markdown, staged


def resolve_vision(options, figures_needing_image):
    """Pick the vision backend: None means export images to an assets folder."""
    if not figures_needing_image or options.vision == "skip":
        return None
    config = load_config().get("vision")
    if options.vision == "auto" and config is None:
        if options.unattended:
            return None
        raise NeedsVision(figures_needing_image)
    if config is None or config.get("mode") == "skip":
        return None
    return config


def convert(src, out_dir, log_file, root, options):
    digest = sha256_file(src)
    log = load_log(log_file)
    matching = [e for e in log["entries"] if entry_matches(e, src, root)]
    output = out_dir / f"{src.stem}.md"
    if any(e.get("sha256") == digest for e in matching) and not options.force:
        logged = (root / matching[-1]["output"])
        note = "" if logged.exists() else " (output file is missing; use --force to rebuild)"
        say(f"HIT {src.name}{note}")
        return "HIT", logged
    status = "RECONVERTED" if matching and not options.force else ("FORCED" if matching else "CONVERTED")
    if output.exists() and not any((root / e["output"]).resolve() == output.resolve() for e in matching) \
            and not options.overwrite:
        raise Conflict(f"{rel(output, root)} exists but is not in the log; re-run with --overwrite to replace it")

    image_only = src.suffix.lower() == ".pdf" and pdf_has_text_layer(src) is False
    if image_only:
        say(f"IMAGE-ONLY {src.name}: the PDF has no text layer (every page is a picture); running full-page OCR")
    converter, staged = "docling", None
    report = {"warnings": [], "text_graphics": [], "inline_text": 0, "decorative": 0,
              "described": 0, "exported": 0}
    work = Path(tempfile.mkdtemp(prefix="dl"))  # short path: Docling writes long image names
    try:
        try:
            docling_log = run_docling(src, work, ["--ocr-mode", "full_page"] if image_only else [])
        except RuntimeError as error:
            if not image_only:
                raise
            docling_log = str(error)
        md_files, json_files = list(work.glob("*.md")), list(work.glob("*.json"))
        markdown = md_files[0].read_text(encoding="utf-8") if len(md_files) == 1 else ""
        route, columns = "docling", []
        if image_only:
            ocr_ok = bool(markdown.strip()) and len(json_files) == 1
            reason = None if ocr_ok else (ocr_problem(docling_log) or "returned no text")
            if ocr_ok:
                columns = side_by_side_pages(json.loads(json_files[0].read_text(encoding="utf-8")))
            route = image_only_route(src, options, ocr_ok, columns, reason)
            if route == "vision":
                transcribed = transcribe_pages(src, api_vision(options))
                if transcribed:
                    markdown, converter = transcribed, "vision-transcription"
                elif ocr_ok:
                    route = "ocr"
                    say(f"IMAGE-ONLY {src.name}: using the OCR text instead")
                else:
                    route = "pages"
            if route == "pages":
                leave_page_images(src, output, reason)
        if route not in ("vision", "pages"):
            if len(md_files) != 1 or len(json_files) != 1:
                raise RuntimeError("Docling did not produce exactly one Markdown and one JSON file")
            if not markdown.strip():
                raise RuntimeError("Docling returned no Markdown text")
            document = json.loads(json_files[0].read_text(encoding="utf-8"))
            artifacts = next((p for p in work.iterdir() if p.is_dir() and p.name.endswith("_artifacts")), None)
            figures = analyse_pictures(document, artifacts)

            force_pages = set(options.force_figure_pages)
            needing = [f for f in figures
                       if (f["large"] and not (f["lines"] or f["overlapping"])) or f["page"] in force_pages]
            vision = resolve_vision(options, needing)
            markdown, staged = build_markdown(markdown, figures, src.stem, force_pages, vision, report)
            if image_only:
                converter = "docling-ocr"
                markdown = ("<!-- Image-only PDF (no text layer); text recovered by OCR. "
                            "Check key numbers against the source. -->\n\n" + markdown)
                if columns:
                    report["warnings"].append(
                        f"text came from OCR, and pages {', '.join(map(str, columns))} have side-by-side text "
                        "blocks: these need a deeper review, because OCR often merges or reorders columns. "
                        "A vision model would transcribe them more faithfully (see `vision-status`)")
                report["warnings"].append(
                    "text came from OCR: check small or coloured text and letters such as AI/Al against the "
                    "page; fix the Markdown by hand")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / f"{src.stem}_assets"
    if assets.exists():
        shutil.rmtree(assets)
    if staged is not None:
        shutil.move(str(staged), assets)
        shutil.rmtree(staged.parent, ignore_errors=True)
    atomic_write(output, markdown)

    if converter == "vision-transcription":
        summary = "image-only PDF, transcribed from page images by the vision API"
    else:
        summary = (f"{report['described']} described, {report['exported']} exported, "
                   f"{len(report['text_graphics'])} text graphic(s), {report['decorative']} decorative dropped")
    append_log(log, log_file, src, digest, output, root, converter, summary)
    write_index(out_dir, log, root)

    say(f"{status} {src.name} -> {rel(output, root)} ({summary})")
    for page, count in report["text_graphics"]:
        say(f"REVIEW page {page}: large graphic kept as {count} text items. If it is a diagram where arrows "
            f"or layout carry meaning, look at the page and re-run with --force-figure-pages {page}")
    for warning in report["warnings"]:
        say(f"WARNING {warning}")
    return status, output


# ---------------------------------------------------------------- commands

def documents_in(folder, recursive):
    paths = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in paths if p.is_file() and p.suffix.lower() in SUPPORTED
                   and "extracted" not in p.relative_to(folder).parts[:-1]), key=lambda p: str(p).lower())


def out_dir_for(src_dir, args):
    chosen = args.out or os.environ.get("DOC_TO_MD_OUT")
    return Path(chosen).resolve() if chosen else src_dir / "extracted"


def handle_run(function, *call_args):
    try:
        return function(*call_args), 0
    except NeedsVision as needed:
        if needed.image_only:
            without = ("the copy will come from OCR and need a deeper review against the source" if needed.ocr_ok
                       else "you will get page images to transcribe yourself")
            say(f"VISION CHOICE NEEDED: {needed.image_only.name} is an image-only PDF and {needed.why}. A vision "
                f"model (a cloud API key, or a local model such as Ollama) would read it more faithfully; without "
                f"one, {without}. Nothing was written. Run `vision-status` to see keys and local models found on "
                "this machine, then `vision-set` or `vision-set --skip`, then re-run.")
            return None, 3
        pages = sorted({str(f["page"]) for f in needed.figures if f["page"]}) or ["?"]
        say(f"VISION CHOICE NEEDED: {len(needed.figures)} large image(s) without text (pages {', '.join(pages)}). "
            "A vision model (a cloud API key, or a local model such as Ollama) would describe them; without one, "
            "they are exported as image files next to the Markdown. Nothing was written. Run `vision-status` to "
            "see keys and local models found on this machine, then `vision-set` or `vision-set --skip`, then re-run.")
        return None, 3
    except KeyMissing as missing:
        say(f"API KEY MISSING: environment variable {missing} is not set. Set it, or run `vision-set --skip`.")
        return None, 4
    except PageImagesNeeded as needed:
        say(f"PAGE IMAGES NEEDED {needed.src.name}: no text layer and Docling OCR {needed.reason}. "
            f"Rendered {needed.count} page image(s) to {needed.pages_dir}. Nothing was logged. Read every image, "
            f"write the Markdown to {needed.output}, then run: record \"{needed.src}\"")
        return None, 5
    except Conflict as conflict:
        say(f"CONFLICT {conflict}")
        return None, 1
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as error:
        say(f"ERROR {error}")
        return None, 1


def cmd_get(args):
    src = Path(args.path).resolve()
    if not src.is_file():
        say(f"ERROR no such file: {src}")
        return 1
    if src.suffix.lower() in NATIVE:
        say(f"NATIVE {src.name}: already text, read it directly")
        print(src)
        return 0
    if src.suffix.lower() not in SUPPORTED:
        say(f"ERROR unsupported format {src.suffix}; ask for a text or PDF export")
        return 1
    root = Path(args.root).resolve()
    result, code = handle_run(convert, src, out_dir_for(src.parent, args), log_path_for(src.parent, args.log), root, args)
    if code:
        return code
    _, output = result
    print(output.read_text(encoding="utf-8") if args.print else output)
    return 0


def cmd_sync(args):
    folder = Path(args.folder).resolve()
    root = Path(args.root).resolve()
    failures = pending = 0
    for src in documents_in(folder, args.recursive):
        result, code = handle_run(convert, src, out_dir_for(src.parent, args), log_path_for(src.parent, args.log), root, args)
        if code in (3, 4):
            return code
        pending += code == 5
        failures += code not in (0, 5)
    return 1 if failures else (5 if pending else 0)


def cmd_record(args):
    """Log a Markdown copy written by hand (from page images) so later requests return it as a HIT."""
    src = Path(args.path).resolve()
    if not src.is_file():
        say(f"ERROR no such file: {src}")
        return 1
    root = Path(args.root).resolve()
    out_dir = out_dir_for(src.parent, args)
    output = Path(args.md).resolve() if args.md else out_dir / f"{src.stem}.md"
    if not output.is_file() or not output.read_text(encoding="utf-8").strip():
        say(f"ERROR write the Markdown to {output} first")
        return 1
    log_file = log_path_for(src.parent, args.log)
    log = load_log(log_file)
    append_log(log, log_file, src, sha256_file(src), output, root, "transcribed-from-page-images",
               "image-only PDF, transcribed from page images")
    write_index(output.parent, log, root)
    pages_dir = output.parent / f"{src.stem}_pages"
    if pages_dir.is_dir():
        shutil.rmtree(pages_dir)
        say(f"removed {rel(pages_dir, root)}")
    say(f"RECORDED {src.name} -> {rel(output, root)}")
    print(output)
    return 0


def cmd_watch(args):
    folder = Path(args.folder).resolve()
    root = Path(args.root).resolve()
    args.unattended = True  # nobody to ask: undecided figures are exported
    seen, handled = {}, {}
    say(f"watching {folder}")
    while True:
        now = time.monotonic()
        for src in documents_in(folder, False):
            try:
                stat = src.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            if handled.get(src) == signature:
                continue
            first = seen.get(src)
            if not first or first[0] != signature:
                seen[src] = (signature, now)
                continue
            if now - first[1] < 4:
                continue
            _, code = handle_run(convert, src, out_dir_for(src.parent, args), log_path_for(src.parent, args.log), root, args)
            handled[src] = signature if code in (0, 5) else (-1, -1)  # 5: page images wait for a human
        time.sleep(2)


def cmd_list(args):
    folder = Path(args.folder).resolve()
    log = load_log(log_path_for(folder, args.log))
    latest = {}
    for entry in log["entries"]:
        latest[entry["source"]] = entry
    for entry in latest.values():
        print(f"{entry['converted_at'][:19]}  {entry['source']} -> {entry['output']}  {entry.get('figures', '')}")
    return 0


def cmd_doctor(_args):
    """Check that Docling, OCR and page rendering work; print the fix for anything that does not."""
    problems = 0
    version = None
    if shutil.which("docling"):
        done = subprocess.run(["docling", "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        match = re.search(r"Docling version:\s*(\d+)\.(\d+)", done.stdout)
        version = (int(match.group(1)), int(match.group(2))) if match else None
        say(f"ok      docling {'.'.join(map(str, version)) if version else '(version unknown)'}")
    elif shutil.which("uvx"):
        say("warn    docling not installed as a tool; falling back to uvx (slow). Fix: " + OCR_FIX)
    else:
        say("missing docling. Fix: install uv, then " + OCR_FIX)
        return 1
    python = docling_python()
    if python is None:
        say("missing Docling's Python environment (needed for OCR checks and page images). Fix: " + OCR_FIX)
        return 1
    for name, script in (("ocr", OCR_CHECK_SCRIPT), ("render", "import pypdfium2; print('ok')")):
        done = subprocess.run(python + ["-I", "-c", script], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if done.returncode == 0:
            say(f"ok      {name}")
        else:
            problems += 1
            detail = (done.stderr or done.stdout).strip().splitlines()[-1:]
            say(f"missing {name} ({' '.join(detail)}). Fix: {OCR_FIX}")
    if version and version < OCR_MIN_DOCLING:
        problems += 1
        say(f"old     docling {version[0]}.{version[1]} cannot use current rapidocr; needs "
            f"{OCR_MIN_DOCLING[0]}.{OCR_MIN_DOCLING[1]}+. Fix: {OCR_FIX}")
    say("doctor: all good" if not problems else f"doctor: {problems} problem(s); image-only PDFs will fall back to page images")
    return 1 if problems else 0


def cmd_vision_status(_args):
    print(json.dumps(vision_status(), indent=2))
    return 0


def cmd_vision_set(args):
    config = load_config()
    if args.skip:
        config["vision"] = {"mode": "skip"}
    else:
        preset = PRESETS.get(args.provider or "")
        base_url = args.base_url or (preset[0] if preset else None)
        if not base_url or not args.model:
            say("ERROR vision-set needs --model and either a known --provider or --base-url")
            return 1
        key_env = args.key_env
        if key_env is None and preset:
            names = preset[1]
            key_env = next((n for n in names if os.environ.get(n)), names[0] if names else None)
        config["vision"] = {"mode": "api", "provider": args.provider or "custom", "base_url": base_url,
                            "model": args.model, "key_env": key_env}
        if args.auth_header:
            config["vision"]["auth_header"] = args.auth_header
    save_config(config)
    print(json.dumps(vision_status(), indent=2))
    if not args.skip:
        say("saved; run `vision-test` to check that the model can read an image")
    return 0


def cmd_vision_test(_args):
    """Send a small built-in image (no document content) and check that the model reads it back."""
    vision = load_config().get("vision")
    if not vision or vision.get("mode") != "api":
        say("ERROR no vision API configured; run `vision-set` first")
        return 1
    probe = Path(tempfile.mkdtemp(prefix="dlv")) / "probe.png"
    probe.write_bytes(base64.b64decode(PROBE_PNG))
    started = time.monotonic()
    try:
        reply = describe_image(vision, probe, "Copy the text in this image exactly. Output only the text.")
    except KeyMissing as missing:
        say(f"API KEY MISSING: environment variable {missing} is not set")
        return 4
    except urllib.error.HTTPError as error:
        say(f"FAILED HTTP {error.code}: {error.read()[:200].decode('utf-8', 'replace')}")
        return 1
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as error:
        say(f"FAILED {error}")
        return 1
    finally:
        shutil.rmtree(probe.parent, ignore_errors=True)
    ok = PROBE_TEXT in reply
    say(f"{'OK' if ok else 'FAILED'} {vision.get('provider')} / {vision.get('model')} replied {reply[:60]!r} "
        f"in {time.monotonic() - started:.1f}s" + ("" if ok else f"; expected {PROBE_TEXT!r}. The model may not "
                                                       "accept images"))
    return 0 if ok else 1


def cmd_vision_reset(_args):
    config = load_config()
    config.pop("vision", None)
    save_config(config)
    say("vision configuration cleared")
    return 0


def add_convert_options(parser):
    parser.add_argument("--out", help="output folder (default: <source folder>/extracted)")
    parser.add_argument("--log", help="log file (default: <source folder>/.doc-to-markdown-log.json)")
    parser.add_argument("--root", default=os.getcwd(), help="paths in the log are relative to this (default: cwd)")
    parser.add_argument("--force", action="store_true", help="reconvert even if the source is unchanged")
    parser.add_argument("--overwrite", action="store_true", help="replace an output that is not in the log")
    parser.add_argument("--vision", choices=["auto", "skip", "api"], default="auto",
                        help="auto: use saved config; skip: export large images to an assets folder")
    parser.add_argument("--force-figure-pages", type=lambda v: [int(x) for x in v.split(",") if x], default=[],
                        help="comma-separated pages whose text-bearing graphics should also be treated as images")
    parser.set_defaults(unattended=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    get = commands.add_parser("get", help="convert one document if needed; print the Markdown path")
    get.add_argument("path")
    get.add_argument("--print", action="store_true", help="print the Markdown instead of its path")
    add_convert_options(get)
    get.set_defaults(run=cmd_get)

    sync = commands.add_parser("sync", help="convert every supported document in a folder")
    sync.add_argument("folder")
    sync.add_argument("-r", "--recursive", action="store_true")
    add_convert_options(sync)
    sync.set_defaults(run=cmd_sync)

    watch = commands.add_parser("watch", help="poll a folder and convert new or changed documents")
    watch.add_argument("folder")
    add_convert_options(watch)
    watch.set_defaults(run=cmd_watch)

    record = commands.add_parser("record", help="log a Markdown copy transcribed from page images")
    record.add_argument("path")
    record.add_argument("--md", help="the Markdown file (default: <out>/<stem>.md)")
    record.add_argument("--out", help="output folder (default: <source folder>/extracted)")
    record.add_argument("--log")
    record.add_argument("--root", default=os.getcwd())
    record.set_defaults(run=cmd_record)

    listing = commands.add_parser("list", help="show what has been converted")
    listing.add_argument("folder")
    listing.add_argument("--log")
    listing.set_defaults(run=cmd_list)

    commands.add_parser("doctor", help="check Docling, OCR and page rendering; print fixes").set_defaults(run=cmd_doctor)
    commands.add_parser("vision-status", help="show vision settings and which API key variables are set").set_defaults(run=cmd_vision_status)
    setter = commands.add_parser("vision-set", help="save the vision API choice (never the key)")
    setter.add_argument("--provider", help=", ".join(PRESETS) + ", or omit with --base-url")
    setter.add_argument("--base-url")
    setter.add_argument("--model")
    setter.add_argument("--key-env", help="name of the environment variable holding the key")
    setter.add_argument("--auth-header", help="header for the key if not 'Authorization: Bearer' (e.g. api-key)")
    setter.add_argument("--skip", action="store_true", help="never use an API; export large images to assets")
    setter.set_defaults(run=cmd_vision_set)
    commands.add_parser("vision-reset", help="forget the saved vision choice").set_defaults(run=cmd_vision_reset)
    commands.add_parser("vision-test", help="check the saved vision model with a built-in test image").set_defaults(run=cmd_vision_test)

    args = parser.parse_args()
    try:
        sys.exit(args.run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
