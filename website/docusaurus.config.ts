import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'iac-code',
  tagline: 'AI-powered Infrastructure as Code for cloud infrastructure',
  favicon: 'img/favicon.png',

  url: process.env.SITE_URL ?? 'https://aliyun.github.io',
  baseUrl: process.env.BASE_URL ?? '/iac-code/',

  organizationName: 'aliyun',
  projectName: 'iac-code',

  onBrokenLinks: 'throw',
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-Hans', 'ja', 'fr', 'de', 'es', 'pt'],
    localeConfigs: {
      en: {
        label: 'English',
        direction: 'ltr',
        htmlLang: 'en-US',
      },
      'zh-Hans': {
        label: '简体中文',
        direction: 'ltr',
        htmlLang: 'zh-CN',
      },
      ja: {
        label: '日本語',
        direction: 'ltr',
        htmlLang: 'ja',
      },
      fr: {
        label: 'Français',
        direction: 'ltr',
        htmlLang: 'fr',
      },
      de: {
        label: 'Deutsch',
        direction: 'ltr',
        htmlLang: 'de',
      },
      es: {
        label: 'Español',
        direction: 'ltr',
        htmlLang: 'es',
      },
      pt: {
        label: 'Português',
        direction: 'ltr',
        htmlLang: 'pt',
      },
    },
  },

  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        language: ['en', 'zh'],
        indexBlog: false,
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          routeBasePath: 'docs',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/hero-cloud-terminal.png',
    navbar: {
      title: 'IaC Code',
      logo: {
        alt: 'IaC Code logo',
        src: 'img/logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/docs/cli/usage',
          label: 'CLI',
          position: 'left',
        },
        {
          href: 'https://github.com/aliyun/iac-code',
          label: 'GitHub',
          position: 'right',
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/getting-started/installation',
            },
            {
              label: 'CLI Overview',
              to: '/docs/cli/usage',
            },
            {
              label: 'Slash Commands',
              to: '/docs/cli/commands',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/aliyun/iac-code',
            },
            {
              label: 'Issues',
              href: 'https://github.com/aliyun/iac-code/issues',
            },
            {
              label: 'Discussions',
              href: 'https://github.com/aliyun/iac-code/discussions',
            },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'PyPI',
              href: 'https://pypi.org/project/iac-code/',
            },
            {
              label: 'Contact',
              to: '/docs/contact',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} IaC Code contributors.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'yaml', 'python', 'hcl'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
