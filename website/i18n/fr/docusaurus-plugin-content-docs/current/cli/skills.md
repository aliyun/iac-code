---
title: Skills
description: Sélectionner les skills dans la saisie, les gérer dans les plugins et créer des skills personnalisés.
---

# Skills

## Gérer les compétences

1. Ouvrez **Modules d’extension → Compétences** depuis la barre latérale ou les paramètres.

2. Choisissez le projet, puis recherchez un skill par son nom ou sa description.

3. Activez ou désactivez les skills utilisateur ou projet avec leur interrupteur. Les skills intégrés restent actifs ; les changements sont enregistrés automatiquement.

### Sélectionner et invoquer un skill

1. Dans la conversation, tapez `$` et une partie du nom ; les suggestions `/` contiennent aussi les skills.

2. Sélectionnez le skill. Une pastille apparaît près de la saisie.

3. Ajoutez votre demande ou les arguments et envoyez. Répondez ensuite aux questions et aux cartes d’autorisation.

Les compétences désactivées sont enregistrées dans `settings.yml` sous `disabled_skills`. Les compétences intégrées restent verrouillées comme activées et ne sont pas écrites dans la liste des désactivations.

La suite concerne les auteurs de skills. L’utilisation des skills intégrés n’exige pas d’écrire un fichier `SKILL.md`.

## Format des fichiers de compétence

Une compétence est un répertoire contenant un fichier `SKILL.md`. Le nom du répertoire sert de nom de compétence par défaut. Le répertoire peut aussi contenir des fichiers de référence supplémentaires :

```text
skills/
  deploy-check/
    SKILL.md
  my-skill/
    SKILL.md
    references/
      template.yml
```

## Découverte et priorité

IaC Code découvre les compétences depuis plusieurs emplacements. Lorsque des compétences partagent le même nom, les sources de priorité plus élevée remplacent celles de priorité plus faible :

| Priorité | Emplacement | Description |
|----------|----------|-------------|
| 1 (la plus faible) | `~/.iac-code/skills/` | Compétences globales utilisateur (suit `IAC_CODE_CONFIG_DIR`) |
| 2 | `skills/` | Répertoire de compétences au niveau du projet |
| 3 | `.iac-code/skills/` | Répertoire de compétences au niveau de la configuration du projet |
| 4 (la plus élevée) | Intégrées | Compétences intégrées livrées avec IaC Code ; elles ne peuvent pas être masquées par des compétences utilisateur ou projet portant le même nom |

Les répertoires de compétences de projet sont recherchés depuis la racine du dépôt git jusqu'au répertoire de travail courant ; en dehors d'un dépôt git, seul le répertoire courant est recherché.

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

Enregistrez le fichier sous `~/.iac-code/skills/checklist/SKILL.md` ou `.iac-code/skills/checklist/SKILL.md`, puis invoquez `/checklist` ou `$checklist` dans le champ de saisie web ou de bureau. `$` limite les suggestions aux skills.

## Permissions

- **Bundled skills** are always allowed automatically.
- **User/project skills** with no shell commands and no `allowed_tools` are auto-allowed.
- **Other skills** prompt for user confirmation on first use.
- **Les compétences utilisateur/projet désactivées** sont masquées des listes visibles par le modèle et des déclencheurs automatiques ; les appels directs à l'outil `skill` renvoient une erreur de compétence désactivée.
