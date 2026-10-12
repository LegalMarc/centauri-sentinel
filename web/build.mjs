// Build the dashboard's UI bundle into sentinel/web/static/vendor/openbridge/.
// Run by the Dockerfile's web stage, and locally with `npm --prefix web ci && npm --prefix web run build`.
import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = join(here, "node_modules", "@oicl", "openbridge-webcomponents");
const out = join(here, "..", "sentinel", "web", "static", "vendor", "openbridge");
mkdirSync(out, { recursive: true });

await build({
  entryPoints: [join(here, "entry.js")],
  bundle: true,
  format: "esm",
  minify: true,
  target: ["es2022"],
  legalComments: "eof",
  outfile: join(out, "ob.js"),
  logLevel: "info",
});

copyFileSync(join(pkg, "dist", "openbridge.css"), join(out, "openbridge.css"));
copyFileSync(join(pkg, "dist", "NotoSans.ttf"), join(out, "NotoSans.ttf"));
copyFileSync(join(here, "THIRD_PARTY_LICENSES.txt"), join(out, "LICENSE.txt"));
