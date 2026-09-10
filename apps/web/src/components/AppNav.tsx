import { NavLink } from 'react-router-dom';
import { APP_BRAND } from '../shared/brand';
import { BOARD_KEYS, BOARDS } from '../shared/config/boards';

export function AppNav() {
  return (
    <nav className="app-nav">
      <NavLink to="/quotes" className={({ isActive }) => (isActive ? 'on' : '')}>
        行情
      </NavLink>
      <NavLink to="/terminal" className={({ isActive }) => (isActive ? 'on' : '')}>
        工作台
      </NavLink>
      {/* X v1.3.0 复刻 main v1.4.0、复盘独立；顺序单源，未来 X overrides 演进不改导航。 */}
      {BOARD_KEYS.map((key) => (
        <NavLink key={key} to={BOARDS[key].route}
          className={({ isActive }) => (isActive ? 'on' : '')}
          title={key === 'main' ? undefined : `${BOARDS[key].parameterVersion} · ${BOARDS[key].note ?? ''}`}>
          {BOARDS[key].label}
        </NavLink>
      ))}
      <NavLink to="/review" className={({ isActive }) => (isActive ? 'on' : '')}>
        复盘选币
      </NavLink>
      <NavLink to="/markets" className={({ isActive }) => (isActive ? 'on' : '')}>
        合约清单
      </NavLink>
      <span className="nav-spacer" />
      <span className="nav-brand">{APP_BRAND}</span>
    </nav>
  );
}
