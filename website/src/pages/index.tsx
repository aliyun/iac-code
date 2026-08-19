import {type ReactNode, useEffect, useRef, useState} from 'react';
import clsx from 'clsx';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import styles from './index.module.css';
import demoEnGif from '@site/static/img/demo_en.gif';
import demoZhGif from '@site/static/img/demo_zh.gif';
import desktopCnScreenshot from '@site/static/img/screenshots/iac-code-desktop-cn.jpg';
import desktopEnScreenshot from '@site/static/img/screenshots/iac-code-desktop-en.jpg';
import webCnScreenshot from '@site/static/img/screenshots/iac-code-web-cn.jpg';
import webEnScreenshot from '@site/static/img/screenshots/iac-code-web-en.jpg';
import whyCnShot1 from '@site/static/img/screenshots/iac-code-desktop-cn-1.jpg';
import whyCnShot2 from '@site/static/img/screenshots/iac-code-desktop-cn-2.jpg';
import whyCnShot3 from '@site/static/img/screenshots/iac-code-desktop-cn-3.jpg';
import whyCnShot4 from '@site/static/img/screenshots/iac-code-desktop-cn-4.jpg';
import whyEnShot1 from '@site/static/img/screenshots/iac-code-desktop-en-1.jpg';
import whyEnShot2 from '@site/static/img/screenshots/iac-code-desktop-en-2.jpg';
import whyEnShot3 from '@site/static/img/screenshots/iac-code-desktop-en-3.jpg';
import whyEnShot4 from '@site/static/img/screenshots/iac-code-desktop-en-4.jpg';

const whyCnShots = [whyCnShot1, whyCnShot2, whyCnShot3, whyCnShot4];
const whyEnShots = [whyEnShot1, whyEnShot2, whyEnShot3, whyEnShot4];

type Locale = 'en' | 'zh-Hans' | 'ja' | 'fr' | 'de' | 'es' | 'pt';
type InstallTarget = 'shell' | 'windows';
type Platform = 'macOS' | 'Windows' | 'Linux';

type InstallCommand = {
  label: string;
  command: string;
};

type HomeCopy = {
  title: string;
  subtitle: string;
  installLabel: string;
  copyCommand: string;
  copiedCommand: string;
  installCommands: Record<InstallTarget, InstallCommand>;
  demoAlt: string;
  sections: Array<{title: string; body: string}>;
  workflowEyebrow: string;
  workflowTitle: string;
  workflowSteps: Array<{title: string; body: string}>;
};

const installCommand = 'pip install iac-code';

const desktopDownloads: Record<Platform, string> = {
  macOS:
    'https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg',
  Windows:
    'https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe',
  Linux:
    'https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage',
};

type DemoEvent = {
  kind: 'prompt' | 'thought' | 'tool' | 'message' | 'confirm' | 'status' | 'success' | 'plain' | 'selected' | 'shell';
  title: ReactNode;
  detail?: ReactNode;
};

type VisualKey =
  | 'naturalLanguage'
  | 'iacEngines'
  | 'aiProviders'
  | 'agentWorkflow'
  | 'interactiveMode'
  | 'headlessMode';

type TerminalScene = {
  title: string;
  variant?: 'repl' | 'menu' | 'workflow' | 'shell';
  events: DemoEvent[];
};

type HomepageItem = {
  visual?: VisualKey;
  screenshot?: 'web' | 'desktop';
  title: string;
  body: string;
  command?: string;
  alt?: string;
};

type HomepageDraft = {
  whyTitle: string;
  whyItems: HomepageItem[];
  howTitle: string;
  howItems: HomepageItem[];
  downloadLabel: string;
  otherPlatformsLabel: string;
  pipHint: string;
  surfacesToggleLabel: string;
  shotTabDesktop: string;
  shotTabTerminal: string;
};

type TerminalTerms = {
  thought: string;
  skillLoaded: string;
  skill: string;
  read: string;
  readLines: string;
  write: string;
  update: string;
  wroteLines: string;
  editedFile: string;
  callSucceeded: string;
  aliyunApi: string;
  expand: string;
  selectProvider: string;
  thirdParty: string;
  alibabaCloud: string;
  zhipuAI: string;
  kimi: string;
  miniMax: string;
  volcengine: string;
  siliconFlow: string;
  deepSeek: string;
  openAI: string;
  anthropic: string;
  googleGemini: string;
  azureOpenAI: string;
  openRouter: string;
  local: string;
  compatible: string;
  current: string;
  input: string;
  output: string;
  exitCode: string;
};

