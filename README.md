# document-to-markdown

An agent skill that converts PDF, Word, PowerPoint, Excel and HTML files into **self-contained Markdown, once**, and hands your AI agent the saved copy every time after that.

Built for [Claude Code](https://claude.com/claude-code) and any agent that supports the `SKILL.md` format. Conversion runs locally with [Docling](https://github.com/docling-project/docling).

## Why

Every time an agent reads a PDF or Office file directly, you pay for it again: the file is re-parsed, pages may be sent as images, and the same content fills the context window on every new session. Agents also tend to improvise (`pdftotext` here, `pandoc` there), which gives different, often broken, output each time.

This skill makes document reading **convert-once, read-many**:

- **Saves tokens.** The agent reads plain Markdown text instead of the raw document, and can `grep` or read line ranges instead of loading the whole file.
- **Saves time.** Each file is fingerprinted (SHA-256). If it hasn't changed, the cached Markdown path comes back instantly (`HIT`) and nothing is reconverted.
- **Keeps what matters.** Text hidden inside vector graphics (timelines, cards, badges) is recovered, small decorative images are dropped, and charts or photos are either described by a vision model you choose or exported next to the Markdown. An important image is never silently lost.
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
3. **Large images with no text** (charts, photos, diagrams) are described by a vision API if you set one up, or exported to `<name>_assets/` and linked from the Markdown.

## Requirements

- Python 3.9 or newer (the script uses only the standard library)
- [uv](https://docs.astral.sh/uv/) and Docling, installed once per machine:

```bash
uv tool install docling
```

Docling is large: about 1.1 GB, plus about 500 MB of models downloaded to `~/.cache/huggingface` on first use. If it isn't installed, the script falls back to `uvx --from docling`, which works but starts more slowly.

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
| `--vision auto\|skip\|api` | How to handle large text-free images for this run |

Status lines go to stderr and start with `doc_to_md:`:

| Status | Meaning |
|---|---|
| `HIT` | Source unchanged; nothing converted |
| `CONVERTED` / `RECONVERTED` / `FORCED` | New file, changed file, or forced rerun |
| `NATIVE` | Already text; read it directly |
| `REVIEW` / `WARNING` | Worth a look: a large graphic was kept as text, or an image couldn't be extracted |
| `CONFLICT` | An output file exists that the log doesn't know about; nothing was overwritten |

Exit codes: `0` ok, `1` error, `3` vision choice needed, `4` vision API key missing.

## Optional: describing charts and photos with a vision model

Only needed if your documents contain large images without text. The first time one turns up, the agent asks you which provider and model to use, or whether to skip. Any OpenAI-compatible `/chat/completions` endpoint that accepts images works. Presets: `openai`, `gemini`, `openrouter`, `mistral`, `anthropic`, `ollama` (local), or any `--base-url`.

```bash
python scripts/doc_to_md.py vision-status
python scripts/doc_to_md.py vision-set --provider openai --model <model-name>
python scripts/doc_to_md.py vision-set --provider ollama --model <local-model>
python scripts/doc_to_md.py vision-set --skip      # never call an API; export images instead
python scripts/doc_to_md.py vision-reset           # forget the choice
```

**Your API key is never stored.** Only the provider, URL, model and the *name* of the environment variable (such as `OPENAI_API_KEY`) are saved, in `~/.config/doc-to-markdown/config.json`. Set the key itself in your shell environment.

**Privacy:** with a cloud vision API configured, large images from your documents are sent to that provider. Use `ollama` or `--skip` for confidential material. Without a vision API, everything runs locally.

## Files it creates

```
your-folder/
├── report.pdf
├── .doc-to-markdown-log.json      # fingerprint log (source of truth for HIT / reconvert)
└── extracted/
    ├── INDEX.md                   # list of converted documents
    ├── report.md
    └── report_assets/             # only if images were exported
```

Add `extracted/` and `.doc-to-markdown-log.json` to `.gitignore` if you don't want converted copies in version control, or commit them so teammates don't have to reconvert.

## Limitations

- Docling is a large install, and the first run is slow while models download. After that, a 16-page PDF takes about 20 seconds.
- Diagrams kept as text lose their arrows and layout. Use `--force-figure-pages` when structure matters.
- Vision descriptions can vary between runs and can be wrong. Check key numbers against the source.
- An exported `_assets/` folder makes the output multi-file. Move it together with its Markdown.
- Needs a local Python environment with shell access, such as Claude Code or another coding agent.

## Contributing

Issues and pull requests are welcome. Please include a sample document (or a description of its layout) when reporting a bad conversion.

## License

[MIT](LICENSE) © Sreenath H B
