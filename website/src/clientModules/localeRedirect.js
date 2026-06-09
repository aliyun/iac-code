import siteConfig from '@generated/docusaurus.config';
import redirectRules from './localeRedirectRules.cjs';

const {getBrowserLocaleRedirect} = redirectRules;
const storageKey = 'iac-code.localeRedirect.v1';

function wasRedirectedInSession() {
  try {
    return window.sessionStorage.getItem(storageKey) === 'true';
  } catch {
    return false;
  }
}

function markRedirectedInSession() {
  try {
    window.sessionStorage.setItem(storageKey, 'true');
  } catch {
    // Storage can be unavailable in strict browser privacy modes.
  }
}

function getBrowserLanguages() {
  if (Array.isArray(window.navigator.languages) && window.navigator.languages.length > 0) {
    return window.navigator.languages;
  }

  return window.navigator.language ? [window.navigator.language] : [];
}

export function onRouteDidUpdate({location}) {
  if (typeof window === 'undefined') {
    return;
  }

  const redirectTo = getBrowserLocaleRedirect({
    baseUrl: siteConfig.baseUrl,
    defaultLocale: siteConfig.i18n.defaultLocale,
    locales: siteConfig.i18n.locales,
    pathname: location.pathname,
    search: location.search,
    hash: location.hash,
    browserLanguages: getBrowserLanguages(),
    alreadyRedirected: wasRedirectedInSession(),
  });

  if (redirectTo) {
    markRedirectedInSession();
    window.location.replace(redirectTo);
  }
}
