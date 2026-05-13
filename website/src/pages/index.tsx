import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import styles from './index.module.css';

type Locale = 'en' | 'zh-Hans';

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
          <div className={styles.terminalHeader}>
            <span />
            <span />
            <span />
          </div>
          <pre className={styles.terminalBody}>
            <code>
              <span className={styles.prompt}>$ </span>
              {t.terminalCommand}
              {'\n\n'}
              <span className={styles.output}>{t.terminalOutput}</span>
            </code>
          </pre>
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