const homepageDrafts: Record<Locale, HomepageDraft> = {
  en: {
    whyTitle: 'Why IaC Code?',
    whyItems: [
      {
        title: 'Manage cloud infrastructure with natural language',
        body: 'Bring resource planning, template generation, change review, and deployment operations into one terminal conversation.',
      },
      {
        title: 'Support multiple IaC engines',
        body: 'Use Terraform and Alibaba Cloud ROS in one workflow to produce reviewable, executable infrastructure changes.',
      },
      {
        title: 'Support multiple AI providers',
        body: 'Connect multiple model providers so teams can choose AI capabilities by model, budget, and compliance needs.',
      },
      {
        title: 'Agentic workflow',
        body: 'Let the agent understand context, call tools, inspect results, and settle deliverable infrastructure changes.',
      },
    ],
    howTitle: 'Start using IaC Code',
    downloadLabel: 'Download for {os}',
    otherPlatformsLabel: 'Other platforms: ',
    pipHint: 'All three run from the command line — install first:',
    surfacesToggleLabel: 'Other usage modes: ',
    shotTabDesktop: 'Desktop',
    shotTabTerminal: 'Terminal',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'Interactive mode',
        body: 'Start a terminal session, refine requirements, review templates, run tools, and complete infrastructure tasks across turns.',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'Headless mode',
        body: 'Pass a single prompt to IaC Code for scripts, pipelines, and automation systems.',
        command: 'iac-code --prompt "Create a VPC" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Web app',
        body: 'Launch the local Web app from the CLI and work in a browser with the full conversational IaC workspace.',
        command: 'iac-code web',
        alt: 'IaC Code Web app',
      },
      {
        screenshot: 'desktop',
        title: 'Desktop app',
        body: 'Install the native app on macOS, Windows, or Linux for an integrated desktop experience and signed updates.',
        alt: 'IaC Code Desktop app',
      },
    ],
  },
  'zh-Hans': {
    whyTitle: '为什么选择 IaC Code？',
    whyItems: [
      {
        title: '使用自然语言管理基础设施',
        body: '把资源规划、模板生成、变更审阅和部署操作放进同一个终端对话流。',
      },
      {
        title: '支持多种 IaC 引擎',
        body: '支持 Terraform 和阿里云 ROS 等多种 IaC 引擎，按目标工作流组织可审阅、可执行的基础设施变更。',
      },
      {
        title: '支持多种 AI 供应商',
        body: '可接入多种模型提供商，让团队按自己的模型、预算和合规要求选择 AI 能力。',
      },
      {
        title: '智能体工作流',
        body: '让智能体理解上下文、调用工具、检查结果，并把可交付的基础设施变更沉淀下来。',
      },
    ],
    howTitle: '开始使用 IaC Code',
    downloadLabel: '下载 {os} 版',
    otherPlatformsLabel: '其他平台：',
    pipHint: '以上三种方式通过命令行运行，先安装：',
    surfacesToggleLabel: '其他使用方式：',
    shotTabDesktop: '桌面版',
    shotTabTerminal: '终端版',
    howItems: [
      {
        visual: 'interactiveMode',
        title: '交互模式',
        body: '启动终端会话，持续补充需求、审阅模板、执行工具，并在多轮对话中完成基础设施任务。',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: '无头模式',
        body: '把单次提示词交给 IaC Code，适合脚本、流水线和自动化系统调用。',
        command: 'iac-code --prompt "创建一个 VPC" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Web 应用',
        body: '通过 CLI 启动本地 Web 应用，在浏览器中使用完整的对话式 IaC 工作空间。',
        command: 'iac-code web',
        alt: 'IaC Code Web 应用',
      },
      {
        screenshot: 'desktop',
        title: '桌面应用',
        body: '在 macOS、Windows 或 Linux 上安装原生应用，获得一体化桌面体验和签名更新。',
        alt: 'IaC Code 桌面应用',
      },
    ],
  },
  ja: {
    whyTitle: 'IaC Code を選ぶ理由',
    whyItems: [
      {
        title: '自然言語でクラウドインフラを管理',
        body: 'リソース設計、テンプレート生成、変更レビュー、デプロイ操作を 1 つのターミナル対話にまとめます。',
      },
      {
        title: '複数の IaC エンジンに対応',
        body: 'Terraform と Alibaba Cloud ROS を同じワークフローで扱い、レビュー可能で実行可能なインフラ変更を作成します。',
      },
      {
        title: '複数の AI プロバイダーに対応',
        body: '複数のモデルプロバイダーに接続し、モデル、予算、コンプライアンス要件に応じて AI 能力を選べます。',
      },
      {
        title: 'エージェントワークフロー',
        body: 'エージェントがコンテキストを理解し、ツールを呼び出し、結果を確認して、引き渡せるインフラ変更にまとめます。',
      },
    ],
    howTitle: 'IaC Code を使い始める',
    downloadLabel: '{os} 版をダウンロード',
    otherPlatformsLabel: 'その他のプラットフォーム：',
    pipHint: '上記の3つの方法はいずれもコマンドラインで実行します。先にインストール：',
    surfacesToggleLabel: 'その他の利用方法：',
    shotTabDesktop: 'デスクトップ版',
    shotTabTerminal: 'ターミナル版',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'インタラクティブモード',
        body: 'ターミナルセッションを開始し、要件を追加しながらテンプレートをレビューし、ツールを実行してインフラタスクを進めます。',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'ヘッドレスモード',
        body: '単発プロンプトを IaC Code に渡し、スクリプト、パイプライン、自動化システムから実行できます。',
        command: 'iac-code --prompt "VPCを作成" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Web アプリ',
        body: 'CLI からローカル Web アプリを起動し、ブラウザーで対話型 IaC ワークスペースの全機能を利用できます。',
        command: 'iac-code web',
        alt: 'IaC Code Web アプリ',
      },
      {
        screenshot: 'desktop',
        title: 'デスクトップアプリ',
        body: 'macOS、Windows、Linux にネイティブアプリをインストールし、統合された操作環境と署名付き更新を利用できます。',
        alt: 'IaC Code デスクトップアプリ',
      },
    ],
  },
  fr: {
    whyTitle: 'Pourquoi choisir IaC Code ?',
    whyItems: [
      {
        title: 'Gérer l’infrastructure cloud en langage naturel',
        body: 'Regroupez planification des ressources, génération de templates, revue des changements et opérations de déploiement dans une conversation terminal.',
      },
      {
        title: 'Prendre en charge plusieurs moteurs IaC',
        body: 'Utilisez Terraform et Alibaba Cloud ROS dans un même workflow pour produire des changements d’infrastructure révisables et exécutables.',
      },
      {
        title: 'Prendre en charge plusieurs fournisseurs IA',
        body: 'Connectez plusieurs fournisseurs de modèles pour choisir les capacités IA selon le modèle, le budget et les exigences de conformité.',
      },
      {
        title: 'Workflow agentique',
        body: 'Laissez l’agent comprendre le contexte, appeler les outils, inspecter les résultats et stabiliser des changements d’infrastructure livrables.',
      },
    ],
    howTitle: 'Commencer avec IaC Code',
    downloadLabel: 'Télécharger pour {os}',
    otherPlatformsLabel: 'Autres plateformes : ',
    pipHint: 'Ces trois modes s’exécutent en ligne de commande — installez d’abord :',
    surfacesToggleLabel: 'Autres modes d’utilisation : ',
    shotTabDesktop: 'Bureau',
    shotTabTerminal: 'Terminal',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'Mode interactif',
        body: 'Démarrez une session terminal, affinez les exigences, révisez les templates, exécutez les outils et terminez les tâches d’infrastructure en plusieurs tours.',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'Mode headless',
        body: 'Transmettez un prompt unique à IaC Code pour les scripts, les pipelines et les systèmes d’automatisation.',
        command: 'iac-code --prompt "Créer un VPC" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Application web',
        body: 'Lancez l’application web locale depuis la CLI et retrouvez dans le navigateur tout l’espace de travail IaC conversationnel.',
        command: 'iac-code web',
        alt: 'Application web IaC Code',
      },
      {
        screenshot: 'desktop',
        title: 'Application de bureau',
        body: 'Installez l’application native sur macOS, Windows ou Linux pour une expérience intégrée et des mises à jour signées.',
        alt: 'Application de bureau IaC Code',
      },
    ],
  },
  de: {
    whyTitle: 'Warum IaC Code?',
    whyItems: [
      {
        title: 'Cloud-Infrastruktur mit natürlicher Sprache verwalten',
        body: 'Bringen Sie Ressourcenplanung, Template-Generierung, Änderungsreview und Deployment-Vorgänge in eine Terminal-Konversation.',
      },
      {
        title: 'Mehrere IaC-Engines unterstützen',
        body: 'Nutzen Sie Terraform und Alibaba Cloud ROS in einem Workflow, um prüfbare und ausführbare Infrastrukturänderungen zu erzeugen.',
      },
      {
        title: 'Mehrere KI-Anbieter unterstützen',
        body: 'Binden Sie mehrere Modellanbieter an, damit Teams KI-Fähigkeiten nach Modell, Budget und Compliance-Anforderungen wählen können.',
      },
      {
        title: 'Agentischer Workflow',
        body: 'Der Agent versteht Kontext, ruft Tools auf, prüft Ergebnisse und verdichtet alles zu lieferbaren Infrastrukturänderungen.',
      },
    ],
    howTitle: 'Mit IaC Code starten',
    downloadLabel: 'Für {os} herunterladen',
    otherPlatformsLabel: 'Andere Plattformen: ',
    pipHint: 'Alle drei Varianten laufen über die Kommandozeile – zuerst installieren:',
    surfacesToggleLabel: 'Weitere Nutzungsarten: ',
    shotTabDesktop: 'Desktop',
    shotTabTerminal: 'Terminal',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'Interaktiver Modus',
        body: 'Starten Sie eine Terminal-Sitzung, verfeinern Sie Anforderungen, prüfen Sie Templates, führen Sie Tools aus und erledigen Sie Infrastrukturaufgaben über mehrere Runden.',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'Headless-Modus',
        body: 'Übergeben Sie IaC Code einen einzelnen Prompt für Skripte, Pipelines und Automatisierungssysteme.',
        command: 'iac-code --prompt "VPC erstellen" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Web-App',
        body: 'Starten Sie die lokale Web-App über die CLI und nutzen Sie den vollständigen dialogorientierten IaC-Arbeitsbereich im Browser.',
        command: 'iac-code web',
        alt: 'IaC Code Web-App',
      },
      {
        screenshot: 'desktop',
        title: 'Desktop-App',
        body: 'Installieren Sie die native App unter macOS, Windows oder Linux für eine integrierte Desktop-Umgebung und signierte Updates.',
        alt: 'IaC Code Desktop-App',
      },
    ],
  },
  es: {
    whyTitle: '¿Por qué elegir IaC Code?',
    whyItems: [
      {
        title: 'Gestiona infraestructura cloud con lenguaje natural',
        body: 'Lleva planificación de recursos, generación de plantillas, revisión de cambios y operaciones de despliegue a una conversación de terminal.',
      },
      {
        title: 'Compatible con múltiples motores IaC',
        body: 'Usa Terraform y Alibaba Cloud ROS en un mismo flujo para producir cambios de infraestructura revisables y ejecutables.',
      },
      {
        title: 'Compatible con múltiples proveedores de IA',
        body: 'Conecta varios proveedores de modelos para que el equipo elija capacidades de IA según modelo, presupuesto y requisitos de cumplimiento.',
      },
      {
        title: 'Flujo de trabajo con agente',
        body: 'Deja que el agente entienda el contexto, invoque herramientas, inspeccione resultados y consolide cambios de infraestructura entregables.',
      },
    ],
    howTitle: 'Empieza a usar IaC Code',
    downloadLabel: 'Descargar para {os}',
    otherPlatformsLabel: 'Otras plataformas: ',
    pipHint: 'Las tres formas se ejecutan desde la línea de comandos; instala primero:',
    surfacesToggleLabel: 'Otros modos de uso: ',
    shotTabDesktop: 'Escritorio',
    shotTabTerminal: 'Terminal',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'Modo interactivo',
        body: 'Inicia una sesión de terminal, refina requisitos, revisa plantillas, ejecuta herramientas y completa tareas de infraestructura en varias rondas.',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'Modo headless',
        body: 'Envía un único prompt a IaC Code para scripts, pipelines y sistemas de automatización.',
        command: 'iac-code --prompt "Crear una VPC" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Aplicación web',
        body: 'Inicia la aplicación web local desde la CLI y utiliza en el navegador todo el espacio de trabajo conversacional de IaC.',
        command: 'iac-code web',
        alt: 'Aplicación web de IaC Code',
      },
      {
        screenshot: 'desktop',
        title: 'Aplicación de escritorio',
        body: 'Instala la aplicación nativa en macOS, Windows o Linux para disfrutar de una experiencia integrada y actualizaciones firmadas.',
        alt: 'Aplicación de escritorio de IaC Code',
      },
    ],
  },
  pt: {
    whyTitle: 'Por que escolher IaC Code?',
    whyItems: [
      {
        title: 'Gerencie infraestrutura em nuvem com linguagem natural',
        body: 'Leve planejamento de recursos, geração de templates, revisão de mudanças e operações de implantação para uma conversa no terminal.',
      },
      {
        title: 'Suporte a múltiplos motores IaC',
        body: 'Use Terraform e Alibaba Cloud ROS no mesmo fluxo para produzir mudanças de infraestrutura revisáveis e executáveis.',
      },
      {
        title: 'Suporte a múltiplos provedores de IA',
        body: 'Conecte vários provedores de modelos para que a equipe escolha capacidades de IA por modelo, orçamento e requisitos de conformidade.',
      },
      {
        title: 'Fluxo de agente',
        body: 'Deixe o agente entender o contexto, chamar ferramentas, inspecionar resultados e consolidar mudanças de infraestrutura entregáveis.',
      },
    ],
    howTitle: 'Comece a usar IaC Code',
    downloadLabel: 'Baixar para {os}',
    otherPlatformsLabel: 'Outras plataformas: ',
    pipHint: 'As três formas rodam pela linha de comando — instale primeiro:',
    surfacesToggleLabel: 'Outras formas de uso: ',
    shotTabDesktop: 'Desktop',
    shotTabTerminal: 'Terminal',
    howItems: [
      {
        visual: 'interactiveMode',
        title: 'Modo interativo',
        body: 'Inicie uma sessão no terminal, refine requisitos, revise templates, execute ferramentas e conclua tarefas de infraestrutura em várias rodadas.',
        command: 'iac-code',
      },
      {
        visual: 'headlessMode',
        title: 'Modo headless',
        body: 'Envie um único prompt ao IaC Code para scripts, pipelines e sistemas de automação.',
        command: 'iac-code --prompt "Criar uma VPC" --output-format stream-json',
      },
      {
        screenshot: 'web',
        title: 'Aplicativo web',
        body: 'Inicie o aplicativo web local pela CLI e use no navegador todo o espaço de trabalho conversacional de IaC.',
        command: 'iac-code web',
        alt: 'Aplicativo web do IaC Code',
      },
      {
        screenshot: 'desktop',
        title: 'Aplicativo para desktop',
        body: 'Instale o aplicativo nativo no macOS, Windows ou Linux para ter uma experiência integrada e atualizações assinadas.',
        alt: 'Aplicativo para desktop do IaC Code',
      },
    ],
  },
};

