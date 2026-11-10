/** Display names for stable workflow keys. Keys themselves are never renamed. */

type WorkflowLike = {
  workflow_key?: string;
  title?: string;
  description?: string;
  manifest?: { title?: string; description?: string } | null;
};

const FAMILY: Record<string, { title: string; usage: string }> = {
  'demo-mock': {
    title: '离线演示',
    usage: '不连接 Google、不扣费，只用来熟悉界面。生成的是示例图，不是真实作品。',
  },
  'krea2-moss-dual-t2i': {
    title: 'Krea-2 + MOSS 文生图',
    usage: '先用 Krea-2 快速构图，再用 MOSS 精修。适合要胶片或写实质感的图片，不用上传参考图。',
  },
  'moss-image-sft-t2i': {
    title: 'MOSS 文生图',
    usage: '只用文字生成一张图，不需要参考图。',
  },
  'minimax-h3-i2v': {
    title: '图生视频（首帧）',
    usage: '上传一张首帧图，生成带声音的短视频。尾帧可以不传。',
  },
  'minimax-h3-ref2v': {
    title: '多参考图生视频',
    usage: '上传参考图锁定人物和画风，生成带声音的短视频。至少需要第一张参考图。',
  },
};

function readable(value: unknown): string {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!text || /^[a-z0-9_.-]+$/i.test(text)) return '';
  return text;
}

export function workflowTitle(workflow: WorkflowLike): string {
  return readable(workflow.manifest?.title) || readable(workflow.title) || FAMILY[workflow.workflow_key || '']?.title || workflow.workflow_key || '未命名工作流';
}

export function workflowUsage(workflow: WorkflowLike): string {
  return readable(workflow.manifest?.description) || readable(workflow.description) || FAMILY[workflow.workflow_key || '']?.usage || '';
}

export function workflowSearchText(workflow: WorkflowLike & { model_id?: string }): string {
  return [workflowTitle(workflow), workflowUsage(workflow), workflow.workflow_key, workflow.model_id].filter(Boolean).join(' ');
}
