export type ProjectOption = { id: string; title?: string };

export function projectOptionLabel(project: ProjectOption, projects: ProjectOption[]): string {
  const title = (project.title || '').trim() || '未命名项目';
  const duplicates = projects.filter((item) => ((item.title || '').trim() || '未命名项目') === title).length;
  return duplicates > 1 ? `${title} · ${project.id.slice(0, 6)}` : title;
}

/** Real activation uses the project the user already selected. It never picks another row. */
export function selectActivationProjectId(projects: ProjectOption[], selectedId: string): string {
  if (!selectedId) return '';
  return projects.some((project) => project.id === selectedId) ? selectedId : '';
}
