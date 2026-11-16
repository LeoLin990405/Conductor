import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTrigger,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../dialog';
import { ModalShell } from '../ModalShell';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../dropdown-menu';
import { Select } from '../select';
import { Button } from '../button';
import { Input } from '../input';
import { ToggleSwitch } from '../switch';
import { RadioChoice, CheckboxChoice } from '../choice';
import { SegmentedControl } from '../segmented-control';
import { Toast, ToastDescription, ToastProvider, ToastTitle, ToastViewport } from '../toast';
import { WizardProgress, WizardExit } from '../wizard';
import * as tokens from '../tokens';
import * as dialogModule from '../dialog';
import * as dropdownMenuModule from '../dropdown-menu';
import * as toastModule from '../toast';
import * as buttonModule from '../button';
import * as inputModule from '../input';
import * as selectModule from '../select';
import * as switchModule from '../switch';
import * as choiceModule from '../choice';
import * as segmentedControlModule from '../segmented-control';

/**
 * The motion a reader who asked for none should get.
 *
 * framer-motion keeps the transition it was handed out of the DOM, so the
 * contract is read from the props the primitive gives it instead: the mock
 * below records every `motion.*` it is rendered with, and the test reads the
 * durations and the initial opacity off that record. Reduced motion is forced
 * on for the whole file, because every entrance in this folder answers to it.
 */
const motionCalls = vi.hoisted(() => [] as { tag: string; props: Record<string, any> }[]);

vi.mock('@/lib/framer', async (importOriginal) => {
  const actual = await importOriginal<Record<string, any>>();
  const { createElement } = await import('react');
  // The motion props are what the test reads, so they are kept out of the DOM
  // rather than rendered: a div cannot take an `animate` prop.
  const MOTION_PROPS = ['initial', 'animate', 'exit', 'transition', 'variants', 'custom'];
  const record = (tag: string) => (props: Record<string, any>) => {
    motionCalls.push({ tag, props });
    const rest = { ...props };
    for (const key of MOTION_PROPS) delete rest[key];
    return createElement('div', { 'data-motion': tag, ...rest });
  };
  return {
    ...actual,
    useReducedMotion: () => true,
    motion: { div: record('div'), span: record('span') },
  };
});

/**
 * The primitives' token geometry.
 *
 * The restyle converged the floating surfaces onto one fill — surface
 * elevation 1 with the token shadow — capped their radii at radius.overlay,
 * put the 2px ink.focus ring on every control, set identifiers and key hints
 * in the mono face, and made a dialog's footer buttons wrap instead of
 * overflowing.
 *
 * These lock that geometry in the source, so a primitive that reaches for its
 * own hex, its own shadow or its own radius again fails here rather than in a
 * screenshot. The classes are asserted rather than the computed style
 * because the stylesheet is not loaded in jsdom: a class that is missing is a
 * class the build would never emit.
 */

const UI_DIR = join(__dirname, '..');
const UI_SOURCES = readdirSync(UI_DIR)
  .filter((name) => /\.(tsx|css)$/.test(name))
  .map((name) => name);
const CONFIRM_DIALOG = join(UI_DIR, '..', 'ConfirmDialog.tsx');

/** Every file the restyle owns, with its text. */
function ownedSources(): { file: string; text: string }[] {
  const files = UI_SOURCES.map((name) => ({
    file: `components/ui/${name}`,
    text: readFileSync(join(UI_DIR, name), 'utf8'),
  }));
  files.push({
    file: 'components/ConfirmDialog.tsx',
    text: readFileSync(CONFIRM_DIALOG, 'utf8'),
  });
  return files;
}

const classOf = (el: Element | null): string => el?.getAttribute('class') ?? '';

function expectClasses(el: Element | null, expected: string[]) {
  const classes = classOf(el);
  for (const cls of expected) {
    expect(classes, `class list of ${el?.tagName ?? 'element'}`).toContain(cls);
  }
}

