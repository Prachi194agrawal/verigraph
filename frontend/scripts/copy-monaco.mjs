// Serve Monaco from this app instead of a CDN, so the code views also work offline and behind strict proxies.
// Runs before `next dev` / `next build` and copies monaco-editor's AMD build to public/monaco/vs.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = createRequire(import.meta.url).resolve("monaco-editor/package.json", { paths: [root] });
const src = join(dirname(pkg), "min", "vs");
const dest = join(root, "public", "monaco", "vs");
if (!existsSync(src)) {
  console.error(`monaco-editor is not installed (${src} is missing); run npm install`);
  process.exit(1);
}
rmSync(dest, { recursive: true, force: true });
cpSync(src, dest, { recursive: true });
console.log(`copied Monaco to ${dest}`);
