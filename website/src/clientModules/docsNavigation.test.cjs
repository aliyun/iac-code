const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const websiteRoot = path.resolve(__dirname, '../..');
const pipelineDocPath = '/docs/automation/pipeline-mode';

function readWebsiteFile(...segments) {
  return fs.readFileSync(path.join(websiteRoot, ...segments), 'utf8');
}

function readLocaleJson(locale, fileName) {
  return JSON.parse(readWebsiteFile('i18n', locale, 'docusaurus-theme-classic', fileName));
}

test('global navigation exposes Pipeline documentation directly', () => {
  const config = readWebsiteFile('docusaurus.config.ts');

  assert.match(config, /label:\s*'Pipeline'/);
  assert.match(config, /label:\s*'Pipeline Mode'/);
  assert.equal((config.match(new RegExp(`to:\\s*'${pipelineDocPath}'`, 'g')) ?? []).length, 2);
});

test('localized navbar and footer include Pipeline documentation labels', () => {
  const expected = {
    'zh-Hans': {navbar: 'Pipeline', footer: 'Pipeline 模式'},
    ja: {navbar: 'Pipeline', footer: 'Pipeline モード'},
    fr: {navbar: 'Pipeline', footer: 'Mode Pipeline'},
    de: {navbar: 'Pipeline', footer: 'Pipeline-Modus'},
    es: {navbar: 'Pipeline', footer: 'Modo Pipeline'},
    pt: {navbar: 'Pipeline', footer: 'Modo pipeline'},
  };

  for (const [locale, labels] of Object.entries(expected)) {
    const navbar = readLocaleJson(locale, 'navbar.json');
    const footer = readLocaleJson(locale, 'footer.json');

    assert.equal(navbar['item.label.Pipeline']?.message, labels.navbar);
    assert.equal(footer['link.item.label.Pipeline Mode']?.message, labels.footer);
  }
});
