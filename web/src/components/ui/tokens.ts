/**
 * The terminal-native geometry the primitives in this folder wear.
 *
 * One place, because the point of the restyle is convergence: the floating
 * surfaces stop carrying three different fills (a popover on one, a menu on
 * another, a dialog on the page ground) and share one — surface elevation 1
 * with the token shadow — and every control wears the same 2px ink.focus
 * ring. A primitive that reached for its own hex, its own shadow or its own
 * radius would put the split back, so the strings live here and the
 * primitives import them.
 *
 * Every value is a token, never a literal: the fill reads --surface-elevated
 * through --color-bg-elevated, the shadow reads --shadow-elevated (which is
 * --elevation-1), the radius reads --radius-overlay and the ring reads
 * --border-focus (2px) in --color-focus-ring (ink.focus).
 */

import './focus-ring.css';

/** Surface elevation 1: the one fill a floating surface wears. */
export const FLOATING_FILL = 'bg-(--color-bg-elevated)';

/** The token shadow — --shadow-elevated, which is --elevation-1. */
export const FLOATING_SHADOW = 'shadow-(--shadow-elevated)';

/** The radius cap: nothing in this folder rounds past radius.overlay. */
export const FLOATING_RADIUS = 'rounded-[var(--radius-overlay)]';

/** The hairline edge a floating surface carries. */
export const FLOATING_EDGE = 'border border-(--color-border-muted)';

/** The whole floating surface, in one string. */
export const FLOATING_SURFACE = `${FLOATING_FILL} ${FLOATING_EDGE} ${FLOATING_SHADOW} ${FLOATING_RADIUS}`;

/**
 * The 2px ink.focus ring, outset. Every control wears it.
 *
 * The ring is painted by `focus-ring.css`, inside the same cascade layer as
 * the app's baseline `:focus-visible` ring (`styles/tokens.css`). That layer is
 * what makes the ring safe to name in a shared primitive: the layer holds the
 * defaults, so a surface that answers focus in its own language -- the auth
 * surface, which clears the outline and paints its own ember -- still wins.
 * A Tailwind `focus-visible:outline-*` utility would be unlayered, and an
 * unlayered rule beats every layered one whatever its specificity, so a
 * primitive that named the ring that way would take the ring away from the
 * sign-in page as soon as it rendered an <Input>.
 */
export const FOCUS_RING = 'ui-focus-ring';

/**
 * The same ring drawn inward, for a control inside a box that clips the outset
 * one — a menu panel, a segmented control, a row of a list.
 */
export const FOCUS_RING_INWARD = 'ui-focus-ring-inward';

/** Chrome, in the mono face: the terminal's voice for control text. */
export const MONO_CHROME = 'font-mono';

/**
 * An identifier: data, never prose, so the figures line up and a hex id is
 * never mistaken for a sentence.
 */
export const MONO_ID = 'font-mono tabular-nums';

/** A keyboard hint: the same face as chrome, so a key reads as a key. */
export const MONO_KBD = 'font-mono';

/**
 * The chip a keyboard hint wears. A control's geometry, not a floating
 * surface's: it sits inside a panel rather than over the page.
 */
export const KBD_CHIP = [
  'inline-flex items-center gap-1',
  'rounded-[var(--radius-control)] border border-(--color-border-muted) bg-(--color-bg-input)',
  'px-1.5 py-0.5 text-[0.6875rem] text-(--color-text-secondary)',
  MONO_KBD,
].join(' ');

/** A control's own radius: 2px, the terminal's control geometry. */
export const CONTROL_RADIUS = 'rounded-[var(--radius-control)]';
