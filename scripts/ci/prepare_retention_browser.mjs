#!/usr/bin/env node
/** Start the existing isolated frontend/ROS/A2A test services; UI is driven separately.
 * No browser automation, credentials, account selection or cloud writes here.
 */
import { writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const values = Object.fromEntries(Array.from({ length: Math.floor((process.argv.length - 2) / 2) }, (_, n) =>
  [process.argv[2 + n * 2].replace(/^--/, ''), process.argv[3 + n * 2]]));
for (const key of ['frontend-repo', 'ros-agent-repo', 'iac-code-repo', 'source-config-dir', 'run-dir']) {
  if (!values[key]) throw new Error(`--${key} is required`);
}
const runDir = path.resolve(values['run-dir']);
await mkdir(runDir, { recursive: true, mode: 0o700 });
const lifecycle = await import(pathToFileURL(path.join(values['frontend-repo'],
  'scripts/aibuy-a2a-e2e/service-lifecycle.mjs')));
const env = await lifecycle.createCaseEnvironment({
  caseId: 'ecs-retention-browser', consoleUrl: 'local',
  frontendRepo: values['frontend-repo'], rosAgentRepo: values['ros-agent-repo'],
  iacCodeRepo: values['iac-code-repo'], sourceConfigDir: values['source-config-dir'],
  outputRoot: runDir, ports: { bridge: 41881 }, aliyunProfile: '',
  model: values.model || 'glm-5.2-fast-preview',
});
const stop = async () => { await env.stop(); process.exit(0); };
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
process.on('exit', () => env.emergencyStop());
try {
  await env.prepare();
  await env.start();
  const descriptor = { url: `${env.frontendOrigin}/aibuy?pipeline=solution-first&aibuyBackend=local-a2a`,
    frontendOrigin: env.frontendOrigin, bridgePort: env.bridgePort,
    a2aPort: env.a2aPort, root: env.dirs.root, model: values.model || 'glm-5.2-fast-preview' };
  await writeFile(path.join(runDir, 'browser-environment.json'), JSON.stringify(descriptor, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(descriptor));
  await new Promise(() => { setInterval(() => {}, 1000); });
} catch (error) {
  await writeFile(path.join(runDir, 'startup-error.txt'), String(error?.message || error), { mode: 0o600 });
  await env.stop().catch(() => {});
  console.error('Browser test services failed; inspect private startup-error.txt');
  process.exitCode = 1;
}
