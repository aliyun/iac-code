---
title: Application de bureau
description: Installez et utilisez l'application native IaC Code sous macOS, Windows et Linux.
---

# Application de bureau

L'application de bureau IaC Code propose le même agent, les mêmes fournisseurs, intégrations cloud, projets et conversations que la CLI et l'application web, dans une application native installée. L'hôte Tauri lance l'environnement Python fourni et charge l'interface locale IaC Code par une connexion de bouclage ; aucun service web public n'est exposé.

## Paquets pris en charge

Téléchargez le paquet adapté à votre plateforme depuis les [versions GitHub](https://github.com/aliyun/iac-code/releases).

| Système d'exploitation | Architecture | Paquet | Mode de mise à jour |
|---|---|---|---|
| macOS | Puce Apple | `.dmg` | Mise à jour dans l'application |
| Windows | x64 | programme d'installation `.exe` | Mise à jour dans l'application |
| Linux | x64 | `.AppImage` | Mise à jour dans l'application |
| Debian / Ubuntu | x64 | `.deb` | Installation d'un paquet plus récent |

Chaque version contient également `SHA256SUMS`, une nomenclature logicielle (SBOM) et les mentions relatives aux logiciels tiers.

## Installation

### macOS

1. Téléchargez et ouvrez le fichier `.dmg`, puis faites glisser **IaC Code** dans **Applications**.
2. Ouvrez IaC Code depuis le dossier Applications.
3. Le paquet actuel ne possède pas encore de signature Apple Developer ID et n'est pas notarié. macOS peut donc bloquer le premier lancement. Après avoir vérifié la somme de contrôle, cliquez sur l'application tout en maintenant la touche Contrôle enfoncée, puis choisissez **Ouvrir**. Vous pouvez aussi l'autoriser dans **Réglages Système > Confidentialité et sécurité**.

### Windows

1. Téléchargez et exécutez le programme d'installation `.exe`. IaC Code s'installe pour l'utilisateur actuel et crée les raccourcis de l'application.
2. Si Microsoft Defender SmartScreen signale un éditeur inconnu, vérifiez `SHA256SUMS`, sélectionnez **Informations complémentaires** et ne continuez que si la somme correspond à celle de la version publiée.
3. Le paquet comprend la prise en charge du programme d'amorçage WebView2 nécessaire à l'interface. Au premier lancement, IaC Code vérifie également la présence de Git Bash et propose un guide d'installation s'il est absent.

### Linux AppImage

Rendez le fichier téléchargé exécutable, puis lancez-le :

```bash
chmod +x iac-code_*.AppImage
./iac-code_*.AppImage
```

Après le premier lancement, l'environnement de bureau peut vous proposer de créer un lanceur. L'AppImage peut se mettre à jour automatiquement lorsqu'une mise à jour signée est disponible.

### Debian ou Ubuntu

Installez le paquet deb avec APT afin que les dépendances système soient résolues :

```bash
sudo apt install ./iac-code_*_amd64.deb
```

Lancez **IaC Code** depuis le menu des applications. Une installation deb n'utilise pas le système de mise à jour intégré ; téléchargez et installez le nouveau paquet deb pour effectuer une mise à niveau.

## Premier lancement

Au premier démarrage, IaC Code vous demande de choisir un dossier de projet. Ce dossier devient l'espace de travail utilisé pour accéder aux fichiers, générer les modèles, exécuter les outils et enregistrer les conversations. Vous pourrez ensuite changer de projet à l'aide du sélecteur.

Si vous avez déjà utilisé la CLI ou l'application web, l'application de bureau réutilise la configuration de `~/.iac-code/` (ou `IAC_CODE_CONFIG_DIR`), notamment les fournisseurs de modèles, les identifiants Alibaba Cloud, les réglages et les sessions enregistrées. Dans le cas contraire, ouvrez les **Réglages** pour configurer un fournisseur de modèles et les identifiants cloud avant de lancer une tâche.

L'interface est disponible en anglais, chinois simplifié, japonais, français, allemand, espagnol et portugais. La langue et le thème de couleurs se règlent dans **Réglages > Général**.

## Mises à jour et signatures des paquets

Les versions macOS, Windows et AppImage consultent périodiquement les informations de la version stable et peuvent télécharger et installer une nouvelle version. Avant l'installation, chaque mise à jour est vérifiée au moyen de la clé publique de mise à jour IaC Code. Le paquet deb suit quant à lui la procédure habituelle des paquets Linux.

La signature d'une mise à jour n'est pas la signature d'éditeur contrôlée par le système d'exploitation. La première confirme que la mise à jour a été produite par IaC Code ; la notarisation macOS et la signature de code Windows identifient l'éditeur auprès du système. Les programmes d'installation actuels ne possèdent pas encore de signature commerciale d'éditeur : les avertissements de la plateforme sont donc prévisibles. Téléchargez toujours les paquets depuis la page officielle et vérifiez `SHA256SUMS`.

## Dépannage

- **L'application reste sur l'écran de démarrage :** utilisez les commandes de récupération pour réessayer ou ouvrir le dossier de diagnostic. Le journal indique si un fichier d'exécution manque, si un port de bouclage est occupé ou si le processus auxiliaire n'a pas pu démarrer.
- **Windows indique que Git Bash est absent :** suivez le guide d'installation, redémarrez IaC Code et relancez la vérification. Sous Windows, les outils d'agent fondés sur le shell ont besoin de Git Bash.
- **Linux ouvre le fichier deb comme une archive :** installez-le avec la commande APT ci-dessus au lieu de l'ouvrir dans un gestionnaire d'archives.
- **Une pile ou un lien externe ne s'ouvre pas sous Linux :** définissez un navigateur par défaut pour la session de bureau, puis réessayez.
- **Les réglages ou les sessions ne sont pas partagés avec la CLI :** vérifiez que les deux applications utilisent la même valeur de `IAC_CODE_CONFIG_DIR` et le même compte utilisateur du système.

Pour l'installation et les commandes de la CLI, consultez [Installation](./getting-started/installation.md) et [Utilisation de la CLI](./cli/usage.md). Pour l'interface dans le navigateur, consultez l'[Application web](./web-app.md).
