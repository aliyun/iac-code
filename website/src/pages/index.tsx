import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import styles from './index.module.css';
import demoEnGif from '@site/static/img/demo_en.gif';
import demoZhGif from '@site/static/img/demo_zh.gif';

type Locale = 'en' | 'zh-Hans' | 'ja' | 'fr' | 'de' | 'es' | 'pt';

const copy = {
  en: {
    title: 'AI-powered Infrastructure as Code for Alibaba Cloud',
    subtitle:
      'Generate, inspect, and manage ROS and Terraform templates from your terminal with an assistant built for real infrastructure workflows.',
    primary: 'Get Started',
    secondary: 'View CLI Usage',
    eyebrow: 'Infrastructure automation, from prompt to template',
    terminalCommand: 'iac-code --prompt "Create a VPC and two ECS instances"',
    terminalOutput: 'Drafting ROS resources, validating parameters, and preparing deployment-ready IaC.',
    sections: [
      {
        title: 'Say it, ship it',
        body: 'Describe what you need in plain language — IaC Code turns your intent into validated, deployment-ready ROS or Terraform templates.',
      },
      {
        title: 'One command to production',
        body: 'Go from template to running infrastructure and applications in one flow — create, update, delete, and monitor stacks across regions.',
      },
      {
        title: 'Cloud smarts built in',
        body: 'Search documentation, check resource availability, and estimate costs before you deploy — every decision backed by real cloud data.',
      },
    ],
  },
  'zh-Hans': {
    title: '面向阿里云的 AI 基础设施即代码助手',
    subtitle:
      '通过终端生成、检查和管理 ROS 与 Terraform 模板，让自然语言需求进入可审阅的基础设施工作流。',
    primary: '快速开始',
    secondary: '查看 CLI 用法',
    eyebrow: '从提示词到模板的基础设施自动化',
    terminalCommand: 'iac-code --prompt "创建一个 VPC 和两台 ECS 实例"',
    terminalOutput: '正在规划 ROS 资源、校验参数，并准备可部署的 IaC 模板。',
    sections: [
      {
        title: '说出来，就生成',
        body: '用自然语言描述需求，IaC Code 自动生成经过校验、可直接部署的 ROS 或 Terraform 模板。',
      },
      {
        title: '一句话到上线',
        body: '从模板到基础设施和应用运行，一站式完成——创建、更新、删除资源栈，跨地域监控部署进度。',
      },
      {
        title: '云端智能加持',
        body: '搜索云产品文档、查询资源库存、部署前估算成本——每一个决策都有真实云数据支撑。',
      },
    ],
  },
  ja: {
    title: 'Alibaba Cloud 向け AI 駆動 Infrastructure as Code',
    subtitle:
      'ターミナルから ROS および Terraform テンプレートを生成・検査・管理。自然言語の要件を、レビュー可能なインフラワークフローに変換します。',
    primary: 'はじめる',
    secondary: 'CLI の使い方',
    eyebrow: 'プロンプトからテンプレートへ、インフラ自動化',
    terminalCommand: 'iac-code --prompt "VPC と 2 台の ECS インスタンスを作成"',
    terminalOutput: 'ROS リソースを設計し、パラメータを検証し、デプロイ可能な IaC を準備しています。',
    sections: [
      {
        title: '言葉にすれば、即生成',
        body: '必要なものを自然言語で記述するだけで、IaC Code が検証済みですぐにデプロイ可能な ROS または Terraform テンプレートを生成します。',
      },
      {
        title: 'ワンコマンドで本番へ',
        body: 'テンプレートから稼働中のインフラとアプリケーションまでを一気通貫で実現。リージョンをまたいでスタックの作成・更新・削除・監視を行います。',
      },
      {
        title: 'クラウドの知見を内蔵',
        body: 'ドキュメント検索、リソース在庫確認、デプロイ前のコスト見積もり — すべての判断が実際のクラウドデータに裏付けられています。',
      },
    ],
  },
  fr: {
    title: 'Infrastructure as Code propulsée par l\'IA pour Alibaba Cloud',
    subtitle:
      'Générez, inspectez et gérez des templates ROS et Terraform depuis votre terminal grâce à un assistant conçu pour les workflows d\'infrastructure réels.',
    primary: 'Commencer',
    secondary: 'Utilisation CLI',
    eyebrow: 'Automatisation d\'infrastructure, du prompt au template',
    terminalCommand: 'iac-code --prompt "Créer un VPC et deux instances ECS"',
    terminalOutput: 'Conception des ressources ROS, validation des paramètres et préparation de l\'IaC déployable.',
    sections: [
      {
        title: 'Décrivez, déployez',
        body: 'Décrivez vos besoins en langage naturel — IaC Code transforme vos intentions en templates ROS ou Terraform validés et prêts au déploiement.',
      },
      {
        title: 'Une commande vers la production',
        body: 'Du template à l\'infrastructure en production en un seul flux — créez, mettez à jour, supprimez et surveillez les piles à travers les régions.',
      },
      {
        title: 'Intelligence cloud intégrée',
        body: 'Recherchez la documentation, vérifiez la disponibilité des ressources et estimez les coûts avant de déployer — chaque décision s\'appuie sur des données cloud réelles.',
      },
    ],
  },
  de: {
    title: 'KI-gestützte Infrastructure as Code für Alibaba Cloud',
    subtitle:
      'Generieren, prüfen und verwalten Sie ROS- und Terraform-Templates direkt aus dem Terminal mit einem Assistenten für reale Infrastruktur-Workflows.',
    primary: 'Loslegen',
    secondary: 'CLI-Nutzung',
    eyebrow: 'Infrastruktur-Automatisierung, vom Prompt zum Template',
    terminalCommand: 'iac-code --prompt "Erstelle eine VPC und zwei ECS-Instanzen"',
    terminalOutput: 'ROS-Ressourcen werden entworfen, Parameter validiert und bereitstellungsfertiges IaC vorbereitet.',
    sections: [
      {
        title: 'Sagen Sie es, deployen Sie es',
        body: 'Beschreiben Sie Ihre Anforderungen in natürlicher Sprache — IaC Code verwandelt Ihre Absichten in validierte, sofort einsatzbereite ROS- oder Terraform-Templates.',
      },
      {
        title: 'Ein Befehl bis zur Produktion',
        body: 'Vom Template zur laufenden Infrastruktur und Anwendung in einem Arbeitsablauf — erstellen, aktualisieren, löschen und überwachen Sie Stacks über Regionen hinweg.',
      },
      {
        title: 'Cloud-Intelligenz inklusive',
        body: 'Durchsuchen Sie Dokumentationen, prüfen Sie die Ressourcenverfügbarkeit und schätzen Sie Kosten vor dem Deployment — jede Entscheidung basiert auf echten Cloud-Daten.',
      },
    ],
  },
  es: {
    title: 'Infrastructure as Code con IA para Alibaba Cloud',
    subtitle:
      'Genera, inspecciona y gestiona plantillas ROS y Terraform desde tu terminal con un asistente diseñado para flujos de trabajo de infraestructura reales.',
    primary: 'Comenzar',
    secondary: 'Uso del CLI',
    eyebrow: 'Automatización de infraestructura, del prompt a la plantilla',
    terminalCommand: 'iac-code --prompt "Crear un VPC y dos instancias ECS"',
    terminalOutput: 'Diseñando recursos ROS, validando parámetros y preparando IaC listo para despliegue.',
    sections: [
      {
        title: 'Dilo y despliégalo',
        body: 'Describe lo que necesitas en lenguaje natural — IaC Code transforma tus intenciones en plantillas ROS o Terraform validadas y listas para desplegar.',
      },
      {
        title: 'Un comando a producción',
        body: 'Del template a la infraestructura en producción en un solo flujo — crea, actualiza, elimina y monitorea stacks en múltiples regiones.',
      },
      {
        title: 'Inteligencia cloud integrada',
        body: 'Busca documentación, verifica la disponibilidad de recursos y estima costos antes de desplegar — cada decisión respaldada por datos reales de la nube.',
      },
    ],
  },
  pt: {
    title: 'Infrastructure as Code com IA para Alibaba Cloud',
    subtitle:
      'Gere, inspecione e gerencie templates ROS e Terraform pelo terminal com um assistente feito para fluxos de trabalho de infraestrutura reais.',
    primary: 'Começar',
    secondary: 'Uso do CLI',
    eyebrow: 'Automação de infraestrutura, do prompt ao template',
    terminalCommand: 'iac-code --prompt "Criar um VPC e duas instâncias ECS"',
    terminalOutput: 'Projetando recursos ROS, validando parâmetros e preparando IaC pronto para implantação.',
    sections: [
      {
        title: 'Diga e implante',
        body: 'Descreva o que você precisa em linguagem natural — o IaC Code transforma suas intenções em templates ROS ou Terraform validados e prontos para implantação.',
      },
      {
        title: 'Um comando para produção',
        body: 'Do template à infraestrutura em produção em um único fluxo — crie, atualize, exclua e monitore stacks em várias regiões.',
      },
      {
        title: 'Inteligência cloud integrada',
        body: 'Pesquise documentação, verifique a disponibilidade de recursos e estime custos antes de implantar — cada decisão apoiada por dados reais da nuvem.',
      },
    ],
  },
} satisfies Record<Locale, {
  title: string;
  subtitle: string;
  primary: string;
  secondary: string;
  eyebrow: string;
  terminalCommand: string;
  terminalOutput: string;
  sections: Array<{title: string; body: string}>;
}>;

