import { Search } from 'lucide-react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { openPalette } from '@/features/palette/PaletteHost';
import { ConductorMark } from '../brand/ConductorMark';
import { SETTINGS_ITEM, type NavItem } from '../nav/navItems';
import { useNavItems } from '../nav/useNavItems';
import { useNavActive } from '../nav/useNavActive';
import AccountMenu from '../Sidebar/AccountMenu';
import './AppNavSidebar.css';

/** App shell rail: product navigation only. Sessions and workspaces are
 *  implementation details of Super-agent, not navigation destinations. */
export default function AppNavSidebar() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const items = useNavItems();
  const isActive = useNavActive();

  const navRow = (item: NavItem) => {
    const Icon = item.icon;
    const active = isActive(item);
    return (
      <button
        key={item.key}
        type="button"
        className={`app-nav-item ${active ? 'app-nav-item--active' : ''}`}
        onClick={() => navigate(item.key)}
        aria-current={active ? 'page' : undefined}
        data-nav-key={item.key}
        data-testid="app-nav-item"
        title={t(item.labelKey)}
      >
        <span className="app-nav-item-marker" aria-hidden="true">
          {active ? '›' : ''}
        </span>
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
        <span className="app-nav-item-label">{t(item.labelKey)}</span>
      </button>
    );
  };

  return (
    <aside className="app-nav-sidebar" data-chrome data-testid="app-rail">
      <div className="app-nav-sidebar-inner">
        <button
          type="button"
          className="app-nav-brand"
          onClick={() => navigate('/inbox')}
          data-testid="app-rail-brand"
        >
          <ConductorMark className="app-nav-brand-glyph" />
          <span className="app-nav-brand-text">
            <span className="app-nav-wordmark">conductor</span>
            <span className="app-nav-tagline">{t('tnShell.brand.tagline')}</span>
          </span>
        </button>

        <button
          type="button"
          className="app-nav-palette"
          onClick={(event) => openPalette(event.currentTarget)}
          aria-label={t('tnShell.palette.open')}
          aria-keyshortcuts="Control+K Meta+K"
          title={t('tnShell.palette.open')}
          data-testid="shell-palette-trigger"
        >
          <Search className="app-nav-palette-icon" size={14} strokeWidth={1.8} aria-hidden="true" />
          <span className="app-nav-palette-label">{t('tnShell.palette.trigger')}</span>
          <kbd className="app-nav-kbd">{t('tnShell.palette.hint')}</kbd>
        </button>

        <nav className="app-nav-items" aria-label={t('tnShell.nav.primary')}>
          {items.map(navRow)}
        </nav>

        <div className="app-nav-bottom">
          {navRow(SETTINGS_ITEM)}
          <AccountMenu variant="row" />
        </div>
      </div>
    </aside>
  );
}
