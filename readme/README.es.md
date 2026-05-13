# iac-code

**Language**: [English](../README.md) | [中文](README.zh.md) | Español | [Français](README.fr.md) | [Deutsch](README.de.md) | [日本語](README.ja.md) | [Português](README.pt.md)

Asistente de Infraestructura como Código (IaC) impulsado por IA que genera y gestiona plantillas de orquestación de recursos de Alibaba Cloud (ROS / Terraform) mediante interacción en lenguaje natural.

> **Documentación**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/)

## Instalación

```bash
pip install iac-code
```

## Uso

En el primer uso, configure el proveedor de LLM y el servicio en la nube de IaC ingresando `/auth` en el modo interactivo.

### Modo Interactivo

Ejecute directamente para ingresar al REPL interactivo:

```bash
iac-code
```

### Modo No Interactivo

Pase un prompt único mediante `--prompt`:

```bash
iac-code --prompt "Crear un VPC y dos instancias ECS"
```

También se admite la lectura desde stdin:

```bash
echo "Crear un bucket OSS" | iac-code --prompt -
```
