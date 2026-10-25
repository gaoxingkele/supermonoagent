/* Shared window-label palette and context menu for both browser frontends. */
(function (root) {
  'use strict';
  const presets = [
    {id:'ink', label:'黑底白字', background:'#101010', foreground:'#ffffff'},
    {id:'red', label:'红底白字', background:'#9b1c31', foreground:'#ffffff'},
    {id:'blue', label:'蓝底黄字', background:'#153e9f', foreground:'#ffe866'},
    {id:'green', label:'绿底白字', background:'#14532d', foreground:'#ffffff'},
    {id:'purple', label:'紫底白字', background:'#581c87', foreground:'#ffffff'},
    {id:'amber', label:'橙底黑字', background:'#ffbf69', foreground:'#101010'},
    {id:'cyan', label:'青底黑字', background:'#7dd3fc', foreground:'#101010'}
  ];
  function apply(element, id) {
    const theme = presets.find(item => item.id === id);
    element.classList.toggle('sma-tab-themed', !!theme);
    element.style.setProperty('--tab-background', theme?.background || '');
    element.style.setProperty('--tab-foreground', theme?.foreground || '');
  }
  let dismiss = null;
  function close() { if (dismiss) dismiss(); }
  function open(event, {theme, rename, select}) {
    event.preventDefault();
    close();
    const previous = document.activeElement;
    const menu = document.createElement('div');
    menu.className = 'sma-tab-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', '窗口标签');
    const add = (label, action, role='menuitem') => {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = label; button.setAttribute('role', role);
      button.onclick = () => { close(); action(); };
      menu.append(button); return button;
    };
    add('修改名称…', rename);
    const heading = document.createElement('div');
    heading.className = 'sma-tab-menu-heading'; heading.textContent = '标签配色'; menu.append(heading);
    for (const preset of presets) {
      const choice = add(preset.label, () => select(preset.id), 'menuitemradio');
      choice.className = 'sma-tab-swatch'; apply(choice, preset.id);
      choice.setAttribute('aria-checked', String(theme === preset.id));
      const check = document.createElement('span'); check.textContent = theme === preset.id ? '✓' : '';
      check.setAttribute('aria-hidden', 'true'); choice.append(check);
    }
    add('恢复默认配色', () => select(''));
    const outside = e => { if (!menu.contains(e.target)) close(); };
    const keys = e => {
      if (e.key === 'Escape' || e.key === 'Tab') { close(); if(e.key === 'Escape')e.preventDefault(); }
      else if (['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {
        e.preventDefault();
        const buttons = [...menu.querySelectorAll('button')], index = buttons.indexOf(document.activeElement);
        buttons[e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length-1 :
          (index + (e.key === 'ArrowUp' ? -1 : 1) + buttons.length) % buttons.length].focus();
      }
    };
    dismiss = () => {
      dismiss = null; menu.remove();
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', keys, true);
      root.removeEventListener('resize', close);
      if (menu.contains(document.activeElement) || document.activeElement === document.body) previous?.focus();
    };
    document.body.append(menu);
    menu.style.left = Math.max(8, Math.min(event.clientX, innerWidth-menu.offsetWidth-8)) + 'px';
    menu.style.top = Math.max(8, Math.min(event.clientY, innerHeight-menu.offsetHeight-8)) + 'px';
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', keys, true);
    root.addEventListener('resize', close);
    menu.querySelector('button').focus();
  }
  const api = {presets, apply, open, close};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TabThemes = api;
})(typeof window === 'undefined' ? globalThis : window);
