/**
 * Chinese display text for the platform's shipped plugins, connectors, skills,
 * sub-agents.
 *
 * The manifests are authored upstream in English and the API returns them
 * verbatim. The CN build reads as a Chinese product, so every list and detail
 * surface swaps the name and the one-line summary for these; ids stay the
 * values the agent, the catalog and the URLs key on. Unknown or user-authored
 * entries fall straight through to their own words.
 */

export type ZhKind = 'plugin' | 'server' | 'skill' | 'subagent';

interface ZhText {
  name: string;
  description: string;
}

const ZH: Record<ZhKind, Record<string, ZhText>> = {
  plugin: {
    'conductor-deliverables': {
      name: '交付物',
      description: 'Agent 交付给你的东西：报告、演示文稿、工作簿与交互式仪表盘，以及让它们看起来出自同一套设计的设计规范。',
    },
    'conductor-service': {
      name: '服务能力',
      description: 'Agent 如何运行自身：定时任务、已保存的工作流与跨会话携带上下文的用户记忆。',
    },
  },
  server: {
    x_api: { name: 'X 搜索', description: '搜索 X（Twitter）帖子，获取用户、推文与回复串，只读。' },
    scrape: { name: '网页抓取', description: '带反爬绕过与 Markdown 提取的网页抓取。' },
  },
  skill: {
    'run-workflow': { name: '工作流编排', description: '用 JavaScript 工作流脚本编排并行子 Agent 流水线。' },
    'conductor-doc': { name: '平台使用说明', description: '介绍平台本身：工作区、文件、会话与能力的边界。' },
    pdf: { name: 'PDF 处理', description: '读取、填写与生成 PDF：结构、字体、文本与表格抽取。' },
    docx: { name: 'Word 文档', description: '用 python-docx 生成或编辑供人审阅修改的 Word 文档。' },
    pptx: { name: 'PowerPoint 演示', description: '用 pptxgenjs 与原生图表制作、就地编辑 PPT 演示文稿。' },
    xlsx: { name: 'Excel 工作簿', description: '用 openpyxl 构建或编辑含实时公式的 Excel 模型并重算。' },
    'research-conventions': { name: '研究规范', description: '研究交付物遵循的证据、引用与数据质量规则。' },
    'ui-design': { name: 'UI 设计规范', description: '交付物的设计质量参考：字体、色彩与版式组合。' },
  },
  subagent: {
    'general-purpose': { name: '通用子 Agent', description: '把复杂任务交给拥有全部文件与工具权限的通用子 Agent。' },
    research: { name: '研究子 Agent', description: '把具体研究课题交给研究子 Agent，一次一个主题。' },
    'report-builder': { name: '报告构建子 Agent', description: '交给专门产出文档、演示与报告的构建子 Agent。' },
  },

};

function isZh(language: string | undefined): boolean {
  return !!language && language.toLowerCase().startsWith('zh');
}

/** The entry's Chinese name, or the fallback for an unknown/custom entry. */
export function zhName(
  kind: ZhKind,
  id: string | null | undefined,
  fallback: string | null | undefined,
  language: string | undefined,
): string {
  const entry = isZh(language) && id ? ZH[kind][id] : undefined;
  return entry?.name ?? fallback ?? id ?? '';
}

/** The entry's Chinese one-line description, or the fallback. */
export function zhDescription(
  kind: ZhKind,
  id: string | null | undefined,
  fallback: string | null | undefined,
  language: string | undefined,
): string {
  const entry = isZh(language) && id ? ZH[kind][id] : undefined;
  return entry?.description ?? fallback ?? '';
}
