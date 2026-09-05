/**
 * Which modal dialogs are currently mounted, in mount order.
 *
 * Deliberately its OWN module, holding nothing but this array and two tiny
 * readers. `useFocusTrap` lives only in lazy chunks (AuthModal, HistoryDrawer,
 * ConnectedAccountsDrawer are all `React.lazy`), but `useLandingState` is in
 * the EAGER chunk and needs `isModalDialogOpen()` for its `/` shortcut.
 * Importing that from the hook dragged the whole trap into the eager bundle:
 * measured 65,540 -> 65,849 B gzip, leaving 151 B of a 66,000 B budget. Split
 * out, the eager chunk pays only for the array and the length check.
 *
 * No React and no app imports here on purpose — it is a plain module-level
 * registry, which is also what makes it safe to read from a `window` listener.
 */

/** Opaque tokens, one per mounted trap. Only identity matters. */
const trapStack: object[] = [];

export function pushTrap(token: object): void {
  trapStack.push(token);
}

export function removeTrap(token: object): void {
  const i = trapStack.indexOf(token);
  if (i !== -1) trapStack.splice(i, 1);
}

/**
 * True only for the top-most mounted dialog. Everything below it is inert,
 * which is exactly what its own `aria-modal="true"` already claims.
 */
export function isTopTrap(token: object): boolean {
  return trapStack[trapStack.length - 1] === token;
}

/**
 * True while any modal dialog is mounted.
 *
 * For page-level keyboard shortcuts, which are not Tab and so are invisible to
 * the focus trap. `useLandingState`'s "/" shortcut focused the hero input
 * behind the backdrop, and the keystrokes after it were not merely lost — a
 * reviewer typed a question and pressed Enter with a drawer open and the app
 * navigated to the chat screen. That is the same hazard #331 is about
 * (operating a control the interface says is unavailable) reached by another
 * key.
 */
export function isModalDialogOpen(): boolean {
  return trapStack.length > 0;
}

/** Exported for tests only — asserts the stack does not leak across mounts. */
export const __testOnly = {
  depth: () => trapStack.length,
};
