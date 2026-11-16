import { useLocation } from 'react-router';
import type { NavItem } from './navItems';

/** Returns a matcher that reports whether a nav item is active for the current pathname. */
export function useNavActive() {
  const { pathname } = useLocation();
  return (item: NavItem): boolean => {
    const paths = [item.key, ...(item.aliases ?? [])];
    return paths.some((path) => (
      item.match === 'prefix'
        ? pathname.startsWith(path)
        : pathname === path || pathname.startsWith(path + '/')
    ));
  };
}
