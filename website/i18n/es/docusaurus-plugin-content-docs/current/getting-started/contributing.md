---
title: Contribuir
description: Cómo configurar un entorno local y contribuir a IaC Code.
---

# Contribuir

## Requisitos previos

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Configuración

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` instala todas las dependencias y configura los hooks de pre-commit (verificaciones de lint y formato en cada commit).

## Flujo de desarrollo

Ejecutar en modo depuración:

```bash
make dev
```

Ejecutar pruebas:

```bash
make test           # versión de Python por defecto
make test PY=3.12   # versión específica
make test PY=all    # todas las versiones soportadas (3.10–3.14)
```

Calidad de código:

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

Cobertura:

```bash
make coverage
```

## Estructura del proyecto

```
src/iac_code/       # código fuente
tests/              # pruebas
website/            # sitio de documentación (Docusaurus)
```

## Enviar cambios

1. Haga fork del repositorio y cree una rama de funcionalidad.
2. Realice sus cambios con pruebas.
3. Ejecute `make format`, luego asegúrese de que `make lint` y `make test` pasen.
4. Abra un pull request contra `main`.