const terminalTerms: Record<Locale, TerminalTerms> = {
  en: {
    thought: 'Thought for {seconds:.1f}s',
    skillLoaded: "Skill '{name}' loaded (inline).",
    skill: 'Skill',
    read: 'Read',
    readLines: 'Read {total} lines',
    write: 'Write',
    update: 'Update',
    wroteLines: 'Successfully wrote {lines} lines to {path}',
    editedFile: 'Successfully edited {path}',
    callSucceeded: 'Call succeeded',
    aliyunApi: 'Aliyun API',
    expand: '(ctrl+o to expand)',
    selectProvider: 'Select provider',
    thirdParty: 'Third-party',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'Local',
    compatible: 'Compatible',
    current: 'current',
    input: 'input',
    output: 'output',
    exitCode: 'Exit code',
  },
  'zh-Hans': {
    thought: '思考完成（耗时 {seconds:.1f}s）',
    skillLoaded: "技能 '{name}' 已加载（内联）。",
    skill: '技能',
    read: '读取',
    readLines: '读取了 {total} 行',
    write: '写入',
    update: '更新',
    wroteLines: '成功写入 {lines} 行到 {path}',
    editedFile: '已成功编辑 {path}',
    callSucceeded: '调用成功',
    aliyunApi: '阿里云 API',
    expand: '(ctrl+o 展开)',
    selectProvider: '选择提供商',
    thirdParty: '第三方',
    alibabaCloud: '阿里云',
    zhipuAI: '智谱 AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: '火山引擎',
    siliconFlow: '硅基流动',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: '本地模型',
    compatible: '兼容模式',
    current: '当前',
    input: 'input',
    output: 'output',
    exitCode: 'Exit code',
  },
  ja: {
    thought: '{seconds:.1f} 秒考えました',
    skillLoaded: "スキル '{name}' を読み込みました（インライン）。",
    skill: 'スキル',
    read: '読み取り',
    readLines: '{total} 行読み取りました',
    write: '書き込み',
    update: '更新',
    wroteLines: '{path} へ {lines} 行の書き込みに成功しました',
    editedFile: '{path} の編集に成功しました',
    callSucceeded: '呼び出しに成功しました',
    aliyunApi: 'Aliyun API',
    expand: '（ctrl+o で展開）',
    selectProvider: 'プロバイダーを選択',
    thirdParty: 'サードパーティ',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'ローカル',
    compatible: '互換モード',
    current: '現在',
    input: '入力',
    output: '出力',
    exitCode: '終了コード',
  },
  fr: {
    thought: 'Réflexion pendant {seconds:.1f}s',
    skillLoaded: 'Skill « {name} » chargé (inline).',
    skill: 'Skill',
    read: 'Lecture',
    readLines: '{total} lignes lues',
    write: 'Écriture',
    update: 'Mettre à jour',
    wroteLines: '{lines} lignes écrites avec succès dans {path}',
    editedFile: '{path} modifié avec succès',
    callSucceeded: 'Appel réussi',
    aliyunApi: 'Aliyun API',
    expand: '(ctrl+o pour développer)',
    selectProvider: 'Sélectionner le fournisseur',
    thirdParty: 'Tiers',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'Local',
    compatible: 'Compatible',
    current: 'actuel',
    input: 'entrée',
    output: 'sortie',
    exitCode: 'Code de sortie',
  },
  de: {
    thought: 'Nachgedacht für {seconds:.1f}s',
    skillLoaded: "Skill '{name}' geladen (inline).",
    skill: 'Skill',
    read: 'Lesen',
    readLines: '{total} Zeilen gelesen',
    write: 'Schreiben',
    update: 'Aktualisieren',
    wroteLines: '{lines} Zeilen erfolgreich nach {path} geschrieben',
    editedFile: '{path} erfolgreich bearbeitet',
    callSucceeded: 'Aufruf erfolgreich',
    aliyunApi: 'Aliyun API',
    expand: '(ctrl+o zum Aufklappen)',
    selectProvider: 'Anbieter auswählen',
    thirdParty: 'Drittanbieter',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'Lokal',
    compatible: 'Kompatibel',
    current: 'aktuell',
    input: 'Input',
    output: 'Output',
    exitCode: 'Exit-Code',
  },
  es: {
    thought: 'Razonamiento durante {seconds:.1f} s',
    skillLoaded: "Skill '{name}' cargado (en línea).",
    skill: 'Skill',
    read: 'Leer',
    readLines: 'Leídas {total} líneas',
    write: 'Escribir',
    update: 'Actualizar',
    wroteLines: 'Se escribieron correctamente {lines} líneas en {path}',
    editedFile: '{path} editado correctamente',
    callSucceeded: 'Llamada correcta',
    aliyunApi: 'Aliyun API',
    expand: '(ctrl+o para expandir)',
    selectProvider: 'Seleccionar proveedor',
    thirdParty: 'Terceros',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'Local',
    compatible: 'Compatible',
    current: 'actual',
    input: 'entrada',
    output: 'salida',
    exitCode: 'Código de salida',
  },
  pt: {
    thought: 'Raciocínio por {seconds:.1f}s',
    skillLoaded: "Skill '{name}' carregada (inline).",
    skill: 'Skill',
    read: 'Ler',
    readLines: '{total} linhas lidas',
    write: 'Gravar',
    update: 'Atualizar',
    wroteLines: '{lines} linhas gravadas com sucesso em {path}',
    editedFile: '{path} editado com sucesso',
    callSucceeded: 'Chamada bem-sucedida',
    aliyunApi: 'Aliyun API',
    expand: '(ctrl+o para expandir)',
    selectProvider: 'Selecionar provedor',
    thirdParty: 'Terceiros',
    alibabaCloud: 'Alibaba Cloud',
    zhipuAI: 'ZhiPu AI',
    kimi: 'Kimi',
    miniMax: 'MiniMax',
    volcengine: 'Volcengine',
    siliconFlow: 'SiliconFlow',
    deepSeek: 'DeepSeek',
    openAI: 'OpenAI',
    anthropic: 'Anthropic',
    googleGemini: 'Google Gemini',
    azureOpenAI: 'Azure OpenAI',
    openRouter: 'OpenRouter',
    local: 'Local',
    compatible: 'Compatível',
    current: 'atual',
    input: 'entrada',
    output: 'saída',
    exitCode: 'Código de saída',
  },
};

