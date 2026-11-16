/**
 * Our own mark, drawn rather than fetched.
 *
 * Inline and stroked in `currentColor` so it takes the tint of whatever tile
 * it sits in: the same mark reads on a light bed and a dark one without a
 * second asset, and it cannot drift from the theme the way a pair of
 * theme-picked files does. The favicons keep their own opaque beds because a
 * browser tab has no surface to inherit; a tile in a list does.
 */
export function ConductorMark({ className = '' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 56 56"
      fill="none"
      stroke="currentColor"
      strokeWidth={4}
      aria-hidden
      className={className}
    >
      <path d="M13 44L28 12L43 44" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M19 34.6L37 30" strokeLinecap="round" />
    </svg>
  );
}
