import * as React from "react"
import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu"

import { cn } from "@/lib/utils"
import { closeAutoFocus } from "@/lib/inputModality"
import {
  CONTROL_RADIUS,
  FOCUS_RING,
  FOCUS_RING_INWARD,
  FLOATING_EDGE,
  FLOATING_FILL,
  FLOATING_RADIUS,
  FLOATING_SHADOW,
  MONO_CHROME,
} from "./tokens"

const DropdownMenu = DropdownMenuPrimitive.Root

function DropdownMenuTrigger({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Trigger>) {
  return (
    <DropdownMenuPrimitive.Trigger
      className={cn(CONTROL_RADIUS, MONO_CHROME, FOCUS_RING, className)}
      {...props}
    />
  )
}

const DropdownMenuGroup = DropdownMenuPrimitive.Group

/**
 * The one floating fill: surface elevation 1 with the token shadow, a
 * hairline edge and a radius capped at radius.overlay.
 */
const MENU_PANEL = [
  "z-1030 min-w-32 max-h-(--radix-dropdown-menu-content-available-height) overflow-y-auto overflow-x-hidden p-1",
  "text-(--color-text-primary)",
  FLOATING_EDGE,
  FLOATING_FILL,
  FLOATING_SHADOW,
  FLOATING_RADIUS,
].join(" ")

function DropdownMenuContent({
  className,
  sideOffset = 4,
  collisionPadding = 8,
  container,
  onCloseAutoFocus,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content> & {
  container?: HTMLElement | null
}) {
  return (
    <DropdownMenuPrimitive.Portal container={container ?? undefined}>
      <DropdownMenuPrimitive.Content
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        onCloseAutoFocus={closeAutoFocus(onCloseAutoFocus)}
        className={cn(
          // Radix measures the space left to the collision boundary and publishes
          // it as --radix-…-available-height; capping there is what keeps a long
          // menu (the model list) from running off-screen with no way to reach
          // the items past the fold.
          MENU_PANEL,
          "pop-in",
          className
        )}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  )
}

// This tint answers pointer and keyboard alike alongside the focus ring, and
// it is written once: a value that clears contrast has to reach every shape of
// item at the same time.
const ITEM_HIGHLIGHT = "data-highlighted:bg-accent/15"

// "Label ... value >": the label at the left, whatever it currently reads
// right-aligned against the chevron. A setting row exists in two shapes, an
// item where the options expand in place and a sub-trigger where they fly out,
// so the geometry belongs here rather than restated at both call sites.
const SETTING_ROW = "justify-between text-[0.8125rem]"

// Every shape of item shares this box, so a checkbox row lines up with the
// plain rows around it. The ring is drawn inward, because the menu clips at
// its own padding and an outset ring would be cut off.
const ITEM_BOX = [
  "relative flex w-full cursor-default select-none items-center gap-2 px-2.5 py-1.5 text-sm",
  "transition-colors data-disabled:pointer-events-none data-disabled:opacity-50",
  CONTROL_RADIUS,
  MONO_CHROME,
  FOCUS_RING_INWARD,
].join(" ")

export const DESTRUCTIVE_ITEM = "text-destructive data-highlighted:bg-destructive/10 data-highlighted:text-destructive"

const itemVariants = {
  default: ITEM_HIGHLIGHT,
  destructive: DESTRUCTIVE_ITEM,
  setting: `${SETTING_ROW} ${ITEM_HIGHLIGHT}`,
} as const

type ItemVariant = keyof typeof itemVariants

function DropdownMenuItem({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item> & {
  variant?: ItemVariant
}) {
  return (
    <DropdownMenuPrimitive.Item
      className={cn(ITEM_BOX, itemVariants[variant], className)}
      {...props}
    />
  )
}

// Radix gives it role="menuitemcheckbox" and aria-checked from `checked`. The
// caller draws the mark, as it draws an item's icon, so the mark sits in the
// same column as the icons on the rows beside it.
function DropdownMenuCheckboxItem({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.CheckboxItem>) {
  return (
    <DropdownMenuPrimitive.CheckboxItem
      className={cn(ITEM_BOX, ITEM_HIGHLIGHT, className)}
      {...props}
    />
  )
}

function DropdownMenuLabel({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Label>) {
  return (
    <DropdownMenuPrimitive.Label
      className={cn("px-2.5 py-1.5 text-sm font-medium", MONO_CHROME, className)}
      {...props}
    />
  )
}

function DropdownMenuSeparator({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Separator>) {
  return (
    <DropdownMenuPrimitive.Separator
      className={cn("-mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  )
}

const DropdownMenuSub = DropdownMenuPrimitive.Sub

function DropdownMenuSubTrigger({
  className,
  children,
  variant = "default",
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubTrigger> & {
  variant?: "default" | "setting"
}) {
  return (
    <DropdownMenuPrimitive.SubTrigger
      className={cn(
        "flex cursor-default select-none items-center gap-2 px-2.5 py-1.5 text-sm data-[state=open]:bg-accent/15",
        CONTROL_RADIUS,
        MONO_CHROME,
        FOCUS_RING_INWARD,
        ITEM_HIGHLIGHT,
        variant === "setting" && SETTING_ROW,
        className
      )}
      {...props}
    >
      {children}
    </DropdownMenuPrimitive.SubTrigger>
  )
}

// Radix anchors a submenu to its trigger *row*, which the parent panel's
// border + p-1 inset 5px from the panel edge — so the stock offset of 0 opens
// the submenu 5px on top of the panel. Clear that, then add the same 4px gap
// DropdownMenuContent leaves against its own trigger.
function DropdownMenuSubContent({
  className,
  sideOffset = 9,
  collisionPadding = 8,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubContent>) {
  return (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.SubContent
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        className={cn(
          MENU_PANEL,
          "pop-in",
          className
        )}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  )
}

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuGroup,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
}
