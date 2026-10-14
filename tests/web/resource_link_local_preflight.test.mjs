import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { checkPrivateFile, matchingChromeProcess } from "../../scripts/web/e2e/resource_link_local_preflight.mjs";

test("local Chrome profile and debugging port must both match exactly", () => {
  const prefix = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const profile = "/tmp/chrome-profile";
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9225 --user-data-dir=${profile}`], profile), true);
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9222 --user-data-dir=${profile}`], profile), false);
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9225 --user-data-dir=${profile}-other`], profile), false);
});
test("token gate rejects broad permissions, empty files and symlinks without exposing values", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "resource-link-local-preflight-test-"));
  try {
    const token = path.join(dir, "fake.token");
    fs.writeFileSync(token, "offline-fake-token", { mode: 0o600 });
    checkPrivateFile(token);
    fs.chmodSync(token, 0o644);
    assert.throws(() => checkPrivateFile(token), /0600/);
    fs.chmodSync(token, 0o600);
    fs.writeFileSync(token, "");
    assert.throws(() => checkPrivateFile(token), /nonempty/);
    fs.writeFileSync(token, "offline-fake-token");
    const link = path.join(dir, "symlink.token");
    fs.symlinkSync(token, link);
    assert.throws(() => checkPrivateFile(link), /regular file/);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
