# CONTINUE — MarkItDown · 2026-07-30T21:18:00-05:00 · [codex/a7-markitdown]

## Ground truth (run first, ~10 s)
```bash
git pull --ff-only && git status -sb
cmp -s CLAUDE.md AGENTS.md && echo "MIRROR OK" || echo "MIRROR DRIFT — fix + commit before any work"
git log --oneline -5
```

## Now
OPICS shared document distillation is implemented in OutPace's
`feketerj/MarkItDown` repository and locally verified. This repo is the
pipeline integration/control surface; the TDP workspace's isolated
DeepDoc runtime is consumed in place as an OCR/position instrument.
79/79 repository tests pass. The known MS51932 holdout reproduced 299 boxes on
page 1 and the full three-page path produced 6,419 Markdown characters and 986
positioned boxes with exact source/model/shim hashes.

## Next (in order)
1. Wire `machine-specs/scripts/crawl.py` to `pipeline_distill.py` for admitted
   completion work, preserving its existing domain extractor and provenance.
2. Add the same adapter at supplier-portals' governed document seam, then
   supplier-discovery. Crosswalk and budget migrations wait for their own DBR
   and parity gates.
3. Repair and independently prove DeepDoc table-structure recognition before
   changing the capability label beyond `deepdoc-ocr`.

## Open / blocked
- Full RAGFlow PDF parsing is not locally operational. The table-structure
  recognizer is present but its current shim construction fails; it is not used.
- The MarkItDown repository is ahead of `origin/master`; checkpoint push remains
  required after review/commit.

## Boot chain (read in order, then act)
1. ~/HAF/DCOM/REPORTING-INSTRUCTIONS.md
2. docs/DECISION-LOG.md — top entry

## Constraints in force
- `~/HAF/DCOM/docs/sop/WORKSPACE-STANDARD.md` — baseline requirements for the repository tree.
- `~/HAF/DCOM/docs/sop/SESSION-CONTINUITY-SOP.md` — thread discipline, checkpointing, and fresh-over-resume.
- `~/HAF/DCOM/docs/doctrine/ROE.md` — rules of engagement, authority, and confidence gates.
- Shared distillation is an extraction instrument, never a field-promotion
  engine. Source applicability, quotes/pages, terminal-null disposition, and
  promotion remain pipeline-owned.
- Never delete caller-owned documents, source-of-record caches, TDP proof
  packages, gold files, or evidence. `--owned-root` only admits a
  pipeline-owned input to recoverable quarantine. `pipeline_reap.py` requires
  independent hash-bound downstream completion authorization before deletion.
- Generic conversion runs in a resource-bounded subprocess behind a two-lane
  DBR drum. Archive expansion/nesting, output size, wall time, process-group
  RSS, CPU, file size, and file-descriptor limits fail closed. Generic
  non-PDF output is `parser_output_only`, never an unproven full-document claim.
