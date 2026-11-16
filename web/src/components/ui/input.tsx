import * as React from "react"
import { cn } from "../../lib/utils"
import { CONTROL_RADIUS, FOCUS_RING, MONO_CHROME } from "./tokens"

/**
 * A text field in the terminal's own geometry: the input surface, a hairline
 * edge, the control radius, the mono face, and the 2px ink.focus ring.
 *
 * The ring the field draws for itself — the accent edge and its one-pixel
 * halo, in `styles/tokens.css` — sits under the ring this class names, so a
 * field the mouse focused still shows the edge that says it is about to take
 * typing, and a field the keyboard reached shows the ring as well.
 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      className={cn(
        "flex h-9 w-full px-3 py-2 text-base sm:text-sm",
        "border border-(--color-border-muted) bg-(--color-bg-input) text-(--color-text-primary)",
        "placeholder:text-(--color-text-tertiary)",
        "file:border-0 file:bg-transparent file:text-sm file:font-medium",
        "disabled:cursor-not-allowed disabled:opacity-50",
        CONTROL_RADIUS,
        MONO_CHROME,
        FOCUS_RING,
        className
      )}
      {...props}
    />
  )
}

export { Input }
