# iac-code Documentation Website

This directory contains the Docusaurus documentation site for `iac-code`.

The English site is the default locale. Simplified Chinese content lives under `i18n/zh-Hans/` and is served with the `/zh-Hans/` locale prefix.

## Requirements

- Node.js 20 or later
- npm

## Development

Install dependencies:

```bash
npm install
```

Start the local development server:

```bash
make dev
```

Build the static site:

```bash
make build
```

Serve the production build locally:

```bash
make serve
```

## Structure

- `docs/`: English documentation pages
- `i18n/zh-Hans/`: Simplified Chinese translations
- `src/pages/`: custom site pages
- `src/css/`: global theme styles
- `static/`: static assets copied into the built site

Build output is written to `build/`. Local Docusaurus cache is written to `.docusaurus/`. Both directories are ignored by git.
