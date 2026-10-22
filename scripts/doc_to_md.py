#!/usr/bin/env python3
"""Convert documents to self-contained Markdown with Docling and keep a fingerprint log.

Standard library only; Docling is called as a command (`docling`, or `uvx --from docling docling`).
Exit codes: 0 ok, 1 error, 3 vision choice needed, 4 vision API key missing.
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
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# Any OpenAI-compatible /chat/completions endpoint works; these are only shortcuts.
PRESETS = {
    "openai": ("https://api.openai.com/v1", ["OPENAI_API_KEY"]),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ["GEMINI_API_KEY", "GOOGLE_API_KEY"]),
    "openrouter": ("https://openrouter.ai/api/v1", ["OPENROUTER_API_KEY"]),
    "mistral": ("https://api.mistral.ai/v1", ["MISTRAL_API_KEY"]),
    "anthropic": ("https://api.anthropic.com/v1", ["ANTHROPIC_API_KEY"]),
    "ollama": ("http://localhost:11434/v1", []),
}
VISION_PROMPT = (
    "This image comes from a document. Describe it so a reader who cannot see it gets all of its "
    "information: the kind of figure, every label and number, and how the parts relate. Copy visible "
    "text verbatim. For a chart or table, give the data as a Markdown table. Do not speculate or add "
    "interpretation. If the image is only decoration (a logo, icon, divider or background) reply with "
    "exactly: DECORATIVE"
)


class NeedsVision(Exception):
    def __init__(self, figures):
        self.figures = figures


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


def vision_status():
    config = load_config().get("vision")
    known = sorted({name for _, names in PRESETS.values() for name in names})
    status = {
        "configured": bool(config),
        "config": config,
        "key_present": None,
        "env_vars_present": [name for name in known if os.environ.get(name)],
        "presets": {name: {"base_url": url, "key_env_candidates": names} for name, (url, names) in PRESETS.items()},
    }
    if config and config.get("mode") == "api":
        status["key_present"] = (not config.get("key_env")) or bool(os.environ.get(config["key_env"]))
    return status


def describe_image(vision, image_path):
    key = ""
    if vision.get("key_env"):
        key = os.environ.get(vision["key_env"], "")
        if not key:
            raise KeyMissing(vision["key_env"])
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = json.dumps({
        "model": vision["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]}],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(vision["base_url"].rstrip("/") + "/chat/completions", body, headers)
    with urllib.request.urlopen(request, timeout=VISION_TIMEOUT_SECONDS) as response:
        reply = json.load(response)
    content = reply["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content.strip()


# ---------------------------------------------------------------- Docling

def run_docling(src, work_dir):
    arguments = [str(src), "--to", "md", "--to", "json", "--image-export-mode", "referenced",
                 "--output", str(work_dir)]
    error = None
    for command in (["docling"], ["uvx", "--from", "docling", "docling"]):
        if shutil.which(command[0]) is None:
            continue
        done = subprocess.run(command + arguments, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CONVERT_TIMEOUT_SECONDS)
        if done.returncode == 0:
            return
        error = (done.stderr or done.stdout).strip().splitlines()[-1:]
    if error is None:
        raise RuntimeError("Docling not found; install it once per machine with: uv tool install docling")
    raise RuntimeError("Docling failed: " + " ".join(error))


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

    work = Path(tempfile.mkdtemp(prefix="dl"))  # short path: Docling writes long image names
    try:
        run_docling(src, work)
        md_files, json_files = list(work.glob("*.md")), list(work.glob("*.json"))
        if len(md_files) != 1 or len(json_files) != 1:
            raise RuntimeError("Docling did not produce exactly one Markdown and one JSON file")
        markdown = md_files[0].read_text(encoding="utf-8")
        if not markdown.strip():
            raise RuntimeError("Docling returned no Markdown text")
        document = json.loads(json_files[0].read_text(encoding="utf-8"))
        artifacts = next((p for p in work.iterdir() if p.is_dir() and p.name.endswith("_artifacts")), None)
        figures = analyse_pictures(document, artifacts)

        force_pages = set(options.force_figure_pages)
        needing = [f for f in figures
                   if (f["large"] and not (f["lines"] or f["overlapping"])) or f["page"] in force_pages]
        vision = resolve_vision(options, needing)
        report = {"warnings": [], "text_graphics": [], "inline_text": 0, "decorative": 0,
                  "described": 0, "exported": 0}
        markdown, staged = build_markdown(markdown, figures, src.stem, force_pages, vision, report)
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

    summary = (f"{report['described']} described, {report['exported']} exported, "
               f"{len(report['text_graphics'])} text graphic(s), {report['decorative']} decorative dropped")
    log["version"] = 1
    log["entries"].append({"source": rel(src, root), "sha256": digest, "output": rel(output, root),
                           "converted_at": datetime.now(timezone.utc).isoformat(),
                           "converter": "docling", "figures": summary})
    atomic_write(log_file, json.dumps(log, indent=2) + "\n")
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
        pages = sorted({str(f["page"]) for f in needed.figures if f["page"]}) or ["?"]
        say(f"VISION CHOICE NEEDED: {len(needed.figures)} large image(s) (pages {', '.join(pages)}). "
            "Nothing was written. Configure with `vision-set` (API) or `vision-set --skip` (export to an "
            "assets folder), then re-run.")
        return None, 3
    except KeyMissing as missing:
        say(f"API KEY MISSING: environment variable {missing} is not set. Set it, or run `vision-set --skip`.")
        return None, 4
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
    failures = 0
    for src in documents_in(folder, args.recursive):
        result, code = handle_run(convert, src, out_dir_for(src.parent, args), log_path_for(src.parent, args.log), root, args)
        if code in (3, 4):
            return code
        failures += bool(code)
    return 1 if failures else 0


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
            handled[src] = signature if code == 0 else (-1, -1)
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
    save_config(config)
    print(json.dumps(vision_status(), indent=2))
    return 0


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

    listing = commands.add_parser("list", help="show what has been converted")
    listing.add_argument("folder")
    listing.add_argument("--log")
    listing.set_defaults(run=cmd_list)

    commands.add_parser("vision-status", help="show vision settings and which API key variables are set").set_defaults(run=cmd_vision_status)
    setter = commands.add_parser("vision-set", help="save the vision API choice (never the key)")
    setter.add_argument("--provider", help=", ".join(PRESETS) + ", or omit with --base-url")
    setter.add_argument("--base-url")
    setter.add_argument("--model")
    setter.add_argument("--key-env", help="name of the environment variable holding the key")
    setter.add_argument("--skip", action="store_true", help="never use an API; export large images to assets")
    setter.set_defaults(run=cmd_vision_set)
    commands.add_parser("vision-reset", help="forget the saved vision choice").set_defaults(run=cmd_vision_reset)

    args = parser.parse_args()
    try:
        sys.exit(args.run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