function tokenReplace(template: string, values: Record<string, string | number>) {
  return Object.entries(values).reduce(
    (result, [key, value]) => result.replace(new RegExp(`\\{${key}(?::[^}]+)?\\}`, 'g'), String(value)),
    template,
  );
}

function row(kind: DemoEvent['kind'], title: ReactNode, detail?: ReactNode): DemoEvent {
  return {kind, title, detail};
}

function paramTable(columns: [ReactNode, ReactNode, ReactNode], header = false) {
  return (
    <span className={clsx(styles.terminalTable, styles.terminalTableParams, header && styles.terminalTableHeader)}>
      <span>{columns[0]}</span>
      <span>{columns[1]}</span>
      <span>{columns[2]}</span>
    </span>
  );
}

function outputTable(columns: [ReactNode, ReactNode], header = false) {
  return (
    <span className={clsx(styles.terminalTable, styles.terminalTableOutput, header && styles.terminalTableHeader)}>
      <span>{columns[0]}</span>
      <span>{columns[1]}</span>
    </span>
  );
}

function getTerminalScenes(locale: Locale): Record<VisualKey, TerminalScene> {
  const t = terminalTerms[locale] ?? terminalTerms.en;
  const isZh = locale === 'zh-Hans';
  const readLines = (total: number) => tokenReplace(t.readLines, {total});
  const thought = (seconds: string) => tokenReplace(t.thought, {seconds});
  const skillLoaded = tokenReplace(t.skillLoaded, {name: 'iac-aliyun'});
  const wroteLines = (lines: number, path: string) => tokenReplace(t.wroteLines, {lines, path});
  const editedFile = (path: string) => tokenReplace(t.editedFile, {path});
  const expand = t.expand;
  const currentProvider = `${t.alibabaCloud} (${t.current})`;
  const prompts = {
    natural: {
      en: 'create a vpc',
      'zh-Hans': '创建vpc',
      ja: 'VPCを作成',
      fr: 'créer un VPC',
      de: 'VPC erstellen',
      es: 'crear una VPC',
      pt: 'criar uma VPC',
    }[locale],
    terraform: {
      en: 'Create a VPC with Terraform',
      'zh-Hans': '使用 Terraform 创建 VPC',
      ja: 'Terraform で VPC を作成',
      fr: 'Créer un VPC avec Terraform',
      de: 'VPC mit Terraform erstellen',
      es: 'Crear una VPC con Terraform',
      pt: 'Criar uma VPC com Terraform',
    }[locale],
    headless: homepageDrafts[locale].howItems[1].command ?? homepageDrafts.en.howItems[1].command ?? '',
  };
  const headlessFirst = prompts.headless.replace(/ --output-format stream-json$/, '');
  const firstOutput = isZh
    ? {id: '实例 ID', publicIp: '公网 IP', privateIp: '内网 IP', zone: '可用区', spec: '规格', image: '镜像', disk: '系统盘'}
    : {
        id: locale === 'ja' ? 'インスタンス ID' : 'Instance ID',
        publicIp: locale === 'ja' ? 'パブリック IP' : locale === 'fr' ? 'IP publique' : locale === 'de' ? 'Öffentliche IP' : locale === 'es' ? 'IP pública' : locale === 'pt' ? 'IP público' : 'Public IP',
        privateIp: locale === 'ja' ? 'プライベート IP' : locale === 'fr' ? 'IP privée' : locale === 'de' ? 'Private IP' : locale === 'es' ? 'IP privada' : locale === 'pt' ? 'IP privado' : 'Private IP',
        zone: locale === 'ja' ? 'ゾーン' : locale === 'fr' ? 'Zone' : locale === 'de' ? 'Zone' : locale === 'es' ? 'Zona' : locale === 'pt' ? 'Zona' : 'Zone',
        spec: locale === 'ja' ? 'スペック' : locale === 'fr' ? 'Configuration' : locale === 'de' ? 'Spezifikation' : locale === 'es' ? 'Especificación' : locale === 'pt' ? 'Especificação' : 'Spec',
        image: locale === 'ja' ? 'イメージ' : locale === 'fr' ? 'Image' : locale === 'de' ? 'Image' : locale === 'es' ? 'Imagen' : locale === 'pt' ? 'Imagem' : 'Image',
        disk: locale === 'ja' ? 'システムディスク' : locale === 'fr' ? 'Disque système' : locale === 'de' ? 'Systemdisk' : locale === 'es' ? 'Disco del sistema' : locale === 'pt' ? 'Disco do sistema' : 'System disk',
      };
  const workflowCopy = {
    queried: {
      en: 'Found all available resources. Selected these parameters for you:',
      'zh-Hans': '已查询到所有可用资源。为您选定以下参数：',
      ja: '利用可能なすべてのリソースを照会しました。以下のパラメータを選択しました：',
      fr: 'Toutes les ressources disponibles ont été trouvées. Paramètres sélectionnés :',
      de: 'Alle verfügbaren Ressourcen wurden gefunden. Diese Parameter wurden ausgewählt:',
      es: 'Se encontraron todos los recursos disponibles. Parámetros seleccionados:',
      pt: 'Todos os recursos disponíveis foram encontrados. Parâmetros selecionados:',
    }[locale],
    param: {en: 'Parameter', 'zh-Hans': '参数', ja: 'パラメータ', fr: 'Paramètre', de: 'Parameter', es: 'Parámetro', pt: 'Parâmetro'}[locale],
    value: {en: 'Value', 'zh-Hans': '值', ja: '値', fr: 'Valeur', de: 'Wert', es: 'Valor', pt: 'Valor'}[locale],
    description: {en: 'Description', 'zh-Hans': '说明', ja: '説明', fr: 'Description', de: 'Beschreibung', es: 'Descripción', pt: 'Descrição'}[locale],
    zone: {en: 'Zone', 'zh-Hans': '可用区', ja: 'ゾーン', fr: 'Zone', de: 'Zone', es: 'Zona', pt: 'Zona'}[locale],
    spec: {en: 'Instance spec', 'zh-Hans': '实例规格', ja: 'インスタンス仕様', fr: 'Type d’instance', de: 'Instanztyp', es: 'Tipo de instancia', pt: 'Tipo da instância'}[locale],
    image: {en: 'Image', 'zh-Hans': '镜像', ja: 'イメージ', fr: 'Image', de: 'Image', es: 'Imagen', pt: 'Imagem'}[locale],
    diskType: {en: 'System disk type', 'zh-Hans': '系统盘类型', ja: 'システムディスク種別', fr: 'Type de disque système', de: 'Systemdisk-Typ', es: 'Tipo de disco del sistema', pt: 'Tipo do disco do sistema'}[locale],
    inStock: {en: 'ecs.g7.large in stock', 'zh-Hans': 'ecs.g7.large 有库存', ja: 'ecs.g7.large 在庫あり', fr: 'ecs.g7.large en stock', de: 'ecs.g7.large verfügbar', es: 'ecs.g7.large disponible', pt: 'ecs.g7.large disponível'}[locale],
    general: {en: '2vCPU / 8GB general purpose', 'zh-Hans': '2vCPU / 8GB 通用型', ja: '2vCPU / 8GB 汎用型', fr: '2 vCPU / 8 Go usage général', de: '2 vCPU / 8 GB Allzweck', es: '2 vCPU / 8 GB uso general', pt: '2 vCPU / 8 GB uso geral'}[locale],
    linux: {en: 'Alibaba Cloud Linux 3.2104 LTS 64-bit', 'zh-Hans': 'Alibaba Cloud Linux 3.2104 LTS 64位', ja: 'Alibaba Cloud Linux 3.2104 LTS 64ビット', fr: 'Alibaba Cloud Linux 3.2104 LTS 64 bits', de: 'Alibaba Cloud Linux 3.2104 LTS 64 Bit', es: 'Alibaba Cloud Linux 3.2104 LTS 64 bits', pt: 'Alibaba Cloud Linux 3.2104 LTS 64 bits'}[locale],
    essd: {en: 'ESSD cloud disk', 'zh-Hans': 'ESSD 云盘', ja: 'ESSD クラウドディスク', fr: 'Disque cloud ESSD', de: 'ESSD-Cloud-Disk', es: 'Disco cloud ESSD', pt: 'Disco em nuvem ESSD'}[locale],
    confirm: {en: 'Confirm deployment to cn-beijing?', 'zh-Hans': '确认部署到 cn-beijing?', ja: 'cn-beijing にデプロイしますか？', fr: 'Confirmer le déploiement vers cn-beijing ?', de: 'Deployment nach cn-beijing bestätigen?', es: '¿Confirmar despliegue en cn-beijing?', pt: 'Confirmar implantação em cn-beijing?'}[locale],
    processed: {en: 'Processed 1m 24s', 'zh-Hans': '已处理 1m 24s', ja: '処理済み 1分24秒', fr: 'Traité 1 min 24 s', de: 'Verarbeitet 1m 24s', es: 'Procesado 1m 24s', pt: 'Processado 1m 24s'}[locale],
    created: {en: 'created successfully', 'zh-Hans': '创建完成', ja: '作成完了', fr: 'créée', de: 'erstellt', es: 'creada', pt: 'criada'}[locale],
    deploymentSuccess: {
      en: 'Deployment succeeded ✅ Stack my-ecs-stack was created in cn-beijing (27s).',
      'zh-Hans': '部署成功 ✅ 栈 my-ecs-stack 已在 cn-beijing 创建完成（耗时 27 秒）。',
      ja: 'デプロイ成功 ✅ スタック my-ecs-stack は cn-beijing に作成されました（27秒）。',
      fr: 'Déploiement réussi ✅ La pile my-ecs-stack a été créée dans cn-beijing (27 s).',
      de: 'Deployment erfolgreich ✅ Stack my-ecs-stack wurde in cn-beijing erstellt (27 s).',
      es: 'Despliegue correcto ✅ El stack my-ecs-stack se creó en cn-beijing (27 s).',
      pt: 'Implantação bem-sucedida ✅ A pilha my-ecs-stack foi criada em cn-beijing (27 s).',
    }[locale],
    queryOutput: {en: 'Query output:', 'zh-Hans': '查询输出信息：', ja: '出力情報を照会：', fr: 'Informations de sortie :', de: 'Ausgabeinformationen:', es: 'Información de salida:', pt: 'Informações de saída:'}[locale],
    ecsCreated: {en: 'ECS instance created successfully. Details:', 'zh-Hans': 'ECS 实例已创建成功，详情如下：', ja: 'ECS インスタンスの作成に成功しました。詳細：', fr: 'Instance ECS créée avec succès. Détails :', de: 'ECS-Instanz erfolgreich erstellt. Details:', es: 'Instancia ECS creada correctamente. Detalles:', pt: 'Instância ECS criada com sucesso. Detalhes:'}[locale],
    item: {en: 'Item', 'zh-Hans': '项目', ja: '項目', fr: 'Élément', de: 'Element', es: 'Elemento', pt: 'Item'}[locale],
  };
  const surfaceCopy = {
    sessionStarted: {
      en: 'IaC Code interactive session started',
      'zh-Hans': 'IaC Code 交互会话已启动',
      ja: 'IaC Code の対話セッションを開始しました',
      fr: 'Session interactive IaC Code démarrée',
      de: 'Interaktive IaC-Code-Sitzung gestartet',
      es: 'Sesión interactiva de IaC Code iniciada',
      pt: 'Sessão interativa do IaC Code iniciada',
    }[locale],
    createVpcEcs: {
      en: 'Create a VPC + 2 ECS instances',
      'zh-Hans': '创建一个 VPC + 2 台 ECS',
      ja: 'VPC 1 個 + ECS 2 台を作成',
      fr: 'Créer un VPC + 2 instances ECS',
      de: 'Eine VPC + 2 ECS-Instanzen erstellen',
      es: 'Crear una VPC + 2 instancias ECS',
      pt: 'Criar uma VPC + 2 instâncias ECS',
    }[locale],
    generatedTemplate: {
      en: 'Generated template',
      'zh-Hans': '生成模板',
      ja: 'テンプレート生成',
      fr: 'Modèle généré',
      de: 'Vorlage generiert',
      es: 'Plantilla generada',
      pt: 'Modelo gerado',
    }[locale],
    validationPassed: {
      en: 'Validation passed',
      'zh-Hans': '校验通过',
      ja: '検証に成功',
      fr: 'Validation réussie',
      de: 'Validierung bestanden',
      es: 'Validación correcta',
      pt: 'Validação aprovada',
    }[locale],
    waitingConfirm: {
      en: ', waiting for deploy confirmation…',
      'zh-Hans': '，等待确认部署…',
      ja: '、デプロイ確認を待機中…',
      fr: ', en attente de confirmation du déploiement…',
      de: ', warte auf Bestätigung der Bereitstellung…',
      es: ', esperando confirmación del despliegue…',
      pt: ', aguardando confirmação da implantação…',
    }[locale],
  };

  return {
    naturalLanguage: {
      title: 'Natural language to ROS',
      events: [
        row('prompt', prompts.natural),
        row('thought', thought('0.7')),
        row('tool', `${t.skill}(iac-aliyun)`, `${skillLoaded}\n4.9k ${t.input} · 76 ${t.output}`),
        row('thought', thought('2.3')),
        row('tool', `${t.read}(vpc.md)`, readLines(54)),
        row('tool', `${t.read}(ros-template.md)`, readLines(155)),
        row('tool', `${t.read}(template-parameters.md)`, `${readLines(206)}\n${expand}`),
        row('thought', thought('4.8')),
        row('tool', `${t.write}(/tmp/vpc-template.yml)`),
      ],
    },
    iacEngines: {
      title: 'ROS and Terraform',
      events: [
        row('prompt', prompts.terraform),
        row('thought', thought('0.9')),
        row('tool', `${t.skill}(iac-aliyun)`, skillLoaded),
        row('thought', thought('1.6')),
        row('tool', `${t.read}(vpc.md)`, readLines(54)),
        row('tool', `${t.read}(terraform-template.md)`, readLines(101)),
        row('tool', `${t.read}(template-parameters.md)`, `${readLines(206)}\n${expand}`),
        row('thought', thought('5.4')),
        row('tool', 'Bash(mkdir -p /tmp/tf-vpc)', `${t.exitCode}: 0`),
        row('tool', `${t.write}(/tmp/tf-vpc/main.tf)`, wroteLines(32, '/tmp/tf-vpc/main.tf')),
      ],
    },
    aiProviders: {
      title: 'Provider switching',
      variant: 'menu',
      events: [
        row('plain', t.selectProvider),
        row('plain', t.thirdParty),
        row('selected', currentProvider),
        row('plain', t.zhipuAI),
        row('plain', t.kimi),
        row('plain', t.miniMax),
        row('plain', t.volcengine),
        row('plain', t.siliconFlow),
        row('plain', t.deepSeek),
        row('plain', t.openAI),
        row('plain', t.anthropic),
        row('plain', t.googleGemini),
        row('plain', t.azureOpenAI),
        row('plain', t.openRouter),
        row('plain', t.local),
        row('plain', t.compatible),
      ],
    },
    agentWorkflow: {
      title: 'Agent workflow',
      variant: 'workflow',
      events: [
        row('message', workflowCopy.queried),
        row('plain', ''),
        row('plain', paramTable([workflowCopy.param, workflowCopy.value, workflowCopy.description], true)),
        row('plain', <span className={clsx(styles.terminalTableRule, styles.terminalTableRuleParams)} aria-hidden="true" />),
        row('plain', paramTable([workflowCopy.zone, 'cn-beijing-l', workflowCopy.inStock])),
        row('plain', paramTable([workflowCopy.spec, 'ecs.g7.large', workflowCopy.general])),
        row('plain', paramTable([workflowCopy.image, 'aliyun_3_x64_20G_alibase_20260326.vhd', workflowCopy.linux])),
        row('plain', paramTable([workflowCopy.diskType, 'cloud_essd', workflowCopy.essd])),
        row('plain', ''),
        row('plain', workflowCopy.confirm),
        row('thought', workflowCopy.processed),
        row('prompt', 'ok'),
        row('tool', `ROS ${isZh ? '资源栈' : 'Stack'}(CreateStack cn-beijing)`, `my-ecs-stack(f60fb4c6-2fb4-4f68-8fcc-e8cd955df858) ${workflowCopy.created} (27s)\n${expand}`),
        row('success', workflowCopy.deploymentSuccess),
        row('plain', workflowCopy.queryOutput),
        row('tool', `${t.aliyunApi}(GetStack ros cn-beijing)`, `${t.callSucceeded}\n${expand}`),
        row('message', workflowCopy.ecsCreated),
        row('plain', ''),
        row('plain', outputTable([workflowCopy.item, workflowCopy.value], true)),
        row('plain', <span className={styles.terminalTableRule} aria-hidden="true" />),
        row('plain', outputTable([firstOutput.id, <span className={styles.terminalAccent}>i-2ze6c7wo4k2ss4uhs3xz</span>])),
        row('plain', outputTable([firstOutput.publicIp, <span className={styles.terminalAccent}>8.141.21.208</span>])),
        row('plain', outputTable([firstOutput.privateIp, <span className={styles.terminalAccent}>192.168.0.194</span>])),
        row('plain', outputTable([firstOutput.zone, 'cn-beijing-l'])),
        row('plain', outputTable([firstOutput.spec, 'ecs.g7.large (2vCPU / 8GB)'])),
        row('plain', outputTable([firstOutput.image, workflowCopy.linux])),
        row('plain', outputTable([firstOutput.disk, 'cloud_essd 40GB'])),
      ],
    },
    interactiveMode: {
      title: 'Interactive REPL',
      events: [
        row(
          'plain',
          <>
            <span className={styles.termPromptGlyph}>›</span> iac-code
          </>,
        ),
        row('plain', <span className={styles.termDim}>{surfaceCopy.sessionStarted}</span>),
        row(
          'plain',
          <>
            <span className={styles.termPromptGlyph}>›</span> {surfaceCopy.createVpcEcs}
          </>,
        ),
        row(
          'plain',
          <>
            <span className={styles.termHl}>✎ {surfaceCopy.generatedTemplate}</span> vpc-ecs-stack.yml
          </>,
        ),
        row(
          'plain',
          <>
            <span className={styles.termOk}>✓ {surfaceCopy.validationPassed}</span>
            {surfaceCopy.waitingConfirm}
          </>,
        ),
      ],
    },
    headlessMode: {
      title: 'Headless command',
      variant: 'shell',
      events: [
        row(
          'plain',
          <>
            <span className={styles.termPromptGlyph}>$</span> {headlessFirst} \
          </>,
        ),
        row('plain', <>{'\u00A0\u00A0\u00A0\u00A0'}--output-format stream-json</>),
        row('plain', <span className={styles.termDim}>{'{"type":"tool_call","name":"ros_generate",…}'}</span>),
        row('plain', <span className={styles.termDim}>{'{"type":"result","status":"success",…}'}</span>),
      ],
    },
  };
}