describe('the shared token vocabulary', () => {
  it('names the one floating fill, the token shadow and the radius cap', () => {
    expect(tokens.FLOATING_FILL).toBe('bg-(--color-bg-elevated)');
    expect(tokens.FLOATING_SHADOW).toBe('shadow-(--shadow-elevated)');
    expect(tokens.FLOATING_RADIUS).toBe('rounded-[var(--radius-overlay)]');
    expect(tokens.FLOATING_SURFACE).toContain(tokens.FLOATING_FILL);
    expect(tokens.FLOATING_SURFACE).toContain(tokens.FLOATING_SHADOW);
    expect(tokens.FLOATING_SURFACE).toContain(tokens.FLOATING_RADIUS);
  });

  it('names the 2px ink.focus ring', () => {
    expect(tokens.FOCUS_RING).toBe('ui-focus-ring');
    expect(tokens.FOCUS_RING_INWARD).toBe('ui-focus-ring-inward');
  });

  it('paints the ring in the layer the app keeps its focus defaults in', () => {
    // The ring is painted by the primitives' own stylesheet, inside
    // `@layer focus-defaults` -- the layer `styles/tokens.css` keeps the
    // baseline ring in. A Tailwind `focus-visible:outline-*` utility would be
    // unlayered, and an unlayered rule beats every layered one, so a shared
    // primitive naming it that way would take the ring away from the auth
    // surface, which answers focus in its own language.
    const css = readFileSync(join(UI_DIR, 'focus-ring.css'), 'utf8');
    expect(css).toContain('@layer focus-defaults');
    expect(css).toContain('.ui-focus-ring:focus-visible');
    expect(css).toContain('.ui-focus-ring-inward[class]:focus-visible');
    expect(css).toContain('outline-width: var(--border-focus)');
    expect(css).toContain('outline-color: var(--color-focus-ring)');
    expect(css).toContain('outline-offset: -2px');
    // The offset the outset ring uses is the app's own token, not a literal.
    expect(readFileSync(join(UI_DIR, '..', '..', 'styles', 'tokens.css'), 'utf8')).toContain(
      'outline-offset: var(--focus-offset)',
    );
  });

  it('names the mono face for ids and key hints', () => {
    expect(tokens.MONO_CHROME).toContain('font-mono');
    expect(tokens.MONO_ID).toContain('font-mono');
    expect(tokens.MONO_KBD).toContain('font-mono');
    expect(tokens.KBD_CHIP).toContain(tokens.MONO_KBD);
    expect(tokens.KBD_CHIP).toContain('rounded-[var(--radius-control)]');
  });

  it('reads only tokens — no hex, no rgb() — in the shared vocabulary', () => {
    for (const [name, value] of Object.entries(tokens)) {
      if (typeof value !== 'string') continue;
      expect(value, name).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(value, name).not.toMatch(/\brgba?\(/);
    }
  });
});

describe('the floating surfaces', () => {
  it('gives the dialog the one fill, the token shadow and the capped radius', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Title</DialogTitle>
          </DialogHeader>
          <DialogDescription>Body</DialogDescription>
        </DialogContent>
      </Dialog>,
    );
    const panel = document.querySelector('[role="dialog"]');
    expect(panel).not.toBeNull();
    expectClasses(panel, [
      tokens.FLOATING_FILL,
      tokens.FLOATING_SHADOW,
      tokens.FLOATING_RADIUS,
      'border',
    ]);
  });

  it('gives the modal shell the same fill and the token shadow', () => {
    render(
      <ModalShell labelId="shell-title" title="Title" onClose={() => {}}>
        body
      </ModalShell>,
    );
    const panel = document.querySelector('[role="dialog"]');
    expect(panel).not.toBeNull();
    expect(panel?.getAttribute('style')).toContain('var(--color-bg-elevated)');
    expect(panel?.getAttribute('style')).toContain('var(--shadow-elevated)');
    // The radius cap, on both shapes: a sheet rounds only its top corners, a
    // centred panel rounds all four, and neither passes radius.overlay.
    expect(classOf(panel)).toMatch(/rounded(-t)?-\[var\(--radius-overlay\)\]/);
  });

  it('gives the dropdown menu and its submenu the same fill', () => {
    render(
      <DropdownMenu open modal={false}>
        <DropdownMenuTrigger>Open</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>Item</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    const panel = document.querySelector('[data-radix-menu-content]');
    expect(panel).not.toBeNull();
    expectClasses(panel, [
      tokens.FLOATING_FILL,
      tokens.FLOATING_SHADOW,
      tokens.FLOATING_RADIUS,
      'border',
    ]);
  });

  it('gives the toast the same fill', () => {
    render(
      <ToastProvider>
        <Toast open>
          <ToastTitle>Toast title</ToastTitle>
          <ToastDescription>Toast body</ToastDescription>
        </Toast>
        <ToastViewport />
      </ToastProvider>,
    );
    const toast = screen.getByText('Toast title').closest('li');
    expect(toast).not.toBeNull();
    expectClasses(toast, [
      tokens.FLOATING_FILL,
      tokens.FLOATING_SHADOW,
      tokens.FLOATING_RADIUS,
      'border',
    ]);
  });

  it('gives the select the same fill as the floating surfaces', () => {
    render(
      <Select aria-label="model">
        <option value="a">model-a</option>
      </Select>,
    );
    const select = screen.getByLabelText('model');
    expectClasses(select, [tokens.FLOATING_FILL, 'border']);
  });
});

