import { NAV_ITEMS, type NavItem } from './navItems';

/** The primary nav items, in NAV_ITEMS order. */
export function useNavItems(): NavItem[] {
  return NAV_ITEMS;
}
