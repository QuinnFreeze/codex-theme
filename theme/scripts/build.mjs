import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
export const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export function build(themeRoot=root,settings=null){
 const cfg={scrimAlpha:0.22,activityAlpha:0.78,fit:'cover',wallpaperBrightness:1,wallpaperBlur:0,...(settings??JSON.parse(fs.readFileSync(path.join(themeRoot,'settings.json'),'utf8')))};
 if(typeof cfg.refraction !== 'boolean')throw Error('refraction must be boolean');
 if(typeof cfg.wallpaper!=='string')throw Error('wallpaper must be a relative image path');
 const file=fs.realpathSync(path.resolve(themeRoot,cfg.wallpaper));const relative=path.relative(fs.realpathSync(themeRoot),file);
 if(path.isAbsolute(cfg.wallpaper)||relative.startsWith('..'+path.sep)||relative==='..'||path.isAbsolute(relative))throw Error('wallpaper must stay inside the theme directory');
 const ext=path.extname(file).toLowerCase();const mime={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp'}[ext];
 if(!mime)throw Error('Use jpg/png/webp wallpaper');
 const numeric=['mainAlpha','sidebarAlpha','floatingAlpha','messageAlpha','sidebarBlur','floatingBlur','scrimAlpha','activityAlpha'];for(const k of numeric){if(!Number.isFinite(cfg[k])||cfg[k]<0||cfg[k]>(k.endsWith('Blur')?24:1))throw Error('Invalid '+k)}
 if(typeof cfg.position!=='string'||!/^\d+(\.\d+)?% \d+(\.\d+)?%$/.test(cfg.position)||cfg.position.split(' ').some(v=>parseFloat(v)>100))throw Error('position must be two percentages from 0 to 100');
 if(!['cover','contain'].includes(cfg.fit))throw Error('fit must be cover or contain');
 if(!Number.isFinite(cfg.wallpaperBrightness)||cfg.wallpaperBrightness<0.25||cfg.wallpaperBrightness>2)throw Error('wallpaperBrightness must be 0.25 to 2');
 if(!Number.isFinite(cfg.wallpaperBlur)||cfg.wallpaperBlur<0||cfg.wallpaperBlur>24)throw Error('wallpaperBlur must be 0 to 24');
 if(!fs.statSync(file).isFile()||fs.statSync(file).size>20*1024*1024)throw Error('wallpaper must be an image up to 20 MiB');
 const vars=`:root[data-wynn-glass="on"]{--wynn-wallpaper:url("data:${mime};base64,${fs.readFileSync(file).toString('base64')}");--wynn-position:${cfg.position};--wynn-wallpaper-fit:${cfg.fit};--wynn-wallpaper-brightness:${cfg.wallpaperBrightness};--wynn-wallpaper-blur:${cfg.wallpaperBlur}px;--wynn-main-alpha:${cfg.mainAlpha};--wynn-sidebar-alpha:${cfg.sidebarAlpha};--wynn-sidebar-blur:${cfg.sidebarBlur}px;--wynn-floating-alpha:${cfg.floatingAlpha};--wynn-floating-blur:${cfg.floatingBlur}px;--wynn-message-alpha:${cfg.messageAlpha};--wynn-scrim-alpha:${cfg.scrimAlpha};--wynn-activity-alpha:${cfg.activityAlpha};}`;
 const css=vars+['wallpaper.css','liquid-glass.css','codex-overrides.css'].map(n=>fs.readFileSync(path.join(themeRoot,'styles',n),'utf8')).join('\n');
 return {css,cfg};
}
export function expression(){const{css,cfg}=build();return `(()=>{
 const id='wynn-codex-glass';let style=document.getElementById(id);if(!style){style=document.createElement('style');style.id=id;document.head.appendChild(style)}style.textContent=${JSON.stringify(css)};
 document.documentElement.dataset.wynnGlass='on';document.documentElement.dataset.wynnRefraction=${JSON.stringify(cfg.refraction?'on':'off')};
 document.documentElement.dataset.wynnWallpaperEffects=${JSON.stringify(cfg.wallpaperBrightness!==1||cfg.wallpaperBlur!==0?'on':'off')};
 if(!${JSON.stringify(cfg.refraction)})document.getElementById('wynn-glass-optics')?.remove();
 if(${JSON.stringify(cfg.refraction)}&&!document.getElementById('wynn-glass-optics')){const host=document.createElementNS('http://www.w3.org/2000/svg','svg');host.id='wynn-glass-optics';host.setAttribute('aria-hidden','true');host.style.cssText='position:fixed;width:0;height:0;pointer-events:none';host.innerHTML='<defs><filter id="wynn-edge-lens" x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB"><feImage href="${map()}" x="0" y="0" width="100%" height="100%" preserveAspectRatio="none" result="edge"/><feDisplacementMap in="SourceGraphic" in2="edge" scale="2.2" xChannelSelector="R" yChannelSelector="G"/></filter></defs>';document.body.appendChild(host)}
 return {installed:true,href:location.href,sidebar:!!document.querySelector('.app-shell-left-panel'),main:!!document.querySelector('[data-app-shell-main-surface]')}
 })()`}
/* Neutral midpoint in center; gradients bend only a thin normalized edge band.
   A static SVG map avoids canvas snapshots, render loops, and lens-size observers. */
function map(){let s=`<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><defs><linearGradient id="x"><stop stop-color="#408080"/><stop offset=".055" stop-color="#808080"/><stop offset=".945" stop-color="#808080"/><stop offset="1" stop-color="#c08080"/></linearGradient><linearGradient id="y" x2="0" y2="1"><stop stop-color="#804080"/><stop offset=".055" stop-color="#808080"/><stop offset=".945" stop-color="#808080"/><stop offset="1" stop-color="#80c080"/></linearGradient></defs><rect width="256" height="256" fill="url(#x)"/><rect width="256" height="256" fill="url(#y)" opacity=".5"/></svg>`;return 'data:image/svg+xml;base64,'+Buffer.from(s).toString('base64')}
export const removeExpression=`(()=>{document.getElementById('wynn-codex-glass')?.remove();document.getElementById('wynn-glass-optics')?.remove();delete document.documentElement.dataset.wynnGlass;delete document.documentElement.dataset.wynnRefraction;delete document.documentElement.dataset.wynnWallpaperEffects;return{removed:!document.getElementById('wynn-codex-glass')}})()`;