const copy = {
  en: {
    title: 'Build cloud infrastructure with IaC Code',
    subtitle: 'From one request to reviewable, executable, deployable cloud infrastructure changes.',
    installLabel: 'Install iac-code',
    copyCommand: 'Copy command',
    copiedCommand: 'Copied',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'iac-code terminal demo',
    sections: [
      {
        title: 'Generate templates',
        body: 'Ask for VPCs, ECS instances, OSS buckets, or complete stacks and get structured ROS or Terraform output.',
      },
      {
        title: 'Review before deploy',
        body: 'Keep generated infrastructure visible in the terminal so teams can inspect, refine, and version the result.',
      },
      {
        title: 'Use cloud context',
        body: 'Use cloud product knowledge, documentation search, and resource-aware guidance in one workflow. Today, IaC Code supports Alibaba Cloud.',
      },
    ],
    workflowEyebrow: 'How it works',
    workflowTitle: 'A coding-agent style loop for infrastructure',
    workflowSteps: [
      {
        title: 'Describe',
        body: 'Start with natural language requirements or pipe a single prompt into automation.',
      },
      {
        title: 'Generate',
        body: 'Let the agent draft templates, parameters, and command-ready infrastructure changes.',
      },
      {
        title: 'Operate',
        body: 'Iterate in the REPL, run checks, and move the final IaC into your delivery workflow.',
      },
    ],
  },
  'zh-Hans': {
    title: '用 IaC Code 构建云基础设施',
    subtitle: '从一句需求，到可审阅、可执行、可部署的云基础设施变更。',
    installLabel: '安装 iac-code',
    copyCommand: '复制命令',
    copiedCommand: '已复制',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'iac-code 终端演示',
    sections: [
      {
        title: '生成模板',
        body: '描述 VPC、ECS、OSS Bucket 或完整资源栈，得到结构化的 ROS 或 Terraform 输出。',
      },
      {
        title: '部署前审阅',
        body: '让生成的基础设施变更留在终端中，方便团队检查、调整并纳入版本管理。',
      },
      {
        title: '理解云上下文',
        body: '把云产品知识、文档搜索和资源相关建议整合进同一个工作流。目前 IaC Code 支持阿里云。',
      },
    ],
    workflowEyebrow: '工作方式',
    workflowTitle: '面向基础设施的编码智能体循环',
    workflowSteps: [
      {
        title: '描述',
        body: '用自然语言提出需求，或把单次提示词接入自动化流程。',
      },
      {
        title: '生成',
        body: '让智能体起草模板、参数和可执行的基础设施变更。',
      },
      {
        title: '操作',
        body: '在 REPL 中迭代、执行检查，并把最终 IaC 纳入交付流程。',
      },
    ],
  },
  ja: {
    title: 'IaC Code でクラウドインフラを構築',
    subtitle: 'ひと言の要件から、レビュー可能で実行可能、デプロイ可能なクラウドインフラ変更へ。',
    installLabel: 'iac-code をインストール',
    copyCommand: 'コマンドをコピー',
    copiedCommand: 'コピー済み',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'iac-code terminal demo',
    sections: [
      {
        title: 'テンプレート生成',
        body: 'VPC、ECS、OSS Bucket、または完全なスタックを依頼し、構造化された ROS / Terraform 出力を得られます。',
      },
      {
        title: 'デプロイ前レビュー',
        body: '生成されたインフラをターミナルで確認し、チームで精査、調整、バージョン管理できます。',
      },
      {
        title: 'クラウド文脈を活用',
        body: 'クラウド製品の知識、ドキュメント検索、リソースに基づく助言を同じワークフローに統合します。現在、IaC Code は Alibaba Cloud をサポートしています。',
      },
    ],
    workflowEyebrow: 'How it works',
    workflowTitle: 'インフラ向け coding-agent ループ',
    workflowSteps: [
      {
        title: '記述',
        body: '自然言語の要件から始めるか、単発プロンプトを自動化に渡します。',
      },
      {
        title: '生成',
        body: 'エージェントがテンプレート、パラメータ、コマンド可能な変更を下書きします。',
      },
      {
        title: '運用',
        body: 'REPL で反復し、チェックを実行して、最終 IaC をデリバリーに組み込みます。',
      },
    ],
  },
  fr: {
    title: 'Construisez une infrastructure cloud avec IaC Code',
    subtitle: 'D’une simple demande à des changements d’infrastructure cloud révisables, exécutables et déployables.',
    installLabel: 'Installer iac-code',
    copyCommand: 'Copier la commande',
    copiedCommand: 'Copié',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'Démonstration terminal iac-code',
    sections: [
      {
        title: 'Générez des templates',
        body: 'Demandez des VPC, instances ECS, buckets OSS ou stacks complets et obtenez une sortie ROS ou Terraform structurée.',
      },
      {
        title: 'Révisez avant déploiement',
        body: 'Gardez l’infrastructure générée visible dans le terminal pour l’inspecter, l’ajuster et la versionner en équipe.',
      },
      {
        title: 'Utilisez le contexte cloud',
        body: 'Intégrez la connaissance des produits cloud, la recherche documentaire et les conseils liés aux ressources dans le même flux. IaC Code prend actuellement en charge Alibaba Cloud.',
      },
    ],
    workflowEyebrow: 'Fonctionnement',
    workflowTitle: 'Une boucle d’agent de code pour l’infrastructure',
    workflowSteps: [
      {
        title: 'Décrire',
        body: 'Démarrez avec des exigences en langage naturel ou envoyez un prompt unique dans l’automatisation.',
      },
      {
        title: 'Générer',
        body: 'L’agent rédige les templates, paramètres et changements d’infrastructure prêts pour la commande.',
      },
      {
        title: 'Opérer',
        body: 'Itérez dans le REPL, lancez les vérifications et intégrez l’IaC final à votre livraison.',
      },
    ],
  },
  de: {
    title: 'Cloud-Infrastruktur mit IaC Code bauen',
    subtitle: 'Von einer Anforderung zu prüfbaren, ausführbaren und deploybaren Cloud-Infrastrukturänderungen.',
    installLabel: 'iac-code installieren',
    copyCommand: 'Befehl kopieren',
    copiedCommand: 'Kopiert',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'iac-code Terminal-Demo',
    sections: [
      {
        title: 'Templates generieren',
        body: 'Fordern Sie VPCs, ECS-Instanzen, OSS-Buckets oder komplette Stacks an und erhalten Sie strukturierte ROS- oder Terraform-Ausgabe.',
      },
      {
        title: 'Vor dem Deployment prüfen',
        body: 'Halten Sie generierte Infrastruktur im Terminal sichtbar, damit Teams sie prüfen, verfeinern und versionieren können.',
      },
      {
        title: 'Cloud-Kontext nutzen',
        body: 'Bringen Sie Cloud-Produktwissen, Dokumentensuche und ressourcenbezogene Empfehlungen in denselben Workflow. IaC Code unterstuetzt derzeit Alibaba Cloud.',
      },
    ],
    workflowEyebrow: 'So funktioniert es',
    workflowTitle: 'Eine Coding-Agent-Schleife für Infrastruktur',
    workflowSteps: [
      {
        title: 'Beschreiben',
        body: 'Beginnen Sie mit natürlichsprachlichen Anforderungen oder leiten Sie einen Prompt in Automatisierung weiter.',
      },
      {
        title: 'Generieren',
        body: 'Der Agent entwirft Templates, Parameter und befehlsbereite Infrastrukturänderungen.',
      },
      {
        title: 'Betreiben',
        body: 'Iterieren Sie im REPL, führen Sie Checks aus und übernehmen Sie die finale IaC in den Lieferprozess.',
      },
    ],
  },
  es: {
    title: 'Construye infraestructura cloud con IaC Code',
    subtitle: 'De una sola petición a cambios de infraestructura cloud revisables, ejecutables y desplegables.',
    installLabel: 'Instalar iac-code',
    copyCommand: 'Copiar comando',
    copiedCommand: 'Copiado',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'Demo de terminal de iac-code',
    sections: [
      {
        title: 'Genera plantillas',
        body: 'Pide VPCs, instancias ECS, buckets OSS o stacks completos y recibe salida ROS o Terraform estructurada.',
      },
      {
        title: 'Revisa antes de desplegar',
        body: 'Mantén la infraestructura generada visible en la terminal para inspeccionarla, refinarla y versionarla con el equipo.',
      },
      {
        title: 'Usa contexto cloud',
        body: 'Integra conocimiento de productos cloud, búsqueda de documentación y guía basada en recursos en el mismo flujo. Actualmente, IaC Code admite Alibaba Cloud.',
      },
    ],
    workflowEyebrow: 'Cómo funciona',
    workflowTitle: 'Un ciclo de agente de código para infraestructura',
    workflowSteps: [
      {
        title: 'Describe',
        body: 'Empieza con requisitos en lenguaje natural o envía un prompt único a la automatización.',
      },
      {
        title: 'Genera',
        body: 'Deja que el agente redacte plantillas, parámetros y cambios de infraestructura listos para comando.',
      },
      {
        title: 'Opera',
        body: 'Itera en el REPL, ejecuta comprobaciones e incorpora la IaC final al flujo de entrega.',
      },
    ],
  },
  pt: {
    title: 'Construa infraestrutura em nuvem com IaC Code',
    subtitle: 'De uma única solicitação a mudanças de infraestrutura em nuvem revisáveis, executáveis e implantáveis.',
    installLabel: 'Instalar iac-code',
    copyCommand: 'Copiar comando',
    copiedCommand: 'Copiado',
    installCommands: {
      shell: {
        label: 'Linux / macOS',
        command: 'pip install iac-code',
      },
      windows: {
        label: 'Windows',
        command: 'pip install iac-code',
      },
    },
    demoAlt: 'Demonstração do terminal iac-code',
    sections: [
      {
        title: 'Gere templates',
        body: 'Peça VPCs, instâncias ECS, buckets OSS ou stacks completos e receba saída ROS ou Terraform estruturada.',
      },
      {
        title: 'Revise antes de implantar',
        body: 'Mantenha a infraestrutura gerada visível no terminal para a equipe inspecionar, refinar e versionar.',
      },
      {
        title: 'Use contexto cloud',
        body: 'Traga conhecimento de produtos cloud, busca de documentação e orientação baseada em recursos para o mesmo fluxo. Atualmente, o IaC Code oferece suporte ao Alibaba Cloud.',
      },
    ],
    workflowEyebrow: 'Como funciona',
    workflowTitle: 'Um ciclo de agente de código para infraestrutura',
    workflowSteps: [
      {
        title: 'Descreva',
        body: 'Comece com requisitos em linguagem natural ou envie um prompt único para automação.',
      },
      {
        title: 'Gere',
        body: 'Deixe o agente redigir templates, parâmetros e mudanças de infraestrutura prontas para comando.',
      },
      {
        title: 'Opere',
        body: 'Itere no REPL, rode verificações e leve o IaC final para o seu fluxo de entrega.',
      },
    ],
  },
} satisfies Record<Locale, HomeCopy>;

