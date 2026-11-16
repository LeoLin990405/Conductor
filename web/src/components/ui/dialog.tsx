import { useTranslation } from 'react-i18next';
import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"
import { closeAutoFocus } from "@/lib/inputModality"
import { useIsMobile } from "@/hooks/useIsMobile"
import { CONTROL_RADIUS, FOCUS_RING, FLOATING_EDGE, FLOATING_FILL, FLOATING_RADIUS, FLOATING_SHADOW, MONO_CHROME } from "./tokens"

// A sheet whose gesture fails to load still opens and closes, it just does not
// swipe. index.html's preload listener reports a chunk a deploy removed.
const DialogSwipe = React.lazy(() =>
  import("./dialog-swipe").catch(() => ({ default: () => null }))
)

const Dialog = DialogPrimitive.Root

function DialogTrigger({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return (
    <DialogPrimitive.Trigger
      className={cn(CONTROL_RADIUS, MONO_CHROME, FOCUS_RING, className)}
      {...props}
    />
  )
}

const DialogPortal = DialogPrimitive.Portal

function DialogClose({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return (
    <DialogPrimitive.Close
      className={cn(CONTROL_RADIUS, MONO_CHROME, FOCUS_RING, className)}
      {...props}
    />
  )
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      className={cn(
        "fixed inset-0 z-1030 bg-(--color-bg-overlay-strong) overlay-fade",
        className
      )}
      {...props} />
  )
}

// Mobile swipe variant: flex column container, no overflow (inner scroll child handles it).
// The sheet is a floating surface like any other: surface elevation 1, the token
// shadow, and a radius capped at radius.overlay. Only the top corners round —
// the sheet is pinned to the bottom edge of the screen.
const DIALOG_MOBILE_SHEET_CLASSES = [
  "fixed left-0 bottom-0 z-1030 flex flex-col w-full max-w-lg",
  FLOATING_EDGE,
  FLOATING_FILL,
  FLOATING_SHADOW,
  "rounded-t-[var(--radius-overlay)]",
  "max-h-[90dvh] sheet-in",
].join(" ")

// Desktop / centered: single-element grid with native overflow scroll.
// Centred through `transform`, not translate-*: the pop-in keyframes carry the
// centring translate in `transform`, and a separate `translate` would stack on
// top of it for the length of the animation.
const DIALOG_CENTERED_CLASSES = [
  "fixed left-[50%] top-[50%] z-1030 grid w-full max-w-lg gap-4 p-6",
  FLOATING_EDGE,
  FLOATING_FILL,
  FLOATING_SHADOW,
  FLOATING_RADIUS,
  "[transform:translate(-50%,-50%)] max-h-[85vh] overflow-y-auto pop-in-center",
].join(" ")

function DialogContent({
  className,
  children,
  variant = 'default',
  onCloseAutoFocus,
  ref,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  /** 'default' = bottom-sheet on mobile, centered on desktop. 'centered' = always centered. */
  variant?: 'default' | 'centered';
}) {
  const isMobile = useIsMobile();
  const { t } = useTranslation();
  const swipeEnabled = isMobile && variant === 'default';

  // Hidden close button ref — clicking it triggers Radix's onOpenChange(false)
  const closeRef = React.useRef<HTMLButtonElement>(null);
  const dismiss = React.useCallback(() => closeRef.current?.click(), []);

  // State-backed nodes so the swipe binding re-attaches on portal remount
  const [containerNode, setContainerNode] = React.useState<HTMLDivElement | null>(null);
  const [contentNode, setContentNode] = React.useState<HTMLDivElement | null>(null);
  const [handleNode, setHandleNode] = React.useState<HTMLDivElement | null>(null);

  // Merge forwarded ref + container state setter
  const containerRefCb = React.useCallback((node: HTMLDivElement | null) => {
    setContainerNode(node);
    if (typeof ref === 'function') ref(node);
    else if (ref) (ref as React.MutableRefObject<HTMLDivElement | null>).current = node;
  }, [ref]);

  // Mobile bottom-sheet with swipe: 2-layer structure
  // Outer: flex column container for positioning + translate transform
  // Inner: flex-1 min-h-0 scroll child for content + touch handling
  if (swipeEnabled) {
    return (
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          ref={containerRefCb}
          className={cn(DIALOG_MOBILE_SHEET_CLASSES, className)}
          onCloseAutoFocus={closeAutoFocus(onCloseAutoFocus)}
          {...props}
        >
          {/* Drag handle */}
          <div
            ref={setHandleNode}
            className="flex justify-center pt-3 pb-1 shrink-0 cursor-grab active:cursor-grabbing"
            style={{ touchAction: 'none' }}
          >
            <div
              className="w-10 h-1 rounded-[var(--radius-control)]"
              style={{ backgroundColor: 'var(--color-border-default)' }}
            />
          </div>
          {/* Scrollable content — mirrors MobileBottomSheet inner div */}
          <div
            ref={setContentNode}
            className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden grid gap-4 px-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] *:min-w-0"
            style={{ overscrollBehaviorY: 'contain' }}
          >
            {children}
          </div>
          {/* Hidden close button for swipe dismiss */}
          <DialogPrimitive.Close ref={closeRef} className="hidden" aria-hidden />
          {/* Content renders only while open, so this loads on first open. */}
          <React.Suspense fallback={null}>
            <DialogSwipe
              container={containerNode}
              content={contentNode}
              handle={handleNode}
              onDismiss={dismiss}
            />
          </React.Suspense>
        </DialogPrimitive.Content>
      </DialogPortal>
    );
  }

  // Desktop bottom-sheet or centered: single-element, native scroll
  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        aria-describedby={undefined}
        ref={ref}
        className={cn(
          DIALOG_CENTERED_CLASSES,
          className
        )}
        onCloseAutoFocus={closeAutoFocus(onCloseAutoFocus)}
        {...props}>
        {children}
        <DialogPrimitive.Close
          aria-keyshortcuts="Escape"
          className={cn(
            'absolute right-4 top-4 opacity-70 transition-opacity hover:opacity-100 disabled:pointer-events-none',
            CONTROL_RADIUS,
            FOCUS_RING,
          )}>
          <X className="h-4 w-4" />
          <span className="sr-only">{t('commonUi.close')}</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

const DialogHeader = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn("flex flex-col space-y-1.5 text-center sm:text-left", className)}
    {...props} />
)
DialogHeader.displayName = "DialogHeader"

/**
 * The buttons under a dialog's body.
 *
 * They wrap: a footer is a flex row on a wide screen and a stack on a narrow
 * one, and a long label (a zh-CN one, or an English one with a sentence in
 * it) wraps inside its own button rather than pushing the row past the edge.
 * `gap` rather than a margin between them, so the space survives the wrap.
 */
const DialogFooter = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col-reverse gap-2 sm:flex-row sm:flex-wrap sm:justify-end",
      className
    )}
    {...props} />
)
DialogFooter.displayName = "DialogFooter"

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn("text-lg font-semibold leading-none tracking-tight", MONO_CHROME, className)}
      {...props} />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn("text-sm text-(--color-text-secondary)", MONO_CHROME, className)}
      {...props} />
  )
}

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogClose,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
}
