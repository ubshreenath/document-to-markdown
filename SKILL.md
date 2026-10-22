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
| IMAGE-ONLY | The PDF has no text layer (every page is a picture). See "Image-only PDFs". |
| VISION CHOICE NEEDED (exit 3) | A vision model would do this job better than the fallback. Ask the user, see "Vision models". |
| API KEY MISSING (exit 4) | The chosen key variable is not set. |
| PAGE IMAGES NEEDED (exit 5) | No text and no vision model: page images are waiting for you to transcribe. |
| RECORDED | A hand transcription was logged with `record`. |

`sync <folder> [-r]` converts everything in a folder; `watch <folder>` polls a folder; `list <folder>` shows what was converted. Defaults: output in `<source folder>/extracted/` (with a generated `INDEX.md`), log in `<source folder>/.doc-to-markdown-log.json`.

## Requirements

Python 3.9+ and Docling 2.141 or newer, installed once per machine and shared by every project, with the RapidOCR extra so image-only PDFs can be read locally:

```
uv tool install --force --upgrade "docling[rapidocr]"
```

That is about 1.1 GB, plus about 500 MB of models cached in `~/.cache/huggingface` on first use. Without the tool install, the script falls back to `uvx --from docling`, which is slower to start. Docling before 2.141 breaks on rapidocr 3.10 and then wrongly reports "No OCR engine found". `uv tool upgrade docling` keeps the extra. No other packages are needed.

**Setup check:** run `doc_to_md.py doctor` on a new machine, and whenever a conversion says OCR is missing. It checks Docling, its version, OCR and page rendering, and prints the fix command for anything that fails. Ask the user before you run an install, because it changes their machine.

## What "self-contained" means

The Markdown file stands alone. Images are handled by size and content:

1. **Small images (icons, logos, badges, avatars; under 10% of the page):** removed. Any text inside them stays, inline.
2. **Graphics that contain text** (timelines, card rows, number badges): kept as text, not as images. Docling often labels vector graphics as "pictures" and hides their text; the script recovers it from the JSON.
3. **Large images with no text** (charts, photos, diagrams): described by the vision model if one is configured, otherwise exported to `<stem>_assets/` and linked, with a note at the top of the Markdown naming the folder. A missing important image is worse than an assets folder, so an image is never replaced by a "see the original" placeholder.

If the vision call fails for a figure, that figure is exported instead, so it is not lost.

## Image-only PDFs

Some PDFs are just pictures of pages, with no fonts and no selectable text. Some exporters rasterize every page, and so do scanners. Viewers such as Edge, Acrobat and Preview run their own OCR on these, so text can look selectable when the file has none. The script checks the file for fonts before converting it. With no fonts it prints `IMAGE-ONLY`, runs Docling's local OCR, and then picks a route from the page layout:

| Layout found by OCR | Vision model configured | No vision choice yet | Vision skipped |
|---|---|---|---|
| Single column (tables are fine) | OCR text | OCR text | OCR text |
| Side-by-side text blocks (two-column boxes, card rows) | Vision transcription | Ask (exit 3) | OCR text, with a deeper-review WARNING |
| OCR found no text | Vision transcription | Ask (exit 3) | Page images (exit 5) |

- **Why the split:** OCR is local, fast and good on single-column pages and tables. On side-by-side blocks it often merges two lists into one or reorders them, and it misreads small or coloured text (for example "Al" for "AI"). A vision model reads the page as a whole and keeps the layout. In testing it matched a hand transcription at 99%, where OCR managed 92%.
- **OCR copies** are logged as `docling-ocr` and carry a note at the top. Compare them with the page and fix them by hand, starting with the pages a WARNING names.
- **Vision copies** are logged as `vision-transcription`. If a vision call fails, the OCR text is used instead when there is any.
- **Page images (exit 5):** the pages are left in `<out>/<stem>_pages/` and nothing is logged. Read every image yourself and write the Markdown to the path given. Keep every heading, paragraph, list and table, and leave out running headers and footers. Then run `record "<path-to-document>"` with the same `--out`/`--log`/`--root` as the `get`. This logs the copy, so later requests HIT, and deletes the page images.

Never report an image-only PDF as converted until the Markdown is written (and recorded, for page images). Tell the user it had no text layer: a re-export as real text (for example "Save as PDF" from the source HTML) avoids all of this.

## Vision models

A vision model reads images. It is either a cloud API, reached with an API key, or a model running on this machine (Ollama, LM Studio and similar). The script asks for one only when it would clearly do better: large images without text, or image-only PDFs with side-by-side layouts or no OCR text. It then exits with code 3 (`VISION CHOICE NEEDED`) and writes nothing until a choice exists. The choice is saved once per machine.

**When you get exit code 3:**

