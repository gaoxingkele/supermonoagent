function (configuration) {
  'use strict';
  if (window.top !== window) return;
  const send = action => {
    location.href = 'sma-update://' + action + '?ticket=' + encodeURIComponent(configuration.ticket)
      + '&revision=' + encodeURIComponent(configuration.revision) + '&generation=' + configuration.generation;
  };
  let healthy = false, restored = false;
  const native = Object.freeze({
    uiReady() { if (healthy) return; healthy = true; send('ready'); },
    restore(state) { if (restored) return; restored = true; window.MobileTerminal?.restoreUIState?.(state); },
    check() { send('check'); }, rollback() { send('rollback'); }
  });
  Object.defineProperty(window, 'NativeUpdates', {value:native, writable:false, configurable:false});
  addEventListener('error', event => { if (!healthy && (event.error || event.target?.tagName === 'SCRIPT')) send('fail'); }, true);
  addEventListener('unhandledrejection', () => { if (!healthy) send('fail'); });
  const initialize = () => {
    if (!window.MobileTerminal || typeof window.MobileTerminal.pause !== 'function') return;
    if (!document.getElementById('native-update-check')) {
      const button = document.createElement('button');button.id='native-update-check';button.textContent='Check updates';button.onclick=native.check;
      const settings=document.getElementById('settings-dialog');if(settings)settings.append(button);
      const header=document.querySelector('header');if(header){const menu=document.createElement('button');menu.id='native-update-menu';menu.textContent='Update';menu.onclick=native.check;header.append(menu);}
    }
    // Native supplies validated UI state and acknowledges after restore; no backend login requirement.
    send('ready');
  };
  addEventListener('DOMContentLoaded',initialize,{once:true});
}
