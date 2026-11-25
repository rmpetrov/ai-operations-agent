export function resolvePointer(snapshot: unknown, pointer: string): unknown {
  if (!pointer.startsWith('/')) return undefined;
  let current = snapshot;
  for (const part of pointer.slice(1).split('/')) {
    const key = part.replaceAll('~1', '/').replaceAll('~0', '~');
    if (current === null || typeof current !== 'object' || !Object.hasOwn(current, key)) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}
