---
title: Skills
description: Seleccionar skills en la entrada, gestionarlos en Plugins y crear skills propios cuando sea necesario.
---

# Skills

## Gestionar habilidades

1. Abre **Complementos → Habilidades** desde la barra lateral o los ajustes.

2. Elige el proyecto y busca por nombre o descripción.

3. Activa o desactiva los skills de usuario o proyecto con su interruptor. Los integrados permanecen activos y los cambios se guardan automáticamente.

### Seleccionar e invocar un skill

1. Escribe `$` y parte del nombre del skill en la conversación. Las sugerencias `/` también incluyen skills.

2. Selecciona el skill. Aparece una etiqueta junto a la entrada.

3. Añade la solicitud o los argumentos y envía. Responde después a las preguntas y tarjetas de permisos.

Las habilidades deshabilitadas se guardan en `settings.yml` bajo `disabled_skills`. Las habilidades integradas permanecen siempre habilitadas y no se escriben en la lista de deshabilitadas.

El resto se dirige a autores de skills. No necesita escribir `SKILL.md` para usar los skills integrados.

## Skill File Formats

A skill is a directory containing a `SKILL.md` file. The directory name is the skill's default name. The directory can also hold additional reference files:

```text
skills/
  deploy-check/
    SKILL.md
  my-skill/
    SKILL.md
    references/
      template.yml
```

## Discovery and Priority

IaC Code discovers skills from multiple locations. When skills share the same name, higher-priority sources override lower-priority ones:

| Priority | Location | Description |
|----------|----------|-------------|
| 1 (lowest) | `~/.iac-code/skills/` | User-global skills (follows `IAC_CODE_CONFIG_DIR`) |
| 2 | `skills/` | Project-level skills directory |
| 3 | `.iac-code/skills/` | Project config-level skills directory |
| 4 (highest) | Bundled | Built-in skills shipped with IaC Code; cannot be shadowed by same-name user or project skills |

Project skill directories are searched from the git repository root down to the current working directory. When not inside a git repository, only the current directory is searched.

## Frontmatter Reference

Every skill file starts with YAML frontmatter between `---` delimiters:

```yaml
---
name: deploy-check
description: Verify deployment readiness of the current stack
when_to_use: When the user asks to check or verify a deployment
argument_hint: <stack-name>
arguments:
  - stackName
  - region
allowed_tools:
  - bash
  - aliyun_api
user_invocable: true
model: inherit
effort: ""
context: inline
agent: general-purpose
paths:
  - "*.yml"
  - "templates/**/*.json"
---
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | **Yes** | filename stem | Skill name used for invocation. Falls back to the filename if omitted |
| `description` | **Yes** | `""` | One-line description shown in command listings |
| `descriptions` | No | `{}` | Localized descriptions keyed by language code (e.g., `zh-Hans`) |
| `when_to_use` | No | `""` | Hint for the model on when to invoke this skill automatically |
| `argument_hint` | No | `""` | Placeholder shown after the command name |
| `arguments` | No | `[]` | Named argument list for positional substitution |
| `allowed_tools` | No | `[]` | Tools the skill is allowed to use (applies to both inline and fork modes) |
| `user_invocable` | No | `true` | Whether the user can invoke this skill directly via `/name` (or `$name`) |
| `model` | No | `"inherit"` | Model override for this skill execution |
| `effort` | No | `""` | Thinking effort override |
| `context` | No | `"inline"` | Execution mode: `inline` or `fork` |
| `agent` | No | `"general-purpose"` | Agent type for fork mode |
| `paths` | No | `[]` | Glob patterns for path-based auto-activation |

## Execution Modes

### Inline (default)

The skill's rendered content is injected directly into the current conversation context. The model sees it as additional instructions and acts on them within the same session.

```yaml
context: inline
```

### Fork

The skill runs in an isolated sub-agent with its own context. The sub-agent's final response is returned as a tool result. Use this for self-contained tasks that shouldn't pollute the main conversation.

```yaml
context: fork
agent: general-purpose
```

## Argument Substitution

Skill content can reference arguments passed by the user:

| Placeholder | Description |
|-------------|-------------|
| `$ARGUMENTS` | The full argument string |
| `$0`, `$1`, ... | Positional arguments (space-separated, respects quotes) |
| `$ARGUMENTS[0]`, `$ARGUMENTS[1]` | Explicit indexed access |
| `$argName` | Named argument (matched by position in the `arguments` list) |

If no placeholder is found in the content, arguments are appended as `ARGUMENTS: <value>`.

Example with named arguments:

```yaml
---
name: deploy
arguments:
  - stackName
  - region
---

Deploy the stack **$stackName** in region **$region**.
```

Invocation: `/deploy my-stack cn-hangzhou`

## Built-in Variables

| Variable | Description |
|----------|-------------|
| `${SKILL_DIR}` | Absolute path to the skill's source directory |
| `${SESSION_ID}` | Current session identifier |

## Path-based Auto-activation

Skills with a `paths` field are automatically activated when the model accesses a file matching any of the listed glob patterns:

```yaml
---
name: ros-helper
paths:
  - "*.yml"
  - "templates/**/*.json"
---
```

When a matching file is accessed, the skill becomes available to the model for the remainder of the session.

## Example

A simple skill that generates a deployment checklist:

```markdown
---
name: checklist
description: Generate a pre-deployment checklist
when_to_use: When the user wants to review before deploying
user_invocable: true
---

Review the current project and generate a pre-deployment checklist covering:

1. Template validation status
2. Parameter completeness
3. Security group rules
4. Resource naming conventions
5. Cost estimation

If a stack name is provided, also check the current stack status.
```

Guarde el archivo en `~/.iac-code/skills/checklist/SKILL.md` o `.iac-code/skills/checklist/SKILL.md` y ejecute `/checklist` o `$checklist` en el campo de entrada web o de escritorio. `$` filtra solo skills.

## Permissions

- **Bundled skills** are always allowed automatically.
- **User/project skills** with no shell commands and no `allowed_tools` are auto-allowed.
- **Other skills** prompt for user confirmation on first use.
- **Las habilidades de usuario/proyecto deshabilitadas** se ocultan de los listados visibles para el modelo y de los disparadores automáticos; las llamadas directas a la herramienta `skill` devuelven un error de habilidad deshabilitada.
