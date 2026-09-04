import { copyFile, mkdir, readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = resolve(frontendRoot, "..");
const latestPath = join(projectRoot, "reports", "factor_explorer", "latest.json");
const latest = JSON.parse(await readFile(latestPath, "utf8"));
const reportDirectory = join(
  projectRoot,
  "reports",
  "factor_explorer",
  latest.report_id.replace(/^sha256:/, ""),
);
const source = join(reportDirectory, "evidence-summary.json");
const destination = join(frontendRoot, "public", "data", "factor-explorer.json");

await mkdir(dirname(destination), { recursive: true });
await copyFile(source, destination);
console.log(`Synced ${latest.report_id} -> ${destination}`);
