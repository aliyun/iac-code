---
sidebar_position: 1
title: Visão geral dos Skills oficiais do IaC Code
description: Compare os Skills oficiais do IaC Code e escolha a distribuição adequada.
---

# Visão geral dos Skills oficiais do IaC Code

O IaC Code está disponível em três distribuições oficiais de Skill. Todas permitem gerenciar infraestrutura Alibaba
Cloud a partir de um agente, mas diferem no canal de distribuição e em onde o Agent do IaC Code é executado.

## Escolher um Skill

| Skill | Onde é executado | Quando escolher |
|---|---|---|
| `iac-code` | Runtime verificado do IaC Code baixado na sua máquina | Você quer o pacote independente do projeto iac-code e controle direto de instalação e atualizações. |
| `alibabacloud-iac-code` | O mesmo Runtime local, empacotado para o portal Alibaba Cloud Agent Skills | Você gerencia Alibaba Cloud Skills pelo portal ou por `npx skills`. |
| `alibabacloud-ros-agent` | Agent ROS hospedado pelo Alibaba Cloud, chamado pela API ROS StartChat | Você quer uma conversa remota sem baixar o Runtime local do IaC Code. |

`iac-code` e `alibabacloud-iac-code` oferecem a mesma capacidade. Escolha uma distribuição em cada escopo do agente;
instalar ambas adiciona acionamentos sobrepostos, não novos recursos.

`alibabacloud-ros-agent` é uma integração remota separada. Ele pode coexistir com uma distribuição local quando o
usuário precisa escolher explicitamente entre IaC Code local e o Agent ROS hospedado.

## Obter o Skill independente

[Baixar iac-code-skill.zip estável](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Essa distribuição é ideal para instalação gerenciada manualmente. Ela baixa o Runtime no primeiro uso e reutiliza a
configuração de modelo e Alibaba Cloud em `~/.iac-code/`. Consulte
[Instalar e usar o Skill do IaC Code](./skill-integration.md).

## Obter os Skills do portal Alibaba Cloud

Pesquise os nomes exatos no [portal Alibaba Cloud Agent Skills](https://skills.aliyun.com/) ou instale pelo repositório
oficial:

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

Downloads diretos:

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) · [código-fonte](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) · [código-fonte](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` exige Node.js 18 ou posterior e permite escolher interativamente o agente e o escopo. Para um ZIP, extraia
o diretório Skill superior no diretório de usuário ou projeto aceito pelo agente.

## Diferenças de recursos e configuração

As duas distribuições locais oferecem conversas normais e Pipeline, arquitetura, templates ROS/Terraform, custos,
stacks, implantação e confirmações. Elas exigem um modelo configurado e credenciais do Alibaba Cloud quando a tarefa
consulta ou altera recursos.

`alibabacloud-ros-agent` usa `ros:StartChat` para acessar o Agent ROS do Alibaba Cloud. Não exige Runtime local nem
provedor de modelo local, mas usa a identidade Alibaba Cloud do host. Conceda apenas as permissões RAM necessárias;
um cancelamento remoto explícito também usa `ros:StopChat`.

Em qualquer distribuição, revise recursos, região, impacto, preço e permissões antes de aprovar. Não salve credenciais
em `SKILL.md`, prompts ou arquivos do projeto.

## Documentação relacionada

- [Instalar e usar o Skill do IaC Code](./skill-integration.md)
- [Referência de integração para hosts](./skill-host-integration.md)
- [Credenciais do Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
