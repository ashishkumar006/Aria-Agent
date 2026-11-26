import { File, FileArchive, FileCode2, FileCog, FileImage, FileJson2, FileLock2,
         FileSpreadsheet, FileTerminal, FileText, type LucideIcon } from 'lucide-react';

/* The file icon language of every file-tree built after VS Code: one glyph,
   coloured by what the extension is. A near-monochrome tree is what this
   studio looked like before — every row was grey text and the eye had
   nothing to land on but the names. Shared by the Explorer and the inline
   diff card so "what a .py file looks like" cannot drift between the two. */

export interface FileVisual {
  icon: LucideIcon;
  cls: string;
}

const byExt = (icon: LucideIcon, cls: string, exts: string[]) =>
  Object.fromEntries(exts.map((e) => [e, { icon, cls }]));
const source = (cls: string, exts: string[]) => byExt(FileCode2, cls, exts);

export const FILE_VISUALS: Record<string, FileVisual> = {
  ...source('#b0b0b0', ['py', 'ipynb']),
  ...source('#b0b0b0', ['js', 'mjs', 'cjs', 'jsx']),
  ...source('#b0b0b0', ['ts', 'tsx']),
  ...source('#b0b0b0', ['html', 'htm']),
  ...source('#b0b0b0', ['css', 'scss', 'less']),
  ...source('#b0b0b0', ['java', 'c', 'h', 'cpp', 'cc', 'hpp']),
  ...source('#b0b0b0', ['rs']),
  ...source('#b0b0b0', ['go']),
  ...byExt(FileTerminal, '#b0b0b0', ['sh', 'zsh', 'bash', 'ps1', 'bat', 'cmd']),
  ...byExt(FileSpreadsheet, '#b0b0b0', ['sql', 'csv', 'tsv']),
  ...byExt(FileArchive, '#b0b0b0', ['zip', 'gz', 'tar', '7z', 'rar']),
  ...byExt(FileText, '#b0b0b0', ['md', 'markdown', 'rst']),
  ...byExt(FileText, '#8a8a8a', ['txt', 'log']),
  ...byExt(FileJson2, '#b0b0b0', ['json', 'jsonl']),
  ...byExt(FileCog, '#8a8a8a', ['yml', 'yaml', 'toml', 'ini', 'cfg', 'conf']),
  ...byExt(FileImage, '#b0b0b0', ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg']),
  ...byExt(FileLock2, '#8a8a8a', ['lock']),
};

export const fileVisual = (name: string): FileVisual => {
  const ext = (name || '').toLowerCase().split('.').pop() || '';
  const base = (name || '').toLowerCase();
  if (FILE_VISUALS[ext]) return FILE_VISUALS[ext];
  /* lockfiles arrive as `.lock`; a `.gitignore`-style dotfile with no
     extension gets the cog. */
  if (base.endsWith('.lock')) return { icon: FileLock2, cls: '#8a8a8a' };
  if (base.startsWith('.') && base.length > 1) return { icon: FileCog, cls: '#8a8a8a' };
  return { icon: File, cls: '#8a8a8a' };
};

export const FOLDER_CLS = '#8a8a8a';
