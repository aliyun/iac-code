import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    {
      type: 'category',
      label: 'Getting Started',
      link: {
        type: 'doc',
        id: 'intro',
      },
      items: [
        'getting-started/installation',
        'getting-started/quick-start',
      ],
    },
    {
      type: 'category',
      label: 'Using IaC Code',
      items: [
        'cli/usage',
        'cli/interactive-mode',
        'cli/command-line-options',
        'cli/commands',
      ],
    },
    {
      type: 'category',
      label: 'Configuration',
      items: [
        'configuration/authentication',
        'configuration/llm-providers',
        'configuration/alibaba-cloud-credentials',
        'configuration/environment-variables',
        'configuration/runtime-configuration',
      ],
    },
    {
      type: 'category',
      label: 'ACP Protocol',
      items: [
        'acp/overview',
        'acp/getting-started',
        'acp/protocol-reference',
        'acp/http-transport',
        'acp/examples',
      ],
    },
    {
      type: 'category',
      label: 'Automation',
      items: [
        'automation/non-interactive-mode',
      ],
    },
  ],
};

export default sidebars;