function getSupportedLocale(locale: string): Locale {
  return Object.prototype.hasOwnProperty.call(copy, locale) ? (locale as Locale) : 'en';
}

function useCurrentLocale() {
  const {i18n} = useDocusaurusContext();
  return getSupportedLocale(i18n.currentLocale);
}

function useHomeCopy() {
  return copy[useCurrentLocale()] ?? copy.en;
}

function useHomepageDraft() {
  return homepageDrafts[useCurrentLocale()] ?? homepageDrafts.en;
}

function useTerminalScenes() {
  return getTerminalScenes(useCurrentLocale());
}

function InstallCommandBar({
  command,
  copyCommand,
  copiedCommand,
  className,
  showPrompt = true,
}: {
  command: string;
  copyCommand: string;
  copiedCommand: string;
  className?: string;
  showPrompt?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    await navigator.clipboard?.writeText(command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  return (
    <div className={clsx(styles.commandBar, !showPrompt && styles.commandBarPlain, className)}>
      {showPrompt && (
        <span className={styles.prompt} aria-hidden="true">
          &gt;_
        </span>
      )}
      <code>{command}</code>
      <button
        className={clsx(styles.copyButton, copied && styles.copyButtonCopied)}
        type="button"
        title={copied ? copiedCommand : copyCommand}
        aria-label={copied ? copiedCommand : copyCommand}
        onClick={handleCopy}
      />
    </div>
  );
}

function DemoEventRow({event}: {event: DemoEvent}) {
  return (
    <div className={clsx(styles.demoEvent, styles[`demoEvent${event.kind[0].toUpperCase()}${event.kind.slice(1)}`])}>
      <span className={styles.demoEventMarker} aria-hidden="true" />
      <div className={styles.demoEventBody}>
        <p>{event.title}</p>
        {event.detail && (
          <small>
            <span className={styles.detailPrefix} aria-hidden="true">
              ⎿
            </span>
            {event.detail}
          </small>
        )}
      </div>
    </div>
  );
}

function TerminalMockup({scene, mini = false}: {scene: TerminalScene; mini?: boolean}) {
  const variant = scene.variant ?? 'repl';

  return (
    <div
      className={clsx(
        styles.terminalMockup,
        styles[`terminalMockup${variant[0].toUpperCase()}${variant.slice(1)}`],
        mini && styles.terminalMockupMini,
      )}
      aria-label={scene.title}>
      <div className={styles.demoToolbar}>
        <span />
        <span />
        <span />
      </div>
      <div className={styles.terminalStream}>
        {scene.events.map((event, index) => (
          <DemoEventRow event={event} key={`${event.kind}-${index}`} />
        ))}
      </div>
    </div>
  );
}

function usePlatform(): Platform {
  const [platform, setPlatform] = useState<Platform>('macOS');

  useEffect(() => {
    const userAgent = typeof navigator === 'undefined' ? '' : navigator.userAgent;
    let detected: Platform = 'macOS';
    if (/Windows/i.test(userAgent)) {
      detected = 'Windows';
    } else if (!/Mac|iPhone|iPad/i.test(userAgent)) {
      detected = 'Linux';
    }
    setPlatform(detected);
  }, []);

  return platform;
}

function DownloadIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true">
      <path d="M12 3v12" />
      <path d="m6 11 6 6 6-6" />
      <path d="M4 21h16" />
    </svg>
  );
}

