/* ============================================================
 * 盘中监控台 · 侧边栏导航配置
 * ------------------------------------------------------------
 * 页面源文件统一放在本仓库的 pages/ 目录下，单一维护。
 * src 用相对 index.html 的路径，指向 pages/ 内的自包含 HTML。
 *
 * 扩展新页面只需两步：
 *   1) 准备好一个自包含的 HTML，放进 pages/ 目录
 *   2) 在下方 PAGES 数组里追加一项，填对 src 路径
 *
 * 字段说明：
 *   id    唯一标识（用于切换，不可重复）
 *   name  侧边栏显示名称
 *   icon  emoji 或文字图标
 *   src   相对 index.html 的页面路径
 * ============================================================ */
const PAGES = [
  { id: 'bull-calc',   name: '牛股计算器', icon: '🐂', src: './pages/bull_calc.html' },
  { id: 'sector-flow', name: '板块资金流', icon: '📊', src: './pages/sector_flow_dashboard.html' },

  // —— 扩展示例（取消注释即可启用）——
  // { id: 'futures', name: '期货持仓', icon: '🔮', src: './pages/xxx.html' },
];