describe('the radius cap', () => {
  it('keeps every radius in the primitives at or below radius.overlay', () => {
    // A radius utility above the cap: `rounded-xl` (12px), `rounded-2xl`,
    // `rounded-3xl`, `rounded-t-3xl` and the theme's own `rounded-lg` on a
    // dialog or a shell. `rounded-md` and `rounded-sm` are this theme's
    // 0px and invalid, and `rounded-full` is a circle or a knob — a shape
    // rather than a radius, and checked separately below.
    const overCap = /\brounded-(?:t-)?(?:xl|2xl|3xl)\b/;
    const offenders: string[] = [];
    for (const { file, text } of ownedSources()) {
      text.split('\n').forEach((line, i) => {
        if (overCap.test(line)) offenders.push(`${file}:${i + 1} → ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it('drops the radii the plan named: no rounded-t-3xl and no rounded-lg on the dialog or the shell', () => {
    for (const name of ['dialog.tsx', 'ModalShell.tsx']) {
      const text = readFileSync(join(UI_DIR, name), 'utf8');
      expect(text, name).not.toMatch(/rounded-t-3xl/);
      expect(text, name).not.toMatch(/\brounded-lg\b/);
    }
  });

  it('drops the page-ground fill the dialog used to paint', () => {
    for (const name of ['dialog.tsx', 'ModalShell.tsx']) {
      const text = readFileSync(join(UI_DIR, name), 'utf8');
      expect(text, name).not.toMatch(/bg-background/);
    }
  });

  it('keeps the circles to the marks that are circles', () => {
    // A circle is a shape: the radio mark and its dot, the switch knob, the
    // wizard's progress dot. Nothing else may round to a pill.
    const allowed = new Set(['choice.tsx', 'switch.tsx', 'wizard.tsx']);
    const offenders: string[] = [];
    for (const { file, text } of ownedSources()) {
      const name = file.replace('components/ui/', '');
      if (allowed.has(name)) continue;
      text.split('\n').forEach((line, i) => {
        if (/rounded-full/.test(line)) offenders.push(`${file}:${i + 1} → ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it('caps the pill that floats over the page', () => {
    const css = readFileSync(join(UI_DIR, 'ContextOverflowPill.css'), 'utf8');
    expect(css).toContain('border-radius: var(--radius-overlay)');
    expect(css).not.toMatch(/border-radius:\s*999px/);
  });
});

describe('the focus ring', () => {
  it('puts the 2px ink.focus ring on every control', () => {
    render(
      <div>
        <Button>Button</Button>
        <Input aria-label="field" />
        <Select aria-label="model">
          <option value="a">model-a</option>
        </Select>
        <ToggleSwitch checked={false} onChange={() => {}} ariaLabel="toggle" />
        <SegmentedControl value="a" onChange={() => {}} options={[{ value: 'a', label: 'A' }]} />
        <Dialog open modal={false}>
          <DialogTrigger>Open dialog</DialogTrigger>
          <DialogContent>
            <DialogTitle>Dialog</DialogTitle>
            <DialogClose>Dismiss dialog</DialogClose>
          </DialogContent>
        </Dialog>
        <DropdownMenu open modal={false}>
          <DropdownMenuTrigger>Open</DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuItem>Item</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>,
    );

    const ring = [tokens.FOCUS_RING];
    expectClasses(screen.getByRole('button', { name: 'Button' }), ring);
    expectClasses(screen.getByLabelText('field'), ring);
    expectClasses(screen.getByLabelText('model'), ring);
    expectClasses(screen.getByRole('switch'), ring);
    expectClasses(screen.getByRole('button', { name: 'Open dialog' }), ring);
    expectClasses(screen.getByRole('button', { name: 'Dismiss dialog' }), ring);
    expectClasses(screen.getByRole('button', { name: 'Open' }), ring);
    expectClasses(screen.getByRole('button', { name: 'A' }), [tokens.FOCUS_RING_INWARD]);
    // A menu item lives in a panel that clips the ring, so it wears the
    // inward one -- the ring travels with the item the user is arrowing over.
    expectClasses(screen.getByRole('menuitem', { name: 'Item' }), [tokens.FOCUS_RING_INWARD]);
  });

  it('draws the ring inward where a box clips the outset one', () => {
    render(
      <SegmentedControl value="a" onChange={() => {}} options={[{ value: 'a', label: 'A' }]} />,
    );
    expect(classOf(screen.getByRole('button', { name: 'A' }))).toContain(tokens.FOCUS_RING_INWARD);
    // The row itself clips, so the ring cannot be drawn outside it.
    expect(classOf(screen.getByRole('group'))).toContain('clips-focus-ring');
  });

  it('keeps the ring on the choice row, which rings for the field it holds', () => {
    render(
      <label>
        <CheckboxChoice checked={false} onCheckedChange={() => {}}>
          Choice
        </CheckboxChoice>
      </label>,
    );
    // `rings-within` is the app's mechanism for a control that fills a
    // bordered container: the container draws the ink.focus ring for it.
    expect(classOf(screen.getByText('Choice').closest('label'))).toContain('rings-within');
  });
});

describe('the mono face', () => {
  it('sets identifiers and key hints in mono', () => {
    render(
      <div>
        <Button>Button</Button>
        <Input aria-label="field" />
        <Select aria-label="model">
          <option value="a">model-a</option>
        </Select>
        <SegmentedControl value="a" onChange={() => {}} options={[{ value: 'a', label: 'A' }]} />
      </div>,
    );
    expect(classOf(screen.getByRole('button', { name: 'Button' }))).toContain('font-mono');
    expect(classOf(screen.getByLabelText('field'))).toContain('font-mono');
    // A select holds a name, a state or an identifier, so it speaks in mono
    // with tabular figures.
    expect(classOf(screen.getByLabelText('model'))).toContain('font-mono');
    expect(classOf(screen.getByLabelText('model'))).toContain('tabular-nums');
    expect(classOf(screen.getByRole('button', { name: 'A' }))).toContain('font-mono');
  });

  it('sets the chrome of a dialog and a toast in mono', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Title</DialogTitle>
          </DialogHeader>
          <DialogDescription>Body</DialogDescription>
        </DialogContent>
      </Dialog>,
    );
    expect(classOf(screen.getByText('Title'))).toContain('font-mono');
    expect(classOf(screen.getByText('Body'))).toContain('font-mono');
  });
});

describe('the dialog footer', () => {
  it('wraps its buttons instead of pushing them past the edge', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Title</DialogTitle>
          </DialogHeader>
          <DialogFooter>
            <Button>Cancel</Button>
            <Button>Confirm</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    );
    const footer = screen.getByRole('button', { name: 'Cancel' }).parentElement;
    expect(classOf(footer)).toContain('flex-wrap');
    expect(classOf(footer)).toContain('gap-2');
  });
});

describe('the entrances', () => {
  it('uses the shared entrance animations, which the reduced-motion rule shortens', () => {
    const animations = readFileSync(join(UI_DIR, '..', '..', 'styles', 'animations.css'), 'utf8');
    const reduced = animations.slice(animations.indexOf('@media (prefers-reduced-motion: reduce)'));
    for (const name of ['pop-in', 'pop-in-center', 'sheet-in', 'toast-slide']) {
      expect(animations, name).toContain(`.${name}[data-state='open']`);
      expect(reduced, name).toContain(`.${name}[data-state='open']`);
    }
  });

  it('removes the wizard entrances when reduced motion is requested', () => {
    const css = readFileSync(join(UI_DIR, 'wizard.css'), 'utf8');
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(reduced).toContain('.wizard-mark-in,');
    expect(reduced).toContain('.wizard-fade-in {');
    expect(reduced).toContain('animation: none;');
    expect(reduced).not.toContain('animation-name: wizard-fade-in');
  });

  it('holds the transitions of the primitives still when a reader asked for that', () => {
    render(
      <ToastProvider>
        <Toast open>
          <ToastTitle>Toast title</ToastTitle>
        </Toast>
        <ToastViewport />
      </ToastProvider>,
    );
    expect(classOf(screen.getByText('Toast title').closest('li'))).toContain(
      'motion-reduce:transition-none',
    );
  });

  it('places the modal shell and its backdrop outright, with no duration and no fade', () => {
    // The terminal-native contract (docs/design/terminal-native/system/SPEC.md):
    // while reduced motion holds, every duration is 0 and no fade runs. A fade
    // that takes no time is not a fade, and a panel that starts at its
    // resting opacity was never fading in.
    motionCalls.length = 0;
    render(
      <ModalShell labelId="shell-title" title="Title" onClose={() => {}}>
        body
      </ModalShell>,
    );
    const rendered = [
      ['backdrop', motionCalls.find((call) => call.props.className?.includes('fixed inset-0'))],
      ['panel', motionCalls.find((call) => call.props.role === 'dialog')],
    ] as const;

    for (const [what, call] of rendered) {
      expect(call, `the ${what} was rendered`).toBeTruthy();
      const props = call?.props;
      expect(props?.transition?.duration, `${what}: enter duration`).toBe(0);
      expect(props?.exit?.transition?.duration, `${what}: exit duration`).toBe(0);
      expect(props?.initial?.opacity, `${what}: starts opaque`).toBe(1);
      expect(props?.animate?.opacity, `${what}: rests opaque`).toBe(1);
    }
  });
});

describe('the contrast a failed toast keeps', () => {
  const TOKENS_CSS = readFileSync(join(UI_DIR, '..', '..', 'styles', 'terminal-native.css'), 'utf8');

  /** The custom properties one theme declares. */
  function themeTokens(theme: 'dark' | 'light'): Map<string, string> {
    const light = TOKENS_CSS.indexOf('[data-theme="light"]');
    const block =
      theme === 'dark'
        ? TOKENS_CSS.slice(TOKENS_CSS.indexOf(':root {'), light)
        : TOKENS_CSS.slice(light, TOKENS_CSS.indexOf('@media'));
    const out = new Map<string, string>();
    for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
      out.set(match[1], match[2].trim());
    }
    return out;
  }

  const channels = (hex: string) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const linear = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  const luminance = (c: number[]) =>
    0.2126 * linear(c[0]) + 0.7152 * linear(c[1]) + 0.0722 * linear(c[2]);
  const contrast = (a: string, b: string) => {
    const [hi, lo] = [luminance(channels(a)), luminance(channels(b))].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  /** An ink painted at less than full strength over a surface. */
  const washed = (ink: string, surface: string, alpha: number) =>
    channels(ink)
      .map((v, i) => alpha * v + (1 - alpha) * channels(surface)[i])
      .map((v) => Math.round(v * 255))
      .map((v) => v.toString(16).padStart(2, '0'))
      .join('');

  it('keeps the error ink at 4.5:1 or better on the elevated surface', () => {
    for (const theme of ['dark', 'light'] as const) {
      const tokensFor = themeTokens(theme);
      const surface = tokensFor.get('--surface-elevated')!;
      const ink = tokensFor.get('--ink-error')!;
      expect(contrast(surface, ink), `${theme}: error ink on the elevated surface`).toBeGreaterThanOrEqual(
        4.5,
      );
    }
  });

  it('does not dim the description of a failed toast', () => {
    // Dimmed, the same ink falls under the floor in the light theme (4.11:1),
    // which is why a failed toast keeps its description at full strength.
    const light = themeTokens('light');
    const surface = light.get('--surface-elevated')!;
    const ink = light.get('--ink-error')!;
    expect(contrast(surface, washed(ink, surface, 0.9))).toBeLessThan(4.5);

    render(
      <ToastProvider>
        <Toast open variant="destructive">
          <ToastTitle>Failed</ToastTitle>
          <ToastDescription>It did not work</ToastDescription>
        </Toast>
        <ToastViewport />
      </ToastProvider>,
    );
    const description = screen.getByText('It did not work');
    expect(classOf(description)).toContain('group-[.destructive]:opacity-100');
    expect(classOf(description.closest('li'))).toContain('destructive');
  });
});

describe('the primitives keep their shape', () => {
  it('exports the same names', () => {
    const expected: Record<string, string[]> = {
      dialog: [
        'Dialog',
        'DialogPortal',
        'DialogOverlay',
        'DialogClose',
        'DialogTrigger',
        'DialogContent',
        'DialogHeader',
        'DialogFooter',
        'DialogTitle',
        'DialogDescription',
      ],
      dropdownMenu: [
        'DropdownMenu',
        'DropdownMenuTrigger',
        'DropdownMenuContent',
        'DropdownMenuItem',
        'DropdownMenuCheckboxItem',
        'DropdownMenuLabel',
        'DropdownMenuSeparator',
        'DropdownMenuGroup',
        'DropdownMenuSub',
        'DropdownMenuSubTrigger',
        'DropdownMenuSubContent',
        'DESTRUCTIVE_ITEM',
      ],
      toast: [
        'ToastProvider',
        'ToastViewport',
        'Toast',
        'ToastTitle',
        'ToastDescription',
        'ToastClose',
        'ToastAction',
      ],
      button: ['Button'],
      input: ['Input'],
      select: ['Select'],
      switch: ['ToggleSwitch'],
      choice: ['RadioChoice', 'CheckboxChoice'],
      segmentedControl: ['SegmentedControl'],
    };
    const modules: Record<string, Record<string, unknown>> = {
      dialog: dialogModule,
      dropdownMenu: dropdownMenuModule,
      toast: toastModule,
      button: buttonModule,
      input: inputModule,
      select: selectModule,
      switch: switchModule,
      choice: choiceModule,
      segmentedControl: segmentedControlModule,
    };
    for (const [name, names] of Object.entries(expected)) {
      expect(Object.keys(modules[name]).sort(), name).toEqual([...names].sort());
    }
  });

  it('keeps the data-testid values', () => {
    render(
      <SegmentedControl
        value="a"
        onChange={() => {}}
        options={[{ value: 'a', label: 'A', testId: 'segment-a' }]}
      />,
    );
    expect(screen.getByTestId('segment-a')).toBeInTheDocument();
  });

  it('keeps the wizard progress count and the exit test id', () => {
    render(<WizardProgress steps={['One', 'Two']} current={0} label="Progress" />);
    const count = screen.getByTestId('wizard-progress-count');
    expect(count).toBeInTheDocument();
    expectClasses(count, ['font-mono', 'tabular-nums']);

    render(<WizardExit label="Leave" onClick={() => {}} testId="wizard-exit" />);
    expect(screen.getByTestId('wizard-exit')).toBeInTheDocument();
  });

  it('keeps the props the pages call it with', () => {
    render(
      <RadioChoice name="group" value="a" checked onSelect={() => {}}>
        Radio
      </RadioChoice>,
    );
    expect(screen.getByRole('radio', { name: 'Radio' })).toBeInTheDocument();
  });
});

describe('the sources', () => {
  it('paints no hex and no Tailwind red', () => {
    const offenders: string[] = [];
    for (const { file, text } of ownedSources()) {
      text.split('\n').forEach((line, i) => {
        if (/#[0-9a-fA-F]{3,8}\b/.test(line) && !/url\(/.test(line)) {
          offenders.push(`${file}:${i + 1} → ${line.trim()}`);
        }
        if (/\b(?:bg|text|border|ring)-(?:red|rose)-\d{2,3}\b/.test(line)) {
          offenders.push(`${file}:${i + 1} → ${line.trim()}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });

  it('reads no colour that no theme declares', () => {
    const tokensCss = readFileSync(join(UI_DIR, '..', '..', 'styles', 'tokens.css'), 'utf8');
    const declared = new Set(
      [...tokensCss.matchAll(/(--color-[\w-]+)\s*:/g)].map((m) => m[1]),
    );
    const orphans: string[] = [];
    for (const { file, text } of ownedSources()) {
      text.split('\n').forEach((line, i) => {
        for (const match of line.matchAll(/(?:var)?\((--color-[\w-]+)/g)) {
          if (!declared.has(match[1])) orphans.push(`${file}:${i + 1} → ${match[1]}`);
        }
      });
    }
    expect(orphans).toEqual([]);
  });
});
