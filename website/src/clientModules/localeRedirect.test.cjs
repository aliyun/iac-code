const assert = require('node:assert/strict');
const test = require('node:test');

const {
  getBrowserLocaleRedirect,
  selectPreferredLocale,
} = require('./localeRedirectRules.cjs');

const config = {
  baseUrl: '/iac-code/',
  defaultLocale: 'en',
  locales: ['en', 'zh-Hans', 'ja', 'fr', 'de', 'es', 'pt'],
};

test('selectPreferredLocale maps browser language variants to configured locales', () => {
  assert.equal(selectPreferredLocale(['zh-CN', 'en-US'], config), 'zh-Hans');
  assert.equal(selectPreferredLocale(['ja-JP', 'en-US'], config), 'ja');
  assert.equal(selectPreferredLocale(['pt-BR', 'en-US'], config), 'pt');
  assert.equal(selectPreferredLocale(['it-IT', 'fr-FR'], config), 'fr');
});

test('selectPreferredLocale falls back to the default locale for unsupported languages', () => {
  assert.equal(selectPreferredLocale(['it-IT'], config), 'en');
});

test('getBrowserLocaleRedirect redirects only the root page to a non-default browser locale', () => {
  assert.equal(
    getBrowserLocaleRedirect({
      ...config,
      pathname: '/iac-code/',
      search: '?utm_source=test',
      hash: '#top',
      browserLanguages: ['zh-CN', 'en-US'],
      alreadyRedirected: false,
    }),
    '/iac-code/zh-Hans/?utm_source=test#top',
  );
});

test('getBrowserLocaleRedirect skips localized, non-root, default-locale, and already-redirected pages', () => {
  assert.equal(
    getBrowserLocaleRedirect({
      ...config,
      pathname: '/iac-code/zh-Hans/docs/intro',
      search: '',
      hash: '',
      browserLanguages: ['zh-CN'],
      alreadyRedirected: false,
    }),
    null,
  );
  assert.equal(
    getBrowserLocaleRedirect({
      ...config,
      pathname: '/iac-code/docs/intro',
      search: '',
      hash: '',
      browserLanguages: ['zh-CN'],
      alreadyRedirected: false,
    }),
    null,
  );
  assert.equal(
    getBrowserLocaleRedirect({
      ...config,
      pathname: '/iac-code/',
      search: '',
      hash: '',
      browserLanguages: ['en-US'],
      alreadyRedirected: false,
    }),
    null,
  );
  assert.equal(
    getBrowserLocaleRedirect({
      ...config,
      pathname: '/iac-code/',
      search: '',
      hash: '',
      browserLanguages: ['zh-CN'],
      alreadyRedirected: true,
    }),
    null,
  );
});
