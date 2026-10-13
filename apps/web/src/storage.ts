/** Sortie Deck localStorage helpers. */

export function lsGet(key: string): string | null {
  return localStorage.getItem(key);
}

export function lsSet(key: string, value: string): void {
  localStorage.setItem(key, value);
}

export function lsRemove(key: string): void {
  localStorage.removeItem(key);
}
