import * as React from "react"
import { cn } from "../../lib/utils"
import { CONTROL_RADIUS, FOCUS_RING, MONO_CHROME } from "./tokens"

type ButtonVariant = "default" | "destructive" | "outline" | "secondary" | "ghost" | "link"
type ButtonSize = "default" | "sm" | "lg" | "icon"

export interface ButtonProps extends React.ComponentProps<"button"> {
  variant?: ButtonVariant
  size?: ButtonSize
}

/**
 * A button in the terminal's own geometry: the control radius, the mono face,
 * and the 2px ink.focus ring the shared focus rule paints — named here as
 * well, so a variant cannot lose it.
 */
function Button({ className, variant = "default", size = "default", ...props }: ButtonProps) {
  // Accent discipline (DESIGN.md): the amber is annotation-only — never a
  // button fill, hover wash, or focus ring. Primary is the neutral
  // near-white/near-charcoal pair; hovers are the neutral tint.
  const variants: Record<ButtonVariant, string> = {
    default:
      "bg-(--color-btn-primary-bg) text-(--color-btn-primary-text) hover:opacity-90",
    destructive:
      "bg-(--color-btn-danger) text-(--color-text-on-accent) hover:opacity-90",
    outline:
      "border border-(--color-border-muted) bg-(--color-bg-elevated) text-(--color-text-primary) hover:bg-(--color-bg-hover)",
    secondary:
      "bg-(--color-bg-canvas) text-(--color-text-primary) hover:bg-(--color-bg-hover)",
    ghost: "text-(--color-text-primary) hover:bg-(--color-bg-hover)",
    link: "text-(--color-accent-primary) underline-offset-4 hover:underline",
  }

  const sizes: Record<ButtonSize, string> = {
    default: "h-10 px-4 py-2",
    sm: "h-9 px-3",
    lg: "h-11 px-8",
    icon: "h-10 w-10",
  }

  return (
    <button
      className={cn(
        "inline-flex items-center justify-center text-sm font-medium transition-colors",
        "disabled:pointer-events-none disabled:opacity-50",
        CONTROL_RADIUS,
        MONO_CHROME,
        FOCUS_RING,
        variants[variant],
        sizes[size],
        className
      )}
      {...props}
    />
  )
}

export { Button }
