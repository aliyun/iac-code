import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { checkPrivateFile, matchingChromeProcess } from "../../scripts/web/e2e/resource_link_local_preflight.mjs";

test("local Chrome profile and debugging port must both match exactly", () => {
  const prefix = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const profile = "/tmp/chrome-profile";
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9225 --user-data-dir=${profile}`], profile), true);
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9222 --user-data-dir=${profile}`], profile), false);
  assert.equal(matchingChromeProcess([`${prefix} --remote-debugging-port=9225 --user-data-dir=${profile}-other`], profile), false);
});
test("token gate rejects broad permissions, empty files and symlinks without exposing values", (t) => {
  // Use explicit lstat metadata: Windows chmod cannot express POSIX mode 0600.
  const stat = { isFile: () => true, mode: 0o100600, size: 18 };
  t.mock.method(fs, "lstatSync", () => stat);
  const token = "offline-fake.token";
  checkPrivateFile(token);
  stat.mode = 0o100644;
  assert.throws(() => checkPrivateFile(token), /0600/);
  stat.mode = 0o100666;
  assert.throws(() => checkPrivateFile(token), /0600/);
  stat.mode = 0o100600;
  stat.size = 0;
  assert.throws(() => checkPrivateFile(token), /nonempty/);
  stat.size = 18;
  stat.isFile = () => false;
  assert.throws(() => checkPrivateFile(token), /regular file/);
});
