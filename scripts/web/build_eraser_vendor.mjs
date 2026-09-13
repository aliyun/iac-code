import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ERASER_COMMIT = "6d377f296b94abf63481a07128884066e4930321";
const scriptDir = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDir, "../..");
const sourceRoot = resolve(process.argv[2] || "");
if (!process.argv[2]) {
  throw new Error("Usage: node scripts/web/build_eraser_vendor.mjs /path/to/eraser-diagrams");
}
const actualCommit = execFileSync("git", ["rev-parse", "HEAD"], {
  cwd: sourceRoot,
  encoding: "utf8",
}).trim();
if (actualCommit !== ERASER_COMMIT) {
  throw new Error(`Expected eraser-diagrams ${ERASER_COMMIT}, found ${actualCommit}`);
}
const require = createRequire(join(sourceRoot, "package.json"));
const { build } = require("esbuild");
const eraserModules = new Map([
  ["@eraserlabs/diagrams/library", "packages/diagrams/dist/library/index.js"],
  ["@eraserlabs/diagrams/normalizers", "packages/diagrams/dist/library/normalizers.js"],
  ["@eraserlabs/layout", "packages/layout/dist/index.js"],
  ["@eraserlabs/protocol", "packages/protocol/dist/index.js"],
  ["@eraserlabs/protocol/schema", "packages/protocol/dist/schema.js"],
  ["@eraserlabs/protocol/schemas/tag-schema", "packages/protocol/schemas/tag-schema.schema.json"],
  ["@eraserlabs/render", "packages/render/dist/index.js"],
  ["@eraserlabs/render/browser", "packages/render/dist/browser/index.js"],
  ["@eraserlabs/resolve", "packages/resolve/dist/index.js"],
  ["@eraserlabs/resolve/schema", "packages/resolve/dist/schema/index.js"],
  ["@eraserlabs/utils", "packages/utils/dist/index.js"],
]);
const result = await build({
  absWorkingDir: "/",
  entryPoints: [join(scriptDir, "eraser", "browser-entry.js")],
  bundle: true,
  platform: "browser",
  format: "iife",
  minify: true,
  define: { "process.env": "{}" },
  nodePaths: [join(sourceRoot, "node_modules")],
  plugins: [
    {
      name: "eraser-workspace-modules",
      setup(builder) {
        builder.onResolve({ filter: /^@eraserlabs\// }, (args) => {
          const relative = eraserModules.get(args.path);
          return relative ? { path: join(sourceRoot, relative) } : null;
        });
      },
    },
  ],
  outfile: join(repositoryRoot, "src/iac_code/web/static/js/vendor/eraser-diagrams.min.js"),
  legalComments: "none",
  metafile: true,
});
const mitFallback = (owner) => `MIT License

Copyright (c) ${owner}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`;
const bundledPackages = [...new Set(
  Object.keys(result.metafile.inputs)
    .map((input) => input.match(/node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?((?:@[^/]+\/)?[^/]+)/)?.[1])
    .filter(Boolean),
)].sort();
const thirdPartyLicenses = new Map();
function addPackageLicense(directory) {
  const packageMetadata = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
  const name = String(packageMetadata.name || "");
  if (!name || name.startsWith("@eraserlabs/")) return;
  const key = `${name}@${packageMetadata.version || "unknown"}`;
  if (thirdPartyLicenses.has(key)) return;
  const licenseName = readdirSync(directory)
    .filter((entry) => /^(licen[cs]e|copying)(\.|$)/i.test(entry))
    .sort()[0];
  if (licenseName) {
    thirdPartyLicenses.set(key, readFileSync(join(directory, licenseName), "utf8").trim());
  } else if (packageMetadata.license === "MIT") {
    const author = typeof packageMetadata.author === "string"
      ? packageMetadata.author
      : packageMetadata.author?.name || name;
    thirdPartyLicenses.set(key, mitFallback(author));
  } else {
    throw new Error(`Missing license file for bundled dependency ${key}`);
  }
}
for (const input of Object.keys(result.metafile.inputs)) {
  if (input.startsWith("<") || input.startsWith("(")) continue;
  let directory = dirname(realpathSync(resolve("/", input)));
  while (directory.startsWith(sourceRoot)) {
    const packagePath = join(directory, "package.json");
    if (existsSync(packagePath)) {
      addPackageLicense(directory);
      break;
    }
    const parent = dirname(directory);
    if (parent === directory) break;
    directory = parent;
  }
}
const pnpmStore = join(sourceRoot, "node_modules", ".pnpm");
for (const packageName of bundledPackages) {
  const hasLicense = [...thirdPartyLicenses.keys()].some((key) => key.startsWith(`${packageName}@`));
  if (hasLicense) continue;
  const packageDirectory = readdirSync(pnpmStore)
    .map((entry) => join(pnpmStore, entry, "node_modules", packageName))
    .find((candidate) => existsSync(join(candidate, "package.json")));
  if (!packageDirectory) throw new Error(`Cannot locate bundled dependency ${packageName}`);
  addPackageLicense(packageDirectory);
}
const notice = [
  "Eraser diagrams browser bundle third-party notices",
  "===================================================",
  "",
  `Generated from eraserlabs/eraser-diagrams commit ${ERASER_COMMIT}.`,
  "The Eraser Diagrams license is in eraser-diagrams.LICENSE.",
  "",
  ...[...thirdPartyLicenses.entries()].sort(([left], [right]) => left.localeCompare(right)).flatMap(([name, license]) => [
    name,
    "-".repeat(name.length),
    license,
    "",
  ]),
].join("\n");
writeFileSync(join(repositoryRoot, "src/iac_code/web/static/js/vendor/eraser-diagrams.NOTICE"), notice, "utf8");
console.log(`Built Eraser browser bundle from ${ERASER_COMMIT}: ${bundledPackages.join(", ")}`);
