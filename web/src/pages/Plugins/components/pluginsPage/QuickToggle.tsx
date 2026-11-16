import { Power } from 'lucide-react';
import { Loader } from '@/components/ui/loader';

/**
 * The cell's one-click switch: the menu's power verb without opening the menu.
 *
 * A plugin is switched on or off far more often than it is updated or removed,
 * and until now that cost two clicks (kebab, then the item) on every row. It
 * stays quiet until the cell is pointed at or focused, like the menu, so the
 * trailing state is still the only always-visible thing a cell says; while the
 * write is in flight it holds and shows the spinner so a second press cannot
 * send the opposite of the first.
 */
export function QuickToggle({
  on,
  pending,
  label,
  onToggle,
}: {
  /** What the item is now. Kept for the callers that word the verb from it;
   *  the button itself is a plain action, not a pressed toggle. */
  on: boolean;
  /** The write is in flight; the button holds and shows it. */
  pending: boolean;
  /** Accessible name and tooltip, the row's own switch wording. */
  label: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="surface-quick-toggle"
      aria-label={label}
      title={label}
      disabled={pending}
      onClick={onToggle}
    >
      {pending ? (
        <Loader size={13} className="text-current" />
      ) : (
        <Power className="h-3.5 w-3.5" aria-hidden />
      )}
    </button>
  );
}