function DownloadActions() {
  const draft = useHomepageDraft();
  const platform = usePlatform();
  const others = (Object.keys(desktopDownloads) as Platform[]).filter((item) => item !== platform);

  return (
    <div className={styles.dlRow}>
      <a className={styles.dlBtn} href={desktopDownloads[platform]}>
        <DownloadIcon />
        <span>{tokenReplace(draft.downloadLabel, {os: platform})}</span>
      </a>
      <span className={styles.altPlatforms}>
        {draft.otherPlatformsLabel}
        {others.map((item, index) => (
          <span key={item}>
            {index > 0 && ' · '}
            <a href={desktopDownloads[item]}>{item}</a>
          </span>
        ))}
      </span>
    </div>
  );
}

function HomepageHeader() {
  const t = useHomeCopy();
  const draft = useHomepageDraft();
  const {i18n} = useDocusaurusContext();
  const [shot, setShot] = useState<'desktop' | 'terminal'>('desktop');
  const isZh = i18n.currentLocale === 'zh-Hans';
  const desktopAlt = draft.howItems.find((item) => item.screenshot === 'desktop')?.alt ?? t.demoAlt;

  return (
    <header className={styles.hero}>
      <div className={styles.heroInner}>
        <h1>{t.title}</h1>
        <p className={styles.subtitle}>{t.subtitle}</p>

        <DownloadActions />

        <div className={styles.shotTabs} role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={shot === 'desktop'}
            className={clsx(styles.shotTab, shot === 'desktop' && styles.shotTabActive)}
            onClick={() => setShot('desktop')}>
            {draft.shotTabDesktop}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={shot === 'terminal'}
            className={clsx(styles.shotTab, shot === 'terminal' && styles.shotTabActive)}
            onClick={() => setShot('terminal')}>
            {draft.shotTabTerminal}
          </button>
        </div>

        <div className={styles.heroShot}>
          <img src={isZh ? desktopCnScreenshot : desktopEnScreenshot} alt={desktopAlt} hidden={shot !== 'desktop'} />
          <img src={isZh ? demoZhGif : demoEnGif} alt={t.demoAlt} hidden={shot !== 'terminal'} />
        </div>
      </div>
    </header>
  );
}

