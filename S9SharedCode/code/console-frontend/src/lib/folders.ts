import type { DocFolder } from '../api';

export type FolderNode = DocFolder & {
  /** "Research / Batteries" */
  path: string;
  /** 0 for a top-level folder. */
  depth: number;
};

/** Flatten a folder table into tree order with readable paths.
 *
 * The API returns a flat list; both the Documents tree and Chat's scope picker
 * need the same ordering and labels, so they share this one walk. Orphans
 * (a parent id that no longer exists) are treated as roots rather than being
 * dropped: losing a folder from the picker because one row was corrupt is
 * worse than showing it at the top level. */
export function flattenFolders(folders: DocFolder[]): FolderNode[] {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const children = new Map<string, DocFolder[]>();
  for (const f of folders) {
    const parent = f.parent_id && byId.has(f.parent_id) ? f.parent_id : '';
    const list = children.get(parent) || [];
    list.push(f);
    children.set(parent, list);
  }
  for (const list of children.values()) {
    list.sort((a, b) => a.name.localeCompare(b.name, undefined, {
      sensitivity: 'base',
    }));
  }

  const out: FolderNode[] = [];
  const seen = new Set<string>();
  const walk = (parentId: string, prefix: string, depth: number) => {
    for (const f of children.get(parentId) || []) {
      if (seen.has(f.id)) continue;
      seen.add(f.id);
      const path = prefix ? `${prefix} / ${f.name}` : f.name;
      out.push({ ...f, path, depth });
      walk(f.id, path, depth + 1);
    }
  };
  walk('', '', 0);
  // Cycle or duplicate-id safety: never lose a folder row from the UI.
  for (const f of folders) {
    if (!seen.has(f.id)) out.push({ ...f, path: f.name, depth: 0 });
  }
  return out;
}

/** Descendant-inclusive ids for a folder selection. */
export function folderSubtree(folderId: string, folders: DocFolder[]): Set<string> {
  if (!folderId) return new Set();
  const children = new Map<string, string[]>();
  for (const f of folders) {
    const list = children.get(f.parent_id) || [];
    list.push(f.id);
    children.set(f.parent_id, list);
  }
  const out = new Set<string>();
  const stack = [folderId];
  while (stack.length) {
    const id = stack.pop() as string;
    if (!id || out.has(id)) continue;
    out.add(id);
    stack.push(...(children.get(id) || []));
  }
  return out;
}
