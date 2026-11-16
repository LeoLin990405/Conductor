import React from 'react';
import { cn } from '@/lib/utils';
import { FOCUS_RING } from './tokens';

interface ToggleSwitchProps {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  /** Extra classes layered onto the pill (e.g. `mt-0.5` for top alignment). */
  className?: string;
  /** Accessible name — the visual label beside the pill isn't associated with it. */
  ariaLabel?: string;
}

/**
 * Hand-rolled pill toggle used across Settings (voice input, experiments).
 * Deliberately a plain button rather than the Radix switch — it preserves the
 * exact markup and theme-var styling both call sites already shipped.
 *
 * The track is a control, not a semantic tag, so it wears the terminal's
 * control radius rather than a pill's; the knob stays a circle, because a
 * circle is a shape and not a radius. The shared 2px ink.focus ring is named
 * here so the control cannot lose it.
 */
export function ToggleSwitch({ checked, onChange, disabled, className, ariaLabel }: ToggleSwitchProps): React.ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={onChange}
      disabled={disabled}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-[var(--radius-control)] border-2 border-transparent',
        'transition-colors duration-200 ease-in-out motion-reduce:transition-none',
        FOCUS_RING,
        className,
      )}
      style={{
        backgroundColor: checked ? 'var(--color-accent-primary)' : 'var(--color-bg-elevated)',
        borderColor: 'var(--color-border-muted)',
      }}
    >
      <span
        className="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-(--color-text-primary) ring-0 transition duration-200 ease-in-out motion-reduce:transition-none"
        style={{
          transform: checked ? 'translateX(16px)' : 'translateX(0)',
        }}
      />
    </button>
  );
}
