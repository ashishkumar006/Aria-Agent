/* The command palette's open channel.
 *
 * Kept in its own module so the rail (which dispatches the event) and the
 * palette (which listens for it) do not have to import each other — a direct
 * ui <-> CommandPalette edge would be a cycle, and the rail must not depend on
 * the palette's bundle.
 */
export const OPEN_PALETTE_EVENT = 'aria:open-palette';

/** Ask the mounted palette to open. No-op if it is not mounted yet. */
export function openCommandPalette() {
  window.dispatchEvent(new Event(OPEN_PALETTE_EVENT));
}
