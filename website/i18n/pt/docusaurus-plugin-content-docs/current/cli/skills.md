---
title: Skills
description: Selecionar skills na entrada, gerenciá-los em Plugins e criar skills próprios quando necessário.
---

# Skills

## Gerenciar habilidades

1. Abra **Plugins → Habilidades** pela barra lateral ou configurações.

2. Escolha o projeto no topo e pesquise por nome ou descrição.

3. Use o interruptor para ativar ou desativar skills de usuário ou projeto. Os integrados permanecem ativos e as mudanças são salvas automaticamente.

### Selecionar e invocar um skill

1. Digite `$` e parte do nome do skill na conversa. As sugestões `/` também incluem skills.

2. Selecione o skill. Uma etiqueta aparece junto à entrada.

3. Acrescente o pedido ou os argumentos e envie. Responda depois às perguntas e aos cartões de permissão.

As habilidades desabilitadas são salvas em `settings.yml` em `disabled_skills`. As habilidades integradas ficam sempre habilitadas e não são gravadas na lista de desabilitadas.

As próximas seções são para autores de skills. Não é necessário escrever `SKILL.md` para usar os skills integrados.

## Formato dos arquivos de skill

Uma skill é um diretório que contém um arquivo `SKILL.md` com frontmatter YAML. O nome do diretório é o nome padrão da skill, e o diretório pode conter arquivos de referência adicionais:

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

O IaC Code descobre skills a partir de várias localizações. Quando skills têm o mesmo nome, as fontes de prioridade mais alta substituem as de prioridade mais baixa:

| Prioridade | Localização | Descrição |
|----------|----------|-------------|
| 1 (mais baixa) | `~/.iac-code/skills/` | Skills globais do usuário (segue `IAC_CODE_CONFIG_DIR`) |
| 2 | `skills/` | Diretório de skills no nível do projeto |
| 3 | `.iac-code/skills/` | Diretório de skills no nível de configuração do projeto |
| 4 (mais alta) | Integradas | Skills integradas fornecidas com o IaC Code; não podem ser sobrepostas por skills de usuário ou de projeto com o mesmo nome |

Os diretórios de skills de projeto são pesquisados a partir da raiz do repositório git para baixo, até o diretório de trabalho atual; quando não está dentro de um repositório git, apenas o diretório atual é pesquisado.

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

Salve o arquivo em `~/.iac-code/skills/checklist/SKILL.md` ou `.iac-code/skills/checklist/SKILL.md` e invoque `/checklist` ou `$checklist` no campo de entrada do Web ou Desktop. `$` filtra apenas skills.

## Permissions

- **Bundled skills** are always allowed automatically.
- **User/project skills** with no shell commands and no `allowed_tools` are auto-allowed.
- **Other skills** prompt for user confirmation on first use.
- **Habilidades de usuário/projeto desabilitadas** ficam ocultas das listas visíveis ao modelo e dos gatilhos automáticos; chamadas diretas à ferramenta `skill` retornam um erro de habilidade desabilitada.
