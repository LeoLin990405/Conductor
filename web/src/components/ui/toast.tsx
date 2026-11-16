import * as React from "react"
import * as ToastPrimitives from "@radix-ui/react-toast"
import { cva, type VariantProps } from "class-variance-authority"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"
import { aboveDialogs } from "@/hooks/useDialogA11y"
import {
  CONTROL_RADIUS,
  FOCUS_RING,
  FLOATING_EDGE,
  FLOATING_FILL,
  FLOATING_RADIUS,
  FLOATING_SHADOW,
  MONO_CHROME,
} from "./tokens"

const ToastProvider = ToastPrimitives.Provider

function ToastViewport({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Viewport>) {
  // Above every modal layer, so a toast raised from inside a dialog is not
  // dimmed under its backdrop. The empty viewport still spans its padding, so it
  // passes pointer events through and only the toasts themselves take them.
  // Being above them is also why an open dialog lets a toast keep focus.
  //
  // `--toast-lift` is written by `useToastInset` for any element floating at
  // the bottom of the page, so a toast stacks above its buttons rather than
  // over them. Unset everywhere else, which leaves the edge at 0.
  return (
    <ToastPrimitives.Viewport
      {...aboveDialogs}
      className={cn(
        "pointer-events-none fixed top-0 z-1050 flex max-h-screen w-full flex-col-reverse p-4 sm:bottom-[var(--toast-lift,0px)] sm:right-0 sm:top-auto sm:flex-col sm:transition-[bottom] sm:duration-200 motion-reduce:transition-none md:max-w-[420px]",
        className
      )}
      {...props}
    />
  )
}

// The gap sits before every child after the first, not after every child but
// the last (space-x-4): the close button is the last child and is absolutely
// placed, so space-x would leave its gap on the action beside it. Positioning
// goes through `transform` because the enter and exit keyframes animate
// `transform`, which would stack on the separate `translate` property that
// translate-x-* sets instead of replacing it.
//
// A toast is a floating surface like any other: the one elevation-1 fill with
// the token shadow, a hairline edge and a radius capped at radius.overlay.
const toastVariants = cva(
  [
    "group pointer-events-auto relative flex w-full items-center justify-between",
    "[&>:not([hidden])~:not([hidden])]:ml-4 overflow-hidden p-6 pr-8",
    "transition-all motion-reduce:transition-none",
    "data-[swipe=cancel]:[transform:translateX(0)] data-[swipe=end]:[transform:translateX(var(--radix-toast-swipe-end-x))]",
    "data-[swipe=move]:[transform:translateX(var(--radix-toast-swipe-move-x))] data-[swipe=move]:transition-none",
    "toast-slide",
  ].join(" "),
  {
    variants: {
      variant: {
        // The one fill. The destructive variant keeps it and marks itself with
        // the danger ink and its hairline, so a failure still reads as a
        // failure without becoming a second surface.
        default: [FLOATING_EDGE, FLOATING_FILL, FLOATING_SHADOW, FLOATING_RADIUS, "text-(--color-text-primary)"].join(" "),
        destructive: [
          // `destructive` is the group's own name: the action and the close
          // button inside a failed toast find it through `group-[.destructive]`.
          "destructive border border-(--color-border-loss) bg-(--color-bg-elevated) shadow-(--shadow-elevated)",
          FLOATING_RADIUS,
          "text-(--color-loss)",
        ].join(" "),
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Toast({
  className,
  variant,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Root> & VariantProps<typeof toastVariants>) {
  return (
    <ToastPrimitives.Root
      className={cn(toastVariants({ variant }), className)}
      {...props}
    />
  )
}

function ToastAction({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Action>) {
  return (
    <ToastPrimitives.Action
      className={cn(
        "inline-flex h-8 shrink-0 items-center justify-center border bg-transparent px-3 text-sm font-medium",
        "transition-colors hover:bg-(--color-bg-hover) disabled:pointer-events-none disabled:opacity-50",
        "group-[.destructive]:border-(--color-border-loss) hover:group-[.destructive]:bg-(--color-bg-hover)",
        CONTROL_RADIUS,
        MONO_CHROME,
        FOCUS_RING,
        className
      )}
      {...props}
    />
  )
}

// Revealed by hovering the toast, which a touch screen cannot do: there, and
// while anything in the toast holds focus, it is simply shown.
function ToastClose({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Close>) {
  return (
    <ToastPrimitives.Close
      className={cn(
        "absolute right-2 top-2 p-1 text-(--color-text-tertiary) opacity-0 transition-opacity hover:text-(--color-text-primary) focus:opacity-100",
        "group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100",
        "group-[.destructive]:text-(--color-loss)",
        CONTROL_RADIUS,
        FOCUS_RING,
        className
      )}
      toast-close=""
      {...props}
    >
      <X className="h-4 w-4" />
    </ToastPrimitives.Close>
  )
}

function ToastTitle({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Title>) {
  return (
    <ToastPrimitives.Title
      className={cn("text-sm font-semibold", MONO_CHROME, className)}
      {...props}
    />
  )
}

function ToastDescription({
  className,
  ...props
}: React.ComponentProps<typeof ToastPrimitives.Description>) {
  return (
    <ToastPrimitives.Description
      className={cn(
        "text-sm opacity-90",
        // A failed toast says it in the error ink, and that ink only clears
        // 4.5:1 on the elevated surface at full strength — in the light theme
        // it lands at 4.11:1 once it is dimmed. So a description in a failed
        // toast keeps its ink whole; the ordinary one stays quiet.
        "group-[.destructive]:opacity-100",
        MONO_CHROME,
        className
      )}
      {...props}
    />
  )
}

export {
  ToastProvider,
  ToastViewport,
  Toast,
  ToastTitle,
  ToastDescription,
  ToastClose,
  ToastAction,
}
