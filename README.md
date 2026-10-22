# document-to-markdown

An agent skill that converts PDF, Word, PowerPoint, Excel and HTML files into **self-contained Markdown, once**, and hands your AI agent the saved copy every time after that.

Built for [Claude Code](https://claude.com/claude-code) and any agent that supports the `SKILL.md` format. Conversion runs locally with [Docling](https://github.com/docling-project/docling).

## Why

Every time an agent reads a PDF or Office file directly, you pay for it again: the file is re-parsed, pages may be sent as images, and the same content fills the context window on every new session. Agents also tend to improvise (`pdftotext` here, `pandoc` there), which gives different, often broken, output each time.

This skill makes document reading **convert-once, read-many**:

- **Saves tokens.** The agent reads plain Markdown text instead of the raw document, and can `grep` or read line ranges instead of loading the whole file.
- **Saves time.** Each file is fingerprinted (SHA-256). If it hasn't changed, the cached Markdown path comes back instantly (`HIT`) and nothing is reconverted.
- **Keeps what matters.** Text hidden inside vector graphics (timelines, cards, badges) is recovered, small decorative images are dropped, and charts or photos are either described by a vision model you choose or exported next to the Markdown. An important image is never silently lost.
- **Handles scanned and image-only PDFs.** PDFs whose pages are pictures are read with local OCR when the layout is simple, and with a vision model (local or cloud, your choice) when side-by-side columns would trip OCR up.
- **Stays consistent.** One converter, one output folder, one log, so every session and every teammate sees the same Markdown.

## What it does

```
report.pdf  ──►  doc_to_md.py get report.pdf  ──►  extracted/report.md
                         │
                         └─ logged in .doc-to-markdown-log.json (path + SHA-256)

next time:  doc_to_md.py get report.pdf  ──►  HIT → extracted/report.md (no conversion)
```

| Input | Result |
|---|---|
| `.pdf` `.docx` `.pptx` `.xlsx` `.html` `.htm` | Converted to Markdown in `<source folder>/extracted/`, with a generated `INDEX.md` |
| `.md` `.txt` `.csv` `.json` | Reported as `NATIVE`; the agent reads them directly |
| Changed source file | Reconverted automatically (`RECONVERTED`) |
| Unchanged source file | Cached copy returned (`HIT`) |

How images are handled:

1. **Small images** (icons, logos, avatars; under 10% of the page) are removed. Any text in them is kept inline.
2. **Graphics that contain text** (timelines, card rows, number badges) are kept as text.
3. **Large images with no text** (charts, photos, diagrams) are described by a vision model if you set one up, or exported to `<name>_assets/` and linked from the Markdown.

### Image-only PDFs

Some PDFs have no text layer: every page is a picture, from a scanner or an exporter that rasterizes pages. Your PDF viewer may still let you select text, because it runs its own OCR. The script checks for fonts, and when there are none:

| Page layout | With a vision model | Without one |
|---|---|---|
| Single column (tables are fine) | Local OCR | Local OCR |
| Side-by-side blocks (two-column boxes, card rows) | Vision model transcribes the pages | Local OCR, flagged for a deeper review |
| OCR finds no text | Vision model transcribes the pages | Page images for your agent to transcribe, then `record` |

OCR is local and fast, but on side-by-side blocks it often merges or reorders the columns. A vision model reads the page as a whole. If none is configured, the agent asks you before falling back to OCR.

## Requirements

- Python 3.9 or newer (the script uses only the standard library)
- [uv](https://docs.astral.sh/uv/) and Docling 2.141 or newer with the RapidOCR extra, installed once per machine:

```bash
uv tool install --force --upgrade "docling[rapidocr]"
```

Docling is large: about 1.1 GB, plus about 500 MB of models downloaded to `~/.cache/huggingface` on first use. If it isn't installed, the script falls back to `uvx --from docling`, which works but starts more slowly. The `rapidocr` extra adds local OCR for image-only PDFs. Older Docling versions break on current rapidocr and report "No OCR engine found".

Check your setup at any time:

```bash
python scripts/doc_to_md.py doctor
```

## Installation

### Claude Code (all your projects)

```bash
git clone https://github.com/ubshreenath/document-to-markdown.git ~/.claude/skills/document-to-markdown
```

On Windows (PowerShell):

```powershell
git clone https://github.com/ubshreenath/document-to-markdown.git "$HOME\.claude\skills\document-to-markdown"
```

### Claude Code (one project only)

```bash
git clone https://github.com/ubshreenath/document-to-markdown.git .claude/skills/document-to-markdown
```

### Other agents

Clone into whichever skills folder your agent reads, for example `~/.agents/skills/document-to-markdown`. The skill is just `SKILL.md` plus `scripts/doc_to_md.py`.

Claude Code picks up the skill on the next session. Check it's there by asking Claude "what skills do you have?" or by typing `/document-to-markdown`.

## Add this to your instructions

Skills are triggered by their description, but agents still sometimes open a PDF directly out of habit. Add the snippet below so the skill is used **every time**, which is where the savings come from.

**Where to put it:**

- **For all your projects:** `~/.claude/CLAUDE.md` (Claude Code user instructions)
- **For one project:** `CLAUDE.md` in the project root (commit it so your team gets it too)
- **Other agents:** `AGENTS.md`, or your tool's equivalent rules or custom-instructions file

```markdown
## Reading documents

- Never read a PDF, Word, PowerPoint, Excel or HTML file directly, and never run
  pdftotext, pandoc or another converter on it yourself. Always use the
  `document-to-markdown` skill first:
  `python <skill-dir>/scripts/doc_to_md.py get "<path>"`
  then read the Markdown file whose path it prints.
- If the result is `HIT`, the saved Markdown is current. Use it; do not reconvert.
- For long Markdown files, use grep or read line ranges instead of the whole file.
- Only rerun a conversion (`--force`) when I ask for it.
- Never overwrite an existing Markdown file that isn't in the conversion log
  (`CONFLICT`) without asking me.
```

Replace `<skill-dir>` with the install path, for example `~/.claude/skills/document-to-markdown`.

**Optional, for a folder you drop files into:**

```markdown
- Documents I drop into `dump/` should be converted with
  `doc_to_md.py sync dump/` before you work with them.
```

## Usage

Most of the time you don't run anything: ask your agent about a document and it uses the skill. You can also run the script yourself:

```bash
# Convert one file (or return the cached copy) and print the Markdown path
python scripts/doc_to_md.py get "contracts/agreement.pdf"

# Convert every supported file in a folder (add -r for subfolders)
python scripts/doc_to_md.py sync dump/ -r

# Keep converting new or changed files as they appear
python scripts/doc_to_md.py watch dump/

# Show what has been converted
python scripts/doc_to_md.py list dump/

# Log a Markdown copy your agent transcribed from page images (after exit code 5)
python scripts/doc_to_md.py record "scans/letter.pdf"

# Check Docling, OCR and page rendering, with fix commands
python scripts/doc_to_md.py doctor
```

Options for `get`, `sync` and `watch` (put them after the command, e.g. `doc_to_md.py get file.pdf --force`):

| Option | What it does |
|---|---|
| `--force` | Reconvert even if the source hasn't changed |
| `--overwrite` | Replace an existing Markdown file that isn't in the log |
| `--out DIR` | Output folder (default `<source folder>/extracted`) |
| `--log FILE` | Log file (default `<source folder>/.doc-to-markdown-log.json`) |
| `--root DIR` | Paths in the log are stored relative to this (default: current folder) |
| `--force-figure-pages 3,7` | Also describe or export the graphics on these pages (for flowcharts whose arrows and layout matter) |
| `--vision auto\|skip\|api` | Whether this run may use the vision model (`skip` keeps a confidential document fully local) |

Status lines go to stderr and start with `doc_to_md:`:

| Status | Meaning |
|---|---|
| `HIT` | Source unchanged; nothing converted |
| `CONVERTED` / `RECONVERTED` / `FORCED` | New file, changed file, or forced rerun |
| `NATIVE` | Already text; read it directly |
| `REVIEW` / `WARNING` | Worth a look: a large graphic was kept as text, an image couldn't be extracted, or OCR text needs review |
| `IMAGE-ONLY` | The PDF has no text layer; OCR or a vision model reads it |
| `VISION CHOICE NEEDED` | A vision model would do better here; the agent asks you |
| `PAGE IMAGES NEEDED` | No text and no vision model; page images are left for the agent to transcribe |
| `RECORDED` | A hand transcription was logged |
| `CONFLICT` | An output file exists that the log doesn't know about; nothing was overwritten |

Exit codes: `0` ok, `1` error, `3` vision choice needed, `4` vision API key missing, `5` page images need transcribing.

## Optional: a vision model for charts, photos and image-only PDFs

A vision model reads images: charts and photos without text, and the pages of image-only PDFs whose layout OCR would mangle. The first time one would help, your agent asks whether you want to use one. It explains what you'd get without one, and lists what it found on your machine: API keys already set (names only) and local model servers such as Ollama or LM Studio.

**Keep documents local** with a model on your own machine:

| Local server | Preset | Default address |
|---|---|---|
| [Ollama](https://ollama.com) | `ollama` | `localhost:11434` |
| [LM Studio](https://lmstudio.ai) | `lmstudio` | `localhost:1234` |
| llama.cpp server | `llamacpp` | `localhost:8080` |
| vLLM | `vllm` | `localhost:8000` |
| Jan | `jan` | `localhost:1337` |

**Or use a cloud API** of your choice. Presets: `anthropic`, `openai`, `gemini`, `mistral`, `kimi`, `openrouter`, `groq`, `together`, `xai`. Any other OpenAI-compatible `/chat/completions` endpoint that accepts images works with `--base-url`.

```bash
python scripts/doc_to_md.py vision-status                       # keys and local models found on this machine
python scripts/doc_to_md.py vision-set --provider ollama --model <local-vision-model>
python scripts/doc_to_md.py vision-set --provider gemini --model <model-name>
python scripts/doc_to_md.py vision-set --base-url https://host/v1 --key-env MY_KEY --model <model-name>
python scripts/doc_to_md.py vision-test                         # checks the model reads a built-in test image
python scripts/doc_to_md.py vision-set --skip                   # never use a vision model
python scripts/doc_to_md.py vision-reset                        # forget the choice
```

For an endpoint that takes the key in a different header, add `--auth-header` (for example `--auth-header api-key` for Azure OpenAI, with `?api-version=...` in the `--base-url`).

**Your API key is never stored.** Only the provider, URL, model and the *name* of the environment variable (such as `OPENAI_API_KEY`) are saved, in `~/.config/doc-to-markdown/config.json`. Set the key itself in your environment. `vision-test` sends only a built-in image, never your documents.

**Privacy:** with a cloud vision API configured, images from your documents (figures, and pages of image-only PDFs) are sent to that provider. For confidential material, use a local model, `vision-set --skip`, or `--vision skip` on a single run. Without a cloud API, everything runs locally.

## Files it creates

```
your-folder/
├── report.pdf
├── .doc-to-markdown-log.json      # fingerprint log (source of truth for HIT / reconvert)
└── extracted/
    ├── INDEX.md                   # list of converted documents
    ├── report.md
    ├── report_assets/             # only if images were exported
    └── scan_pages/                # only while an image-only PDF waits for transcription
```

Add `extracted/` and `.doc-to-markdown-log.json` to `.gitignore` if you don't want converted copies in version control, or commit them so teammates don't have to reconvert.

## Limitations

- Docling is a large install, and the first run is slow while models download. After that, a 16-page PDF takes about 20 seconds.
- Diagrams kept as text lose their arrows and layout. Use `--force-figure-pages` when structure matters.
- Vision descriptions and transcriptions can vary between runs and can be wrong. Check key numbers against the source.
- Vision transcription of image-only PDFs takes about 30 to 60 seconds a page on a cloud API (slower on a local model without a GPU), and the provider charges per page.
- The layout check that picks OCR or a vision model looks for side-by-side text blocks. It is a heuristic, and won't catch every layout OCR gets wrong.
- An exported `_assets/` folder makes the output multi-file. Move it together with its Markdown.
- Needs a local Python environment with shell access, such as Claude Code or another coding agent.

## Contributing

Issues and pull requests are welcome. Please include a sample document (or a description of its layout) when reporting a bad conversion.

## License

[MIT](LICENSE) © Sreenath H B
