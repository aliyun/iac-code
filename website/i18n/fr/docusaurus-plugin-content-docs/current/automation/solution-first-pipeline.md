---
title: Pipeline avec solution en premier
description: Choisissez une architecture avant de générer et déployer son modèle ROS.
---

# Pipeline avec solution en premier

`selling_solution_first` est un pipeline d'achat Alibaba Cloud qui permet de comparer les architectures avant que IaC Code ne génère un modèle ROS. Seule la solution sélectionnée est implémentée et chiffrée, ce qui évite de travailler sur des candidats qui ne seront pas déployés.

Le pipeline `selling` reste disponible et demeure le choix par défaut. Le nouveau pipeline est une alternative explicite et ne modifie pas les sessions `selling` existantes.

## Quand l'utiliser

Utilisez `selling_solution_first` pour :

- comparer plusieurs architectures, produits, coûts, avantages et risques avant l'implémentation ;
- préciser la région, l'échelle, le réseau, la disponibilité ou le budget avant de retenir un modèle ;
- générer, prévisualiser et chiffrer uniquement l'architecture choisie ;
- vérifier les paramètres ROS finaux et le devis exact avant de créer des ressources cloud.

| Pipeline | Ordre des opérations |
|---|---|
| `selling` | Génère et évalue les modèles candidats, permet d'en choisir un, puis le déploie. |
| `selling_solution_first` | Planifie et fait choisir une architecture, implémente uniquement ce choix, puis le déploie. |

## Démarrer le pipeline

Dans le terminal interactif :

```bash
IAC_CODE_MODE=pipeline \
IAC_CODE_PIPELINE_NAME=selling_solution_first \
iac-code
```

Dans l'application Web locale, choisissez le mode Pipeline à la création de la conversation et démarrez le serveur avec le nom du pipeline :

```bash
IAC_CODE_PIPELINE_NAME=selling_solution_first iac-code web
```

Avec A2A, l'appelant peut sélectionner le mode et le pipeline pour chaque message sans changer la valeur par défaut du serveur :

```json
{
  "metadata": {
    "iac_code": {
      "run_mode": "pipeline",
      "pipeline_name": "selling_solution_first",
      "preferredLanguage": "fr",
      "candidatePresentation": "rich-v1"
    }
  }
}
```

`pipeline_name` accepte `selling` et `selling_solution_first`. Une valeur non vide non prise en charge est rejetée plutôt que de lancer silencieusement un autre pipeline. Pour poursuivre un pipeline enregistré, réutilisez le même `contextId` A2A ; l'identité conservée dans l'instantané durable fait autorité.

## Les trois étapes

### 1. Planifier et choisir une solution

IaC Code vérifie d'abord que la demande concerne une tâche d'infrastructure Alibaba Cloud prise en charge. Il peut poser des questions ciblées lorsqu'une information manquante modifierait sensiblement les produits, la topologie ou le prix.

Il présente ensuite une à trois solutions comparables. Une solution peut inclure :

- un diagramme d'architecture et la topologie ;
- les produits Alibaba Cloud et l'inventaire des ressources ;
- les spécifications recommandées et les contraintes impératives ;
- les scénarios adaptés et les problèmes résolus ;
- une estimation mensuelle approximative pour la comparaison ;
- les avantages, inconvénients, risques et motifs de la recommandation.

Vous pouvez choisir une solution, modifier le besoin afin de générer un nouvel ensemble ou annuler. Aucun modèle ROS ni aucune ressource cloud n'est créé à cette étape.

### 2. Implémenter la solution sélectionnée

IaC Code travaille uniquement sur la solution retenue. Il génère et écrit le modèle ROS, le valide, résout les paramètres obligatoires, exécute `PreviewStack` et demande une estimation ROS précise.

Avant le déploiement, l'interface affiche l'architecture finale, les paramètres du modèle et le devis. Vous pouvez :

- confirmer le déploiement ;
- modifier les paramètres autorisés et recalculer ;
- revenir à la première étape pour choisir ou planifier une autre solution ;
- annuler sans créer de ressources cloud.

L'estimation approximative de l'étape 1 et le devis ROS précis de l'étape 2 sont deux valeurs différentes. La confirmation du déploiement utilise le devis précis et les paramètres actuels du modèle.

### 3. Déployer

Après confirmation, IaC Code crée la pile ROS, diffuse sa progression faisant autorité, attend l'état terminal et enregistre l'ID de pile et les sorties. Les échecs de déploiement restent disponibles pour le diagnostic et la récupération.

## Confirmation du déploiement et autorisation d'outil

La confirmation du déploiement et l'autorisation d'outil constituent deux limites de sécurité distinctes :

1. **Confirmation du déploiement** : vous acceptez la solution, les paramètres et le coût annoncé.
2. **Autorisation d'outil** : vous autorisez, pour cette exécution, un appel concret modifiant le cloud, comme `ros:CreateStack` ou `vpc:CreateVpc`.

Accepter la première ne valide pas automatiquement la seconde. Lorsqu'un outil nécessite une autorisation, IaC Code s'arrête à cet endroit et présente une demande sûre. Les opérations de lecture, de modification et de suppression sont distinguées. Les détails d'API peuvent contenir le produit, l'API, la région, la séquence d'appels et des paramètres expurgés ; les identifiants, jetons, signatures et autres valeurs sensibles ne figurent jamais dans les champs d'affichage.

L'utilisateur peut choisir **Autoriser une fois** ou **Refuser**. La décision est corrélée à la demande exacte et inscrite dans le journal d'audit. Si l'enregistrement d'audit requis ne peut pas être conservé, une autorisation échoue de manière sûre.

## Pause, récupération et transfert

Le choix d'une solution, les questions, la confirmation et les autorisations sont des attentes récupérables. IaC Code conserve un instantané avant de dépendre de la poursuite par l'appelant. Après un redémarrage ou le rechargement de la conversation, l'interface reconstruit les étapes terminées et replace chaque entrée en attente à son emplacement d'origine.

Pour les intégrations A2A :

- les événements `permission_requested` et `permission_resolved` conservent l'étape propriétaire et les coordonnées du candidat ;
- `pendingPermissions` expose les demandes non résolues dans un instantané restauré ;
- une réponse d'autorisation latérale reprend la tâche et le contexte d'origine ;
- la répétition d'une même décision est idempotente, tandis qu'une décision contradictoire est rejetée.

Lorsque le pipeline se termine, échoue, s'arrête plus tôt ou est annulé, il transfère le même contexte vers la conversation normale. Les requêtes suivantes peuvent utiliser la solution, le modèle, le résultat du déploiement et l'état du nettoyage sans créer une nouvelle conversation.

## Interfaces et langues

Le pipeline fonctionne dans le terminal interactif, l'application Web locale, l'enveloppe Web Desktop, le mode processus SDK et le serveur A2A. Les capacités d'affichage varient — A2A peut par exemple demander la présentation structurée `rich-v1` — mais l'état et les limites de sécurité sont communs.

Les textes visibles sont disponibles en anglais, chinois simplifié, espagnol, français, allemand, japonais et portugais. Les appelants A2A choisissent la langue d'une requête avec `metadata.iac_code.preferredLanguage` ; les noms de champs, valeurs d'énumération, identifiants et structures JSON ne sont pas traduits.

## Documentation associée

- [Mode Pipeline](./pipeline-mode.md)
- [Application Web](../web-app.md)
- [Référence du protocole A2A](../a2a/protocol-reference.md)
- [Identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
