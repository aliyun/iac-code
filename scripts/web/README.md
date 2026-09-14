# Web Development Utilities

The files in this directory support local Web development, asset maintenance, and manual visual evaluation. They are
repository tools: normal Python, Web, and Desktop builds do not execute or package them.

Runtime artifacts generated or checked by these tools live under `src/iac_code/web/static/`. Setuptools includes the
committed JavaScript, license, and notice files in Python distributions, while the Desktop sidecar copies the same
static directory. A release therefore consumes the committed artifacts and does not require Node.js, pnpm, an Eraser
source checkout, or the ROS template corpus.

## Eraser browser bundle

`build_eraser_vendor.mjs` creates the browser-only Eraser bundle. `eraser/browser-entry.js` is its source entry point;
it registers local fonts and icons and exposes the resolver/render adapter used by the sandboxed Web frame. Neither
file is loaded by the product at runtime.

The build is pinned to Eraser commit `6d377f296b94abf63481a07128884066e4930321`. The Eraser checkout requires Node
22.12 or newer and pnpm 10.33. From the two repository roots:

```bash
cd /path/to/eraser-diagrams
corepack pnpm install --frozen-lockfile
corepack pnpm build

cd /path/to/iac-code
node scripts/web/build_eraser_vendor.mjs /path/to/eraser-diagrams
```

The command updates these committed release inputs:

- `src/iac_code/web/static/js/vendor/eraser-diagrams.min.js`
- `src/iac_code/web/static/js/vendor/eraser-diagrams.NOTICE`

The Eraser MIT license is maintained separately at
`src/iac_code/web/static/js/vendor/eraser-diagrams.LICENSE`. Review all three files when changing the pinned Eraser
revision. The script rejects any source checkout whose HEAD differs from the pinned commit and fails when it cannot
collect a bundled dependency's license text.

This command is intentionally outside the normal release build. Rebuilding during every package build would add an
external Git checkout and a Node toolchain to an otherwise self-contained release. Update and commit the vendor files
explicitly instead.

## Fixed-corpus layout evaluation

`evaluate_eraser_corpus.py` checks the fixed 30-template ROS corpus through the public preview API and the same layout
Worker used by the browser. It reports sibling overlap, failed containment, canvas overflow, edge/node intersections,
and extreme aspect ratios. It is a deterministic geometry check and does not call an LLM.

Start the local Web service, then run:

```bash
uv run iac-code web --host 127.0.0.1 --port 8766

uv run python scripts/web/evaluate_eraser_corpus.py \
  --templates-root /path/to/ros-templates \
  --base-url http://127.0.0.1:8766 \
  --output /tmp/iac-code-diagram-eval/run
```

The corpus manifest pins the expected `ros-templates` revision. Use `--allow-template-revision` only for an intentional
comparison with another revision. The evaluator reads templates and POSTs their contents to `--base-url`; keep the
default loopback URL unless sending those files to another service is intentional. The output path receives JSON and
Markdown reports and is not packaged.

The evaluation script remains outside `make test` because it needs a separate template checkout, Node.js, and a
running Web service. Unit and integration coverage for the preview API, graph projection, layout Worker, static assets,
and packaging stays under `tests/`.
