# astrbot_plugin_weibo_album —— 微博原图传 QQ 群相册

给一条微博链接（网页版、手机版、小程序分享文本都行），把里面**全部原图**抓下来，
再通过 NapCat 传进指定 QQ 群相册。

```
/微博相册 https://m.weibo.cn/detail/4990000000000000 | 微博原图
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `/微博相册 <链接> [\| 相册名]` | 抓取该微博的所有原图并上传到群相册，别名 `/微博传图`、`/wbalbum` |
| `/微博图片 <链接>` | 只预览抓到了哪些图，不上传，别名 `/微博预览`、`/wbimg` |
| `/群相册列表` | 列出本群相册名和 album_id，方便确认要填哪个相册 |
| `/绑定相册 <相册名>` | 给本群设默认相册，之后 `/微博相册` 不用再带相册名（需群管理员） |
| `/解绑相册` | 清除本群默认相册（需群管理员） |

链接可以直接跟在命令后面，也可以把 App 里「复制链接」得到的分享文本（含中文说明）原样粘贴，
插件会自己从文本里挑出微博链接。支持的目标形态：

- `https://weibo.com/<uid>/<bid>`、`https://m.weibo.cn/status/<bid>`、`https://m.weibo.cn/detail/<id>`
- 直接发 8-20 位的微博 ID（`/微博相册 Ab1Cd2Ef3`）
- `https://t.cn/xxxx` 短链、微博小程序/App 分享出去的各种包装链接
- 头条文章 `.../ttarticle/p/show?id=230940...`
- 博主微博时间线 / 图集容器页（`containerid=107603...`、`/p/100160...`），按 `max_pages` 翻页抓取

### 相册名怎么写

分享文本本身就带空格，所以插件只认两种无歧义写法，避免把「打开微博小程序查看」当成相册名：

1. 用 `|` 显式分隔：`/微博相册 <任意链接文本> | 相册名`
2. 整段恰好是 `<链接> <单个短词>`：`/微博相册 https://m.weibo.cn/detail/123 微博原图`

其余情况一律走 `/绑定相册` 设的本群默认相册，或插件配置里的 `default_album`。
相册可以用名字、`album_id` 或 `<digits>_<串>` 形式的 ID 指定。

## 安装

1. 准备 NapCat：群相册接口（`get_qun_album_list` / `upload_image_to_qun_album`）是 2025-08-25
   之后进入主干的，最低 **v4.8.101**，带完整 OpenAPI 声明的最早是 4.12.0；同时确认群设置里允许
   成员（机器人）上传相册，并先在群里手动建好目标相册（NapCat 没有创建相册的接口）。
2. 把本目录复制到 AstrBot 的插件目录，**目录名要和 `metadata.yaml` 里的 `name` 一致**：
   ```bash
   cp -r . <AstrBot 根目录>/data/plugins/astrbot_plugin_weibo_album
   ```
3. AstrBot WebUI → 插件管理 → 重载插件（首次会自动装 `requirements.txt`）。
4. `/群相册列表` 确认相册名，然后 `/绑定相册 <相册名>`。

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `napcat_http_root` | 空 | **留空即复用 AstrBot 与 NapCat 已有的 OneBot 连接**，零配置可用；跨机部署时填 `http://127.0.0.1:3000` |
| `napcat_token` | 空 | 走 HTTP 直连时填 NapCat 网络服务里的 token |
| `weibo_cookie` | 空 | 一般不用填，插件会自动走微博访客通道；个别受限微博再填登录 Cookie |
| `default_album` | `微博原图` | 未绑定且命令里没写相册名时的目标相册 |
| `max_images` | `30` | 单次最多上传张数（微博单条上限 18，抓时间线时可调大） |
| `max_pages` | `3` | 抓博主时间线/图集容器时的翻页数 |
| `upload_interval` | `1.5` | 两张图之间的间隔秒数，QQ 侧频控时调大到 3 |
| `skip_exists` | `true` | 按图片 pid 比对相册已有文件名，重复执行同一条微博不会传重 |
| `request_timeout` | `25` | 微博请求超时 |
| `proxy` | 空 | 形如 `http://127.0.0.1:7890` |

## 抓取侧的做法

- 移动端 `m.weibo.cn/statuses/show?id=` 的 `id` 接受 bid 也接受数字 mid，**不需要做 base62 换算**
  （网上流传的 `bid = base62(mid)` 是错的）；列表卡片接口只给前 9 张，所以卡片里图数满了、
  或者是长微博/转发微博时才补一次详情请求，其余直接用卡片数据，避免整页 N+1。
- 接口现在的 `large.url` 指向被压过的 `mw2000`，插件按 pid 把路径 token 改写成 `/large/` 取真原图，
  拿不到才退回接口给的地址；下载必须带微博 Referer，否则 CDN 返 403。
- 匿名抓取靠自动走微博访客网关拿 Cookie。`.weibo.cn` 与 `.weibo.com` 的 `SUB` 同名不同值，
  所以插件自己按域分桶管理，并且给 aiohttp 会话用了 `DummyCookieJar` —— 默认 jar 会在同名键上
  反过来覆盖手工设置的 Cookie，把用户填的登录 Cookie 悄悄换成访客 Cookie。
- 长微博读 `statuses/extend`，转发微博把原微博的图一起收进来，文章/小程序页面走正文图片扫描兜底。
- 数字 ID 请求会核对返回的微博 ID，接口偶发串号时直接拒绝，不会把别人的图传进相册。
- 下载按 64KB 分块累计，超过单张上限立刻中止，不会先把十几 MB 读进内存再判断。

## 上传侧的做法

