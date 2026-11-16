import * as React from "react"
import { ChevronDown } from "lucide-react"
import { cn } from "../../lib/utils"
import { CONTROL_RADIUS, FOCUS_RING, MONO_ID } from "./tokens"

export interface SelectProps extends React.ComponentProps<"select"> {
  /** className is applied to the outer wrapper for layout purposes; ref and style reach the <select> */
}

/**
 * A native select, drawn in the terminal's own geometry: the elevation-1
 * surface, a hairline edge, the control radius, and the 2px ink.focus ring.
 *
 * The value a select holds is a name, a state or an identifier, so it speaks
 * in the mono face with tabular figures — a model key or a number lines up
 * with the rest of the readout rather than drifting in the proportional face.
 */
function Select({ className, children, style, ref, ...props }: SelectProps) {
  return (
    <div className={cn("relative", className)}>
      <select
        ref={ref}
        className={cn(
          "h-9 w-full appearance-none py-0 pl-3 pr-9 text-sm disabled:cursor-not-allowed disabled:opacity-50",
          "border border-(--color-border-muted) bg-(--color-bg-elevated) text-(--color-text-primary)",
          CONTROL_RADIUS,
          MONO_ID,
          FOCUS_RING,
        )}
        style={style}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4"
        style={{ color: "var(--color-text-tertiary)" }}
      />
    </div>
  )
}

export { Select }