function useHomeCopy() {
  const {i18n} = useDocusaurusContext();
  const locale = i18n.currentLocale as Locale;
  return copy[locale] ?? copy.en;
}

function HomepageHeader() {
  const t = useHomeCopy();
  const {i18n} = useDocusaurusContext();

  return (
    <header className={styles.hero}>
      <div className={styles.heroBackdrop} />
      <div className={styles.heroInner}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>{t.eyebrow}</p>
          <h1>{t.title}</h1>
          <p className={styles.subtitle}>{t.subtitle}</p>
          <div className={styles.actions}>
            <Link className={clsx('button', styles.primaryButton)} to="/docs/getting-started/installation">
              {t.primary}
            </Link>
            <Link className={clsx('button', styles.secondaryButton)} to="/docs/cli/usage">
              {t.secondary}
            </Link>
          </div>
        </div>
        <div className={styles.productVisual} aria-label="iac-code terminal preview">
          <img
            src={i18n.currentLocale === 'zh-Hans' ? demoZhGif : demoEnGif}
            alt="iac-code demo"
            className={styles.productGif}
          />
        </div>
      </div>
    </header>
  );
}

function FeatureSection() {
  const t = useHomeCopy();

  return (
    <main className={styles.main}>
      <section className={styles.featureGrid}>
        {t.sections.map((section) => (
          <article className={styles.feature} key={section.title}>
            <h2>{section.title}</h2>
            <p>{section.body}</p>
          </article>
        ))}
      </section>
    </main>
  );
}

export default function Home(): React.JSX.Element {
  const t = useHomeCopy();

  return (
    <Layout title="iac-code" description={t.subtitle}>
      <HomepageHeader />
      <FeatureSection />
    </Layout>
  );
}
