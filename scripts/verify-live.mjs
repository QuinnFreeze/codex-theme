// Read-only inspection of the real themed app. Metrics omit conversation text;
// local screenshots can include the visible conversation and stay in evidence/.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const theme=process.argv[2]||path.join(os.homedir(),'Library/Application Support/CodexTheme');
const output=path.resolve(process.argv[3]||'evidence/plugin-live');
fs.mkdirSync(output,{recursive:true});
const {Connection,pages}=await import(pathToFileURL(path.join(theme,'scripts/runtime.mjs')));
const cfg=JSON.parse(fs.readFileSync(path.join(theme,'settings.json'),'utf8'));
const results=[];
for(const page of await pages(9236)){
 const c=new Connection(page.webSocketDebuggerUrl);
 try{
  const metrics=await c.eval(`(()=>{
   const body=getComputedStyle(document.body),layer=getComputedStyle(document.body,'::before');
   const editor=document.querySelector('[contenteditable="true"]');
   const root=document.documentElement;
   return {theme:root.dataset.wynnGlass,effects:root.dataset.wynnWallpaperEffects,styles:document.querySelectorAll('#wynn-codex-glass').length,
    main:!!document.querySelector('[data-app-shell-main-surface]'),sidebar:!!document.querySelector('.app-shell-left-panel'),editable:!!editor&&editor.getAttribute('contenteditable')==='true',
    fit:body.backgroundSize,position:body.backgroundPosition,filter:layer.filter,layerImage:layer.backgroundImage.startsWith('url("data:image/'),
    wallpaper:body.getPropertyValue('--wynn-wallpaper').startsWith('url("data:image/'),width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,
    style:document.getElementById('wynn-codex-glass')?.textContent||''};
  })()`);
  metrics.cssSha256=createHash('sha256').update(metrics.style).digest('hex');delete metrics.style;
  // Leave private settings-page query tokens out of the inspection report.
  let url='';try{const clean=new URL(page.url);clean.search='';clean.hash='';clean.username='';clean.password='';url=clean.href}catch{}
  results.push({url,id:page.id,...metrics});
  if(metrics.main){const shot=await c.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,'codex.png'),Buffer.from(shot.data,'base64'))}
 }finally{c.close()}
}
const wallpaper=path.resolve(theme,cfg.wallpaper);
const report={settings:cfg,wallpaperSha256:createHash('sha256').update(fs.readFileSync(wallpaper)).digest('hex'),windows:results};
fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report));
