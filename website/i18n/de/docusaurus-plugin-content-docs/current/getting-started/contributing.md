---
title: Mitwirken
description: Lokale Umgebung einrichten und zu IaC Code beitragen.
---

# Mitwirken

## Voraussetzungen

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Einrichtung

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` installiert alle Abhängigkeiten und richtet Pre-Commit-Hooks ein (Lint- und Format-Prüfungen bei jedem Commit).

## Entwicklungsablauf

Im Debug-Modus ausführen:

```bash
make dev
```

Tests ausführen:

```bash
make test           # Standard-Python-Version
make test PY=3.12   # bestimmte Version
make test PY=all    # alle unterstützten Versionen (3.10–3.14)
```

Codequalität:

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

Abdeckung:

```bash
make coverage
```

## Projektstruktur

```
src/iac_code/       # Quellcode
tests/              # Tests
website/            # Dokumentationsseite (Docusaurus)
```

## Änderungen einreichen

1. Forken Sie das Repository und erstellen Sie einen Feature-Branch.
2. Nehmen Sie Ihre Änderungen mit Tests vor.
3. Führen Sie `make format` aus und stellen Sie sicher, dass `make lint` und `make test` bestehen.
4. Öffnen Sie einen Pull Request gegen `main`.