function WhySection() {
  const draft = useHomepageDraft();
  const {i18n} = useDocusaurusContext();
  const shots = i18n.currentLocale === 'zh-Hans' ? whyCnShots : whyEnShots;

  return (
    <section className={styles.featureRowsSection}>
      <div className={styles.rowsHeader}>
        <h2>{draft.whyTitle}</h2>
      </div>
      <div className={styles.featureRows}>
        {draft.whyItems.map((item, index) => (
          <article
            className={clsx(styles.featureRow, index % 2 === 1 && styles.featureRowReverse)}
            key={item.title}>
            <div className={styles.featureRowCopy}>
              <h3>{item.title}</h3>
              <p>{item.body}</p>
            </div>
            <img
              className={styles.featureShot}
              src={shots[index % shots.length]}
              alt={item.alt ?? item.title}
              loading="lazy"
            />
          </article>
        ))}
      </div>
    </section>
  );
}

function SurfaceCard({
  item,
  scene,
  screenshot,
}: {
  item: HomepageItem;
  scene?: TerminalScene;
  screenshot?: string;
}) {
  return (
    <div className={styles.surfaceCard}>
      {screenshot ? (
        <div className={styles.miniShot}>
          <img src={screenshot} alt={item.alt ?? item.title} loading="lazy" />
        </div>
      ) : (
        scene && <TerminalMockup scene={scene} mini />
      )}
      <h4>{item.title}</h4>
      <p>{item.body}</p>
      {item.command && <code className={styles.cmdChip}>{item.command}</code>}
    </div>
  );
}

function GetStartedSection() {
  const t = useHomeCopy();
  const draft = useHomepageDraft();
  const locale = useCurrentLocale();
  const terminalScenes = useTerminalScenes();
  const [open, setOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const screenshots =
    locale === 'zh-Hans'
      ? {web: webCnScreenshot, desktop: desktopCnScreenshot}
      : {web: webEnScreenshot, desktop: desktopEnScreenshot};
  const desktopItem = draft.howItems.find((item) => item.screenshot === 'desktop');
  const surfaceItems = draft.howItems.filter((item) => item.screenshot !== 'desktop');

  function handleToggle() {
    const next = !open;
    setOpen(next);
    window.setTimeout(() => {
      toggleRef.current?.scrollIntoView({behavior: 'smooth', block: next ? 'start' : 'center'});
    }, 60);
  }

  return (
    <section className={clsx(styles.featureRowsSection, open && styles.surfacesSectionOpen)}>
      <div className={styles.rowsHeader}>
        <h2>{draft.howTitle}</h2>
      </div>

      {desktopItem && (
        <div className={styles.surfaceHeroCard}>
          <div>
            <h3>{desktopItem.title}</h3>
            <p>{desktopItem.body}</p>
            <DownloadActions />
          </div>
          <div className={styles.surfaceShot}>
            <img src={screenshots.desktop} alt={desktopItem.alt ?? desktopItem.title} loading="lazy" />
          </div>
        </div>
      )}

      <button
        ref={toggleRef}
        type="button"
        className={styles.surfaceToggle}
        aria-expanded={open}
        onClick={handleToggle}>
        <span>
          {draft.surfacesToggleLabel}
          {surfaceItems.map((item) => item.title).join(' · ')}
        </span>
        <span className={styles.chev} aria-hidden="true">
          ▾
        </span>
      </button>

      <div className={styles.surfaceMore}>
        <div>
          <div className={styles.surfaceGrid}>
            {surfaceItems.map((item) => (
              <SurfaceCard
                key={item.title}
                item={item}
                scene={item.visual ? terminalScenes[item.visual] : undefined}
                screenshot={item.screenshot === 'web' ? screenshots.web : undefined}
              />
            ))}
          </div>

          <div className={styles.cliHint}>
            <span>{draft.pipHint}</span>
            <InstallCommandBar
              command={installCommand}
              copyCommand={t.copyCommand}
              copiedCommand={t.copiedCommand}
              className={styles.hintCommandBar}
              showPrompt={false}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): React.JSX.Element {
  const t = useHomeCopy();

  return (
    <Layout title="iac-code" description={t.subtitle}>
      <main className={styles.home}>
        <HomepageHeader />
        <WhySection />
        <GetStartedSection />
      </main>
    </Layout>
  );
}
