import { Search } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { openPalette } from '@/features/palette/PaletteHost';
import { SETTINGS_ITEM } from '../nav/navItems';
import { useNavActive } from '../nav/useNavActive';
import { useNavItems } from '../nav/useNavItems';
import './BottomTabBar.css';

export default function BottomTabBar() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const isActive = useNavActive();
  // Settings rides at the end of whichever primary items this user can see.
  const navItems = useNavItems();
  const menuItems = [...navItems, SETTINGS_ITEM];
  const handleItemClick = (path: string) => {
    if (location.pathname === path) return;
    navigate(path);
  };

  return (
    <div className="bottom-tab-bar" data-testid="bottom-tab-bar">
      <div className="bottom-tab-bar-pill clips-focus-ring">
        <button
          type="button"
          className="bottom-tab-item bottom-tab-item--palette"
          onClick={(event) => openPalette(event.currentTarget)}
          aria-label={t('tnShell.palette.open')}
          aria-keyshortcuts="Control+K Meta+K"
          title={t('tnShell.palette.open')}
          data-testid="tab-palette-trigger"
        >
          <Search className="bottom-tab-item-icon" size={16} strokeWidth={1.8} aria-hidden="true" />
          <span className="bottom-tab-label">{t('tnShell.palette.trigger')}</span>
        </button>

        {menuItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item);
          const label = t(item.labelKey);

          return (
            <button
              key={item.key}
              type="button"
              className={`bottom-tab-item ${active ? 'active' : ''}`}
              onClick={() => handleItemClick(item.key)}
              aria-label={label}
              aria-current={active ? 'page' : undefined}
              data-nav-key={item.key}
              data-testid="tab-nav-item"
              title={label}
            >
              <Icon className="bottom-tab-item-icon" size={16} strokeWidth={1.8} aria-hidden="true" />
              <span className="bottom-tab-label">{label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
