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
        'getting-started/contributing',
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
        'cli/skills',
        'cli/sessions',
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
      label: 'Automation',
      items: [
        'automation/non-interactive-mode',
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
          label: 'A2A Protocol',
          items: [
            'a2a/overview',
            'a2a/getting-started',
            'a2a/command-reference',
            'a2a/protocol-reference',
            'a2a/http-transport',
            'a2a/examples',
          ],
        },
      ],
    },
    'contact',
  ],
};

export default sidebars;
