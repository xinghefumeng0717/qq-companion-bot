# 角色皮肤：每个角色一套自己的看板和小窝

看板（服务器上的网页）和小窝（Windows 桌面上的控制台窗口）都可以由角色卡自带一整套页面。
服务端只出数据，样子全由卡里的页面决定；换角色就换一套样子，代码不用动。

## 怎么生效

- 角色卡目录里有 `theme/pages/`，并且 `config.toml` 的 `[admin]` 没写 `theme`：自动用这套页面。
- 也可以显式写 `theme = "skin"`。写成别的主题（journal / plain / endfield）就不用卡里的页面。
- 卡里缺哪一页，就用 `companion/skins/default/` 里的同名页兜底。默认皮肤朴素、好读，什么角色都能用。

## 目录长什么样

```
characters/<卡>/theme/pages/
  home.html      此刻（看板首页，路径 /）
  memory.html    记忆（/memory）
  debug.html     调试（/debug）
  costs.html     花销（/costs）
  stickers.html  表情包（/stickers）
  logs.html      日志（/logs）
  admin.html     管理（/admin）
  launcher.html  桌面小窝（由 launcher/home.py 在本机提供）
  其他 css / js / 字体 / 图片……
```

页面里引用自己的文件一律写 `/theme/文件名`，比如 `/theme/qz.css`。
`/theme/` 下的文件不需要 token 就能读：它们只是样子，和仓库里的文件一样。**不要在 `theme/pages/` 里放任何隐私。**
支持的文件类型：html、css、js、json、svg、png、jpg、gif、webp、ico、woff2、txt。

服务器在国内时连不上 Google Fonts，字体要放进卡里（woff2，最好裁成子集）。
霞鹜文楷已经在服务端提供：`/assets/lxgw-wenkai-gb2312.woff2`。

## 数据接口

页面用同源 `fetch` 取数。最省事的办法是引 `/theme/skin.js`（默认皮肤自带，卡里也可以换掉）：

```html
<script src="/theme/skin.js"></script>
<script>
  Skin.api("now").then(function (d) { /* 画页面 */ });
</script>
```

`Skin.api` 会自动带上地址栏里的 `?token=`；页面内链接写 `<a data-page="/memory">` 会被自动补上 token。

| 接口 | 内容 |
|---|---|
| `/api/v1/now` | 她在做什么、心情（标签、描述、效价 v、唤醒 a）、关系阶段（第几首、全部阶段、进度）、她最近一次被告知的「此刻的状态」、他多久没说话、今天的作息（逐段）、约定、记得问他的事、憋着没说的话、她自己的事（生活主线）、QQ 是否在线、主模型最近一次好不好、花销 |
| `/api/v1/memory` | 关于他、她自己说过的（常驻/近期、改口前的说法）、日记（带情绪和墨迹浓淡）、见面、她怎么看他、憋着没说 |
| `/api/v1/debug?turn=<id>` | 最近两天的对话轮次列表；选中那一轮：读到了哪些提示词小节（各多少字）、写下的原稿、实际发出去的（只认真实回执）、校验结果；观察者打分 |
| `/api/v1/costs` | 今日、本月每天、累计、缓存命中、按用途、最近调用 |
| `/api/v1/stickers` | 全部表情包（名字、画面、意思、图片地址）、最近发过的 |
| `/api/v1/logs?level=info|warn|error` | 最近日志，拆好时间、级别、模块、正文，最新在上 |
| `/api/v1/admin` | 最近几份备份、已运行多久 |

写操作沿用原来的 `POST /admin/backup`、`/admin/restart`、`/admin/reset`（重置要带 `{"confirm": "YES"}`）。
用 `Skin.post(path, body)` 会自动带上 `X-Admin-Token` 并要求 JSON 回包。

## 桌面小窝

卡里有 `theme/pages/launcher.html` 时，小窝窗口就用这一页；没有就用原来的 `launcher/home.html`。

- 页面里写 `var INFO = __HOME_INFO__;`，启动时会换成 `{name, secret, ...}`。
- `GET /api/state` 每几秒轮询：隧道、看板、QQ 是否连上，`home` 里是 `/api/home` 的内容（含心情数值、关系第几首、主模型状态）。
- `POST /api/tunnel | /api/backup | /api/sim | /api/folder | /api/open {path}`，请求头带 `X-Home-Secret: INFO.secret`。
- 窗口大小写在页面里：`<meta name="launcher-size" content="456,800">`。

## 现有的皮肤

- **默认皮肤**（`companion/skins/default/`）：朴素、好读，任何角色都能用；角色卡的 `theme/pages/` 缺哪一页，它就替哪一页。
- 公开仓库自带的示例卡 `characters/example` 还没有 `theme/pages/`；给自己的卡画页面时，按上面「目录长什么样」放文件即可生效。
