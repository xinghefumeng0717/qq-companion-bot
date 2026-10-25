// 把 Markdown 里的 Mermaid 图渲染成 PNG（GitHub 手机 App 不渲染 Mermaid，文档里放的是图片）。
//
// 用法（需要 Node 和 Playwright 自带的 Chromium，联网拉 mermaid）：
//   node scripts/util/render_mermaid.js docs/ARCHITECTURE.md docs/images architecture
// 第 N 个 mermaid 代码块 → docs/images/architecture-NN.png。改了图的源码就重跑一次。
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [mdPath, outDir, prefix] = process.argv.slice(2);
if (!mdPath || !outDir || !prefix) {
  console.error('用法: node scripts/util/render_mermaid.js <md 文件> <输出目录> <文件名前缀>');
  process.exit(1);
}
const blocks = [...fs.readFileSync(mdPath, 'utf8').matchAll(/```mermaid\n([\s\S]*?)```/g)].map(m => m[1]);

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 2 });
  await page.setContent('<html><body style="margin:0;background:#fff"><div id="c"></div></body></html>');
  await page.addScriptTag({ url: 'https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js' });
  await page.evaluate(() => mermaid.initialize({ startOnLoad: false, theme: 'default' }));
  fs.mkdirSync(outDir, { recursive: true });
  for (let i = 0; i < blocks.length; i++) {
    const res = await page.evaluate(async ([code, i]) => {
      try {
        const { svg } = await mermaid.render('g' + i, code);
        document.getElementById('c').innerHTML =
          '<div id="w" style="display:inline-block;padding:24px;background:#fff">' + svg + '</div>';
        return 'ok';
      } catch (e) { return '错误: ' + e.message; }
    }, [blocks[i], i]);
    const file = path.join(outDir, `${prefix}-${String(i + 1).padStart(2, '0')}.png`);
    if (res === 'ok') await page.locator('#w').screenshot({ path: file });
    console.log(file, res);
  }
  await browser.close();
})();
