---
name: document-to-markdown
description: 'Convert any document (PDF, Word, PowerPoint, Excel, HTML) to self-contained Markdown with Docling, and reuse the converted copy instead of re-reading the original. Use before reading a PDF or Office file, when processing files in dump/, checking conversion status, or explicitly rerunning a conversion.'
---

# Document to Markdown

Converts each source document once into a self-contained Markdown file, records it in a fingerprint log, and returns the saved copy on every later request. 

## The rule

Never run `pdftotext`, `pandoc`, or another converter directly on a project document, and do not read a PDF or Office file raw when a Markdown copy can exist. Run:

```
python <skill-dir>/scripts/doc_to_md.py get "<path-to-document>"
```

`<skill-dir>` is the folder that holds this `SKILL.md` (for example `~/.agents/skills/document-to-markdown`). It prints the path of the Markdown copy; read that file (use `grep` or line ranges for long ones, `--print` only for short ones). Status lines go to stderr and start with `doc_to_md:`.

| Status | Meaning |
|---|---|
| HIT | Source unchanged since the last conversion. Nothing was converted. |
| CONVERTED / RECONVERTED / FORCED | New source, changed source, or explicit rerun. |
| NATIVE | Already text (md, txt, csv, json). Read it directly. |
| REVIEW / WARNING | Read these, see "After a conversion". |

`sync <folder> [-r]` converts everything in a folder; `watch <folder>` polls a folder; `list <folder>` shows what was converted. Defaults: output in `<source folder>/extracted/` (with a generated `INDEX.md`), log in `<source folder>/.doc-to-markdown-log.json`. 

## Requirements

Python 3.9+ and Docling, installed once per machine and shared by every project: `uv tool install docling` (about 1.1 GB plus about 500 MB of models cached in `~/.cache/huggingface`). Without it the script falls back to `uvx --from docling`, which is slower to start. No other packages are needed.

## What "self-contained" means

The Markdown file stands alone. Images are handled by size and content:

1. **Small images (icons, logos, badges, avatars; under 10% of the page):** removed. Any text inside them stays, inline.
2. **Graphics that contain text** (timelines, card rows, number badges): kept as text, not as images. Docling often labels vector graphics as "pictures" and hides their text; the script recovers it from the JSON.
3. **Large images with no text** (charts, photos, diagrams): described by the vision API if one is configured, otherwise exported to `<stem>_assets/` and linked, with a note at the top of the Markdown naming the folder. A missing important image is worse than an assets folder, so an image is never replaced by a "see the original" placeholder.

If the API call fails for a figure, that figure is exported instead, so it is not lost.

## Vision API setup (provider-agnostic)

Only asked when a document actually has a large text-free image. The script then exits with code 3 and writes nothing, until a choice exists. Any OpenAI-compatible `/chat/completions` endpoint that accepts images works; presets are only shortcuts (`openai`, `gemini`, `openrouter`, `mistral`, `anthropic`, `ollama`, or any `--base-url`).

1. Run `doc_to_md.py vision-status`. If `configured` is true, you are done.
2. Otherwise ask the user which API to use (or to skip), and which **model** (do not guess a model name; they choose it).
3. Run `vision-set --provider <name> --model <model> [--base-url URL] [--key-env NAME]`. Only the provider, URL, model and the *name* of the key variable are saved (in `~/.config/doc-to-markdown/config.json`), never the key.
4. Find the key in the environment: `vision-status` lists which known key variables are set (names only). If the chosen variable is not set (exit code 4), ask the user to set it in their shell profile or environment. If they paste a key in chat, use it only as a per-command environment variable (`NAME=... python ...`) and never write it to a file, log, or Markdown.
5. If the user declines or has no key, run `vision-set --skip`: large images are exported to an assets folder instead. `vision-reset` forgets the choice.

`watch` has nobody to ask, so undecided large images are exported to assets for that run.

Privacy: with an API configured, large images leave the machine and go to that provider. Check this is acceptable for confidential documents.

## After a conversion

1. **Check that an image is really an image.** A "picture" is not trustworthy until verified: Docling also labels vector graphics (cards, timelines, number badges) as pictures and can swallow their text. For every `REVIEW` line, and for any figure that looks suspicious, open the source page and compare. If the page has selectable text where the Markdown shows a figure or a gap, it is text. Compare each page's text against the Markdown (for example with PyMuPDF) before trusting the result.
2. **Check diagrams that were kept as text.** A `REVIEW` line means a large graphic was kept as extracted text. If arrows, layout, or grouping carry meaning (flowcharts, architecture diagrams), re-run with `--force-figure-pages <page>[,<page>]` so it is also described or exported.
3. Read `WARNING` lines. Fix or report anything that says an image could not be extracted.
4. Tell the user the output path, and the count of described, exported and dropped figures.

## Reruns, conflicts, and the log

- A source with a matching path and SHA-256 in the log is skipped (HIT). A changed file is a new version and is reconverted. The log, not which Markdown files exist, is the source of truth.
- Rerun only when the user asks: `--force`. If a logged output file is missing, report it; `--force` rebuilds it.
- If the output file exists but is not in the log, the script stops (CONFLICT). Replace it only with the user's approval, using `--overwrite`.
- The log is append-only; earlier records are kept on reruns. A failed conversion adds nothing.

## Trade-offs

- Docling takes about 20 seconds for a 16-page PDF after a slow first run (model download), and the install is large.
- A diagram kept as text loses arrows and layout; use `--force-figure-pages` when structure matters.
- Vision descriptions can vary between runs and can be wrong. Treat them as an aid, and check key numbers against the source.
- An assets folder makes the output multi-file; move the Markdown and its `_assets/` folder together.
