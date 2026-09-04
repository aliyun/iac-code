---
sidebar_position: 1
title: Visao geral
description: O que o IaC Code faz e por onde comecar.
---

# Visao geral

O IaC Code é um assistente de IA para planejar, gerar, implantar e gerenciar infraestrutura em nuvem. Ele pode ser usado no aplicativo Desktop, no aplicativo Web local, no terminal interativo, em interfaces de automação ou como Skill de outro agente. A arquitetura foi projetada para fluxos multicloud; a versão atual oferece suporte a Alibaba Cloud ROS e Terraform.

Capacidades principais:

- **Diga e obtenha o template** — descreva o que precisa em linguagem natural e obtenha templates ROS validados e prontos para implantacao, ou templates Terraform gerados.
- **Do template a producao** — para o Alibaba Cloud ROS, va do template a infraestrutura em execucao: crie, atualize, exclua e monitore stacks em diferentes regioes. O suporte a Terraform cobre a geracao e a conversao de templates, nao a implantacao.
- **Inteligencia de nuvem integrada** — pesquise documentacao, verifique a disponibilidade de recursos e estime custos antes de implantar; cada decisao respaldada por dados reais da nuvem.

Escolha o ponto de entrada adequado:

- Baixe o [aplicativo Desktop](./desktop-app.md) para uma interface gráfica pronta para uso.
- Siga a [instalação](./getting-started/installation.md) e o [início rápido](./getting-started/quick-start.md) para usar REPL, modo headless ou o [aplicativo Web](./web-app.md) local.
- Escolha uma distribuição na [visão geral dos Skills oficiais do IaC Code](./a2a/skill-overview.md) para adicionar seus recursos de Alibaba Cloud a um agente compatível.
- Use [ACP](./acp/overview.md), [A2A](./a2a/overview.md) ou [AG-UI](./agui/overview.md) para integrar o IaC Code a outro aplicativo ou serviço.

Todos os pontos de entrada exigem um modelo configurado. Configure também as [credenciais do Alibaba Cloud](./configuration/alibaba-cloud-credentials.md) para consultar, alterar ou implantar recursos.