1. Run `doc_to_md.py vision-status`. It shows what this machine already has: `cloud_presets_with_key` (providers whose key variable is set, names only) and `local_servers` (local model servers that are installed or running, with their models; for Ollama, `vision_models` lists the ones that can read images).
2. Ask the user, in one message:
   - what a vision model would do better here (the reason is in the status line), and that **without one** the result falls back to OCR, which will need a deeper review against the source, or to exported image files for figures;
   - whether to use a **local model**, so documents never leave the machine. Name any that `vision-status` found. If Ollama is installed but not running, say it needs `ollama serve` and a vision model (`ollama pull <model>`);
   - or a **cloud API key** (the table below). Say that cloud options send page or figure images to that provider, so they should check that this is acceptable for confidential documents;
   - or to **skip**.
3. Ask which **model**. Do not guess a model name: the user picks one from their provider's list, `ollama list`, or the local server's `models`.
4. Run `vision-set --provider <preset> --model <model>`. For a provider without a preset, use `--base-url URL --key-env NAME`, and `--auth-header NAME` if the key does not go in `Authorization: Bearer` (for example `api-key` for Azure OpenAI, whose URL also carries `?api-version=...`). Only the provider, URL, model, header name and the *name* of the key variable are saved (in `~/.config/doc-to-markdown/config.json`), never the key.
5. Run `vision-test`. It sends a small built-in image, with no document content, and checks that the model reads it back. A model that cannot read images fails here, before any document is sent.
6. If the key variable is not set (exit code 4), ask the user to set it in their environment and restart the agent. If they paste a key in chat, use it only as a per-command environment variable (`NAME=... python ...`) and never write it to a file, log, or Markdown.
7. If the user declines, run `vision-set --skip`. `vision-reset` forgets the choice. `--vision skip` on a single `get` keeps one confidential document local, whatever is configured.

| Option | Where | Preset | Key variable |
|---|---|---|---|
| Ollama (local) | ollama.com | `ollama` | none |
| LM Studio (local) | lmstudio.ai | `lmstudio` | none |
| llama.cpp server / vLLM / Jan (local) | their docs | `llamacpp` / `vllm` / `jan` | none |
| Anthropic | console.anthropic.com | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | platform.openai.com | `openai` | `OPENAI_API_KEY` |
| Google Gemini (has a free tier) | aistudio.google.com | `gemini` | `GEMINI_API_KEY` |
| Mistral | console.mistral.ai | `mistral` | `MISTRAL_API_KEY` |
| Kimi (Moonshot AI) | platform.moonshot.ai (mainland China: `--base-url https://api.moonshot.cn/v1`) | `kimi` | `MOONSHOT_API_KEY` |
| OpenRouter (many models, one key) | openrouter.ai | `openrouter` | `OPENROUTER_API_KEY` |
| Groq / Together / xAI | their consoles | `groq` / `together` / `xai` | `GROQ_API_KEY` / `TOGETHER_API_KEY` / `XAI_API_KEY` |
| Anything else OpenAI-compatible | | `--base-url` | `--key-env` |

Any endpoint that speaks the OpenAI `/chat/completions` format and accepts images works. The local presets assume each server's default port; use `--base-url` for another.

`watch` has nobody to ask, so it never stops for a choice: undecided figures are exported, and image-only PDFs use OCR, or page images if OCR found nothing.

## After a conversion

1. **Check that an image is really an image.** A "picture" is not trustworthy until verified: Docling also labels vector graphics (cards, timelines, number badges) as pictures and can swallow their text. For every `REVIEW` line, and for any figure that looks suspicious, open the source page and compare. If the page has selectable text where the Markdown shows a figure or a gap, it is text. Compare each page's text against the Markdown (for example with PyMuPDF) before trusting the result.
2. **Check diagrams that were kept as text.** A `REVIEW` line means a large graphic was kept as extracted text. If arrows, layout, or grouping carry meaning (flowcharts, architecture diagrams), re-run with `--force-figure-pages <page>[,<page>]` so it is also described or exported.
3. Read `WARNING` lines. Fix or report anything that says an image could not be extracted, or that OCR text needs a deeper review.
4. Tell the user the output path, how the text was obtained (Docling, OCR or vision model), and the count of described, exported and dropped figures.

## Reruns, conflicts, and the log

- A source with a matching path and SHA-256 in the log is skipped (HIT). A changed file is a new version and is reconverted. The log, not which Markdown files exist, is the source of truth.
- Rerun only when the user asks: `--force`. If a logged output file is missing, report it; `--force` rebuilds it.
- If the output file exists but is not in the log, the script stops (CONFLICT). Replace it only with the user's approval, using `--overwrite`.
- The log is append-only; earlier records are kept on reruns. A failed conversion adds nothing.

## Trade-offs

- Docling takes about 20 seconds for a 16-page PDF after a slow first run (model download), and the install is large.
- A diagram kept as text loses arrows and layout; use `--force-figure-pages` when structure matters.
- Vision transcription takes about 30 to 60 seconds a page on a cloud API (slower on a local model without a GPU), and costs per page. It reads one page at a time.
- Vision descriptions and transcriptions can vary between runs and can be wrong. Treat them as an aid, and check key numbers against the source.
- The layout check is a heuristic: it spots side-by-side text blocks, not every layout OCR gets wrong.
- An assets folder makes the output multi-file; move the Markdown and its `_assets/` folder together.