- 传输方式两种，默认复用 AstrBot 已有的 OneBot 连接（`bot.api.call_action`），也可填 NapCat HTTP 地址直连。
- 上传载荷按 **本地路径 → `file://` → `base64://`** 依次尝试：相册里显示的文件名来自上传文件本身
  （NapCat 对 base64 载荷用 `randomUUID` 命名，对本地路径取 `basename`），所以先把图片落成一个
  可读的文件名（`<相册名>_<pid前缀>.jpg`）再传。NapCat 与机器人不同机时路径会抛
  `ENOENT: no such file or directory`，插件把这识别成"换下一种载荷"而不是"接口不存在"，自动降级 base64。
- 只有**载荷类**错误才会换格式重试；权限、相册不存在这类业务错误第一次就抛出，不会白试三遍。
- NapCat 的相册接口并不产出 1400/1401/1404 这类语义错误码（HTTP 通道只给 400/200，WebSocket
  通道给 1400/1200/1404），所以可操作提示是按 message 文本判断的，不承诺具体 retcode。
- NapCat 上传成功不返回图片 id，所以插件在上传前后各读一次相册媒体列表：上传前用它做去重跳过，
  上传后用差集回报「相册新增 N 张」，读不到只降级为提示、不影响上传本身。媒体列表的 `has_more`
  在上游类型声明里并不存在，缺字段时改按 `attach_info` 是否还在往前走来判断，不会只翻一页就停。
- 指令第一件事就是 `event.stop_event()`，避免链接解析类插件把同一批图再往群里刷一遍。
  AstrBot 的管道一旦发现事件已停止就不会再跑 RespondStage，所以本插件**所有**回复都走
  `event.send()` 直发，而不是 `yield`（`yield` 在 stop 之后会被静默丢掉）。
- 同一个群同时只跑一个上传任务，第二个请求会被告知等待，避免两批图互相插。

## 测试

```bash
python tests/mock_napcat_test.py     # 客户端契约：翻页解析、三段载荷降级、频控重试、错误分类、不重试不支持的接口
python tests/test_plugin_e2e.py      # 端到端：真实微博抓取 + mock NapCat 相册，13 组用例
```

`test_plugin_e2e.py` 会按 AstrBot 的真实加载方式（`data.plugins.<name>.main` 命名空间包 + 相对导入）
导入插件，并用一份**照抄上游算法**的 `CommandFilter` 复刻来做指令匹配和参数绑定——指令是走完整的
"消息文本 → 解析参数 → 调用 handler"链路进来的，不是直接调函数。伪造的 astrbot 面刻意"只严不松"：
`get_kv_data` 的 `default` 保持必填、`event.send()` 只接受带 `.chain` 的消息链。替身比真 API 宽松，
是历史上几个阻断性 bug 能全绿通过的原因。

覆盖：18 图全量上传、相册文件名可读、新增数校验、重复执行去重且提示真的发出去、
`skip_exists` 关闭后照常重传、小程序文本、裸 ID、绑定/显式相册优先级、解绑与权限登记、
`max_images` 截断、复用 OneBot 连接、真实 ENOENT 降级 base64、私聊/相册不存在/空参数提示、
同群并发被锁挡住、预览、临时文件清理。

同一次运行内会缓存微博抓取结果（首个仍是真实网络），否则 9 个上传环节会打出 160+ 次请求。

## 已知边界

- 已用真实链接验证：单条微博（含 18 图）、`weibo.com/<uid>/<bid>`、小程序分享文本、博主时间线，
  以及两条上传通道。
- 按官方文档实现但还没拿到真实样本验证：头条文章、转发微博、博主相册容器 `107803`
  （该容器卡片结构与时间线不同，目前抓不到图，需要真实链接来校准）。
- NapCat 没有创建相册的接口，目标相册必须先存在（解析失败时会带上现有相册清单）。
- 只对接 NapCat 的接口名（`get_qun_album_list` / `upload_image_to_qun_album`）。LLOneBot 等协议端
  用的是另一套名字（`get_group_album_list` / `upload_group_album` + `files=[]`），本插件未适配。
- 填了 `napcat_http_root` 走 HTTP 直连时，NapCat 自身不限 JSON body 大小，但反代（nginx 等）可能
  返回 413；跨机部署建议留空该配置，改走 AstrBot 已有连接。
- 只搬图片，不带微博正文；视频、直播、纯文字微博会被跳过。
- 容器类抓取依赖 `container/getIndex`，风控比单条微博严，失败时填 `weibo_cookie`。
- 单张原图超过 30MB 会被跳过。

## 代码规范

按 AstrBot 的开发原则执行：`aiohttp` 异步请求、持久化数据落在 `data/` 下、错误不让插件崩溃、
提交前过 `ruff`。`pyproject.toml` 里的 ruff 规则与 AstrBot 上游一致（同样豁免 E501，中文注释行宽不卡）。

## 灵感来源

本项目在实现思路上参考了以下 AstrBot 插件的公开实现：

- [Zhalslar/astrbot_plugin_qun_album](https://github.com/Zhalslar/astrbot_plugin_qun_album)
  —— 群相册上传的载荷方式与协议端差异处理
- [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
  —— 微博链接路由表与移动端请求头/缓存戳
- [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)
  —— 微博访客 Cookie 引导与 ID 一致性校验
- [Chuubururin/astrbot_plugin_group_cloud_storage](https://github.com/Chuubururin/astrbot_plugin_group_cloud_storage)
  —— 上传后回读校验、文件名即相册展示名
- [Foolllll-J/astrbot_plugin_group_backup](https://github.com/Foolllll-J/astrbot_plugin_group_backup)
  —— 相册媒体 `attach_info` 翻页与 lloc 去重

规范依据：[AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)、
[NapCat 接口文档](https://napneko.github.io/develop/api)。
