/** Images in untrusted Markdown stay inert until the reader chooses to load them. */
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { math } from "micromark-extension-math";
import { mathFromMarkdown } from "mdast-util-math";
import { mathCompatibility } from "./markdownMathCompatibility.ts";
import type { Definition, Image, ImageReference, RootContent } from "mdast";

export type MarkdownImageChoice = { key: string; source: string; host: string; alt: string; trusted: boolean };
export type PrivateMarkdownSource = { text: string; images: MarkdownImageChoice[] };

function httpImage(value: string, origin?: string): URL | null {
  try {
    const url = new URL(value, origin);
    return (url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password ? url : null;
  } catch { return null; }
}

/** Parse image nodes (including reference images), not regex-shaped Markdown.
 * Code fences, inline code, ordinary links and raw HTML retain their existing
 * renderer behavior. Replacing the entire image node keeps a shared reference
 * definition usable by ordinary links without granting an image permission.
 */
export function privateMarkdownSource(text: string, origin?: string): PrivateMarkdownSource {
  const tree = fromMarkdown(text, { extensions: [gfm(), mathCompatibility(), math()], mdastExtensions: [gfmFromMarkdown(), mathFromMarkdown()] });
  const definitions = new Map<string, Definition>();
  const nodes: (Image | ImageReference)[] = [];
  function visit(node: RootContent): void {
    if (node.type === "definition" && !definitions.has(node.identifier.toUpperCase())) {
      definitions.set(node.identifier.toUpperCase(), node);
    }
    if (node.type === "image" || node.type === "imageReference") nodes.push(node);
    if ("children" in node) for (const child of node.children) visit(child as RootContent);
  }
  for (const node of tree.children) visit(node);
  const replacements: { start: number; end: number; text: string }[] = [];
  const images: MarkdownImageChoice[] = [];
  for (const node of nodes) {
    const source = node.type === "image" ? node.url : definitions.get(node.identifier.toUpperCase())?.url;
    const start = node.position?.start.offset, end = node.position?.end.offset;
    if (source === undefined || start === undefined || end === undefined) continue;
    const key = `forgeflow-image-${replacements.length}`;
    const url = httpImage(source, origin);
    const trusted = Boolean(url && origin && url.origin === origin && !url.search && !url.hash
      && /^\/api\/knowledge\/images\/[a-f0-9]{64}$/.test(url.pathname));
    // Do not pass authored destinations through the third-party renderer's
    // automatic HTTP image path, including same-origin URLs that may redirect.
    const alt = (node.alt ?? "").replace(/[\\[\]`*_!<>]/g, "\\$&").replace(/\r?\n/g, " ");
    replacements.push({ start, end, text: `![${alt}](${key})` });
    if (url) images.push({ key, source: url.href, host: url.host, alt: node.alt ?? "", trusted });
  }
  let result = text;
  for (const item of replacements.sort((a, b) => b.start - a.start)) {
    result = result.slice(0, item.start) + item.text + result.slice(item.end);
  }
  return { text: result, images };
}
