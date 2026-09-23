# astrbot_plugin_weibo_album —— 微博原图传 QQ 群相册

给一条微博链接（网页版、手机版、小程序分享文本都行），把里面**全部原图**抓下来，
再通过 NapCat 传进指定 QQ 群相册。

```
/微博相册 https://m.weibo.cn/detail/4990000000000000        # 先整批存到本地，再列出相册让你选
/传相册 2                                                   # 传到列表里的第 2 个相册
/微博相册 https://m.weibo.cn/detail/4990000000000000 | 微博原图   # 或者一步到位
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `/微博相册 <链接>` | 抓取该微博的所有原图，**先整批存到本地**，然后列出本群相册等你选，别名 `/微博传图`、`/wbalbum` |
| `/微博相册 <链接> \| 相册名` | 一步到位：抓完直接传到指定相册，不走选择 |
| `/传相册 <编号或相册名>` | 把刚下载到本地的那批图传进指定相册，别名 `/上传相册`、`/wbpush`；不带参数用本群绑定的默认相册 |
| `/微博图片 <链接>` | 只预览抓到了哪些图，不下载不上传，别名 `/微博预览`、`/wbimg` |
| `/群相册列表` | 列出本群相册名和 album_id，方便确认要填哪个相册 |
| `/绑定相册 <相册名>` | 给本群设默认相册，`/传相册` 不带参数时用它（需群管理员） |
| `/解绑相册` | 清除本群默认相册（需群管理员） |

下载和上传分成两步是有意的：下载可以并发跑完，相册那边 NapCat 只能一张一次调用（见下面"上传侧的做法"），
但**多张可以并发传**，所以 `/微博相册` 先把整批原图落到 `data/astrbot_plugin_weibo_album/albums/<时间戳_微博ID>/`，
再一起提交上传。**本地只是暂存**：每张传成功立刻删掉该文件，整批结束后空目录也删掉，
回复里会写"本地暂存已清理"。只有传失败的那几张会留在原地，方便 `/传相册` 直接重传（下次抓取或重启会清掉）。

选相册那一步还没做决定时，暂存批次会留在本地，所以同一批图可以 `/传相册 1`、`/传相册 2` 依次传进多个相册。

链接可以直接跟在命令后面，也可以把 App 里「复制链接」得到的分享文本（含中文说明）原样粘贴，
插件会自己从文本里挑出微博链接。支持的目标形态：

- `https://weibo.com/<uid>/<bid>`、`https://m.weibo.cn/status/<bid>`、`https://m.weibo.cn/detail/<id>`
- 直接发 8-20 位的微博 ID（`/微博相册 Ab1Cd2Ef3`）
- `https://t.cn/xxxx` 短链、微博小程序/App 分享出去的各种包装链接
- 头条文章 `.../ttarticle/p/show?id=230940...`
- 博主微博时间线 / 图集容器页（`containerid=107603...`、`/p/100160...`），按 `max_pages` 翻页抓取

### 相册名怎么写

相册有三种指定方式：一步到位写 `/微博相册 <链接> | 相册名`；或先 `/微博相册 <链接>` 拿到编号列表，
再 `/传相册 3`；或 `/传相册 某个相册名` 直接写名字（忽略空格与大小写）。绑定的默认相册不再是唯一选择，
它只是 `/传相册` 不带参数时的默认值。

一步到位那种写法里，分享文本本身就带空格，所以只认两种无歧义形式，避免把「打开微博小程序查看」当成相册名：

1. 用 `|` 显式分隔：`/微博相册 <任意链接文本> | 相册名`
2. 整段恰好是 `<链接> <单个短词>`：`/微博相册 https://m.weibo.cn/detail/123 微博原图`

其余情况一律先下载、再把相册列出来让你选。相册可以用名字、`album_id` 或 `<digits>_<串>` 形式的 ID 指定。

## 安装

1. 准备 NapCat：就是 AstrBot 已经接好的那个 aiocqhttp(OneBot) 适配器，**插件不需要另配地址或 token**。
   群相册接口（`get_qun_album_list` / `upload_image_to_qun_album`）是 2025-08-25 之后进入主干的，
   最低 **v4.8.101**，带完整 OpenAPI 声明的最早是 4.12.0；同时确认群设置里允许成员（机器人）上传
   相册，并先在群里手动建好目标相册（NapCat 没有创建相册的接口）。
2. 把本目录复制到 AstrBot 的插件目录，**目录名要和 `metadata.yaml` 里的 `name` 一致**：
   ```bash
   cp -r . <AstrBot 根目录>/data/plugins/astrbot_plugin_weibo_album
   ```
   发布用的 zip 顶层就是这个目录名，WebUI 里直接上传 zip 也一样。
3. AstrBot WebUI → 插件管理 → 重载插件（首次会自动装 `requirements.txt`）。
4. `/群相册列表` 确认相册名。之后每次 `/微博相册 <链接>` 都会把相册列出来让你选，
   `/传相册 <编号>` 即传；想省这一步就 `/绑定相册 <相册名>`（它只是 `/传相册` 不带参数时的默认值），
   或者一步到位写 `/微博相册 <链接> | 相册名`。

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `weibo_cookie` | 空 | 一般不用填，插件会自动走微博访客通道；个别受限微博再填登录 Cookie |
| `default_album` | `微博原图` | `/传相册` 不带参数、且本群没绑定相册时用的目标相册 |
| `max_images` | `30` | 单次最多上传张数（微博单条上限 18，抓时间线时可调大） |
| `max_pages` | `3` | 抓博主时间线/图集容器时的翻页数 |
| `upload_concurrency` | `3` | 同时在传相册的张数。NapCat 每张图要在内部串行发几十个分片请求，串行传整批非常慢；调大更快，开始报"频繁"就往下调，填 `1` 回到串行 |
| `upload_interval` | `0.5` | 每隔这么多秒再放一张进上传队列（错开起点），撞频控时调大到 1~2，填 `0` 一次性全部发起 |
| `skip_exists` | `true` | 不重复上传已经传过的图：既比对相册里的文件名，也比对本插件自己的上传记录（每个相册留 30 天 / 2000 条） |
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

- 传输只有一条路：复用 AstrBot 与 NapCat 之间已经建好的那条 OneBot 连接（`event.bot.call_action`，
  并按 `self_id` 路由到对应的那个适配器）。地址、token 都是 AstrBot 适配器的事，插件不另配一份；
  消息不是来自 aiocqhttp 平台时直接告知"这条消息所在的平台调不到群相册接口"。
  注意 `event.bot` 是 aiocqhttp 的 `CQHttp` 实例，动作口就在 `bot.call_action` 上，**没有 `.api` 这一层**
  （AstrBot 自己的 aiocqhttp 适配器也是这么调的）。
- 权限装饰器用 `filter.PermissionType.ADMIN`，不用 `GROUP_ADMIN`：v4.28.0 及更早的 `PermissionType`
  只有 `ADMIN`/`MEMBER` 两个成员，写 `GROUP_ADMIN` 会在 **import 阶段** 就 `AttributeError`、
  整个插件加载失败。`ADMIN` 的判定本来就是 `event.is_admin()`，语义也正是"群管理员"。
- 上传载荷只有 **本地路径 → `base64://`** 两种候选（`file://` 那种写法 NapCat 的 `checkUriType` 认不出来，
  只会把路径解析成空串、抛 `ENOENT: ... open ''`，所以直接删了）。相册里显示的文件名来自上传文件本身，
  base64 载荷会被 NapCat 用 `randomUUID` 改名，本地路径才保留 `<pid>.jpg`，所以先默认试路径。
  **但跨容器/跨机器时 NapCat 看不见插件写的路径**（例如 AstrBot 在 `/AstrBot`、NapCat 在 `/app/napcat`），
  每张图都会抛一条 ENOENT。插件把这识别成"换下一种载荷"而不是失败，并且**学一次就记住**：
  同批其余图直接走能用的那种，学到的方式还写进 KV 跨重启保留，所以正常最多在第一批的第一张撞一次错误。
  文件名不能截断：同一位博主的 pid 前若干位是公共前缀，实测一条微博 18 张图前 10 位完全相同，
  截断当唯一键会把整批图写进同一个文件、最后 18 次上传的是同一张。
- **一次调用只能一张，批量靠并发**。NapCat 的相册 action 全集就在 `action/router.ts` 里，唯一的上传口
  是 `upload_image_to_qun_album`，载荷写死 `file: Type.String`；底层 `WebApi.uploadImageToQunAlbum(gc, albumId, albumName, path)`
  也只收一个路径。慢的真正原因在它内部：`uploadQunAlbumSlice(..., slice_size=16384)` 对每张图**串行**
  发 16KB 分片 POST，1MB 原图就是 ~61 个请求，18 张串行下来上千次。所以这里按 `upload_concurrency`
  把多张一起提交（每张是分片序列之间互不相干），并用 `upload_interval` 错开起点。
  顺带一说，qzone 协议本身是支持批量的（`data/webapi.ts` 的 `qunAlbumControl()` 返回 `control_req` **数组**，
  端点叫 `FileBatchControl`，还有 `photo_num`/`batch_num` 字段），只是 NapCat 没把它暴露成 action——
  要真正一个请求传多张，得改 NapCat 那边而不是插件这边。
- 本地文件是**暂存**：每张传成功就 `unlink`，整批结束后空目录 `rmdir`（回复里写"本地暂存已清理"）。
  只有失败的留在原地等 `/传相册` 重传，新的抓取、过期、进程重启都会把它们清掉。
- 只有**载荷类**错误才会换格式重试；权限、相册不存在这类业务错误第一次就抛出，不会白试三遍。
- NapCat 的相册接口并不产出 1401 这类语义错误码（走 OneBot 连接只会见到 1400/1200/1404），
  所以可操作提示是按 message 文本判断的，不承诺具体 retcode。
- NapCat 上传成功不返回图片 id，所以插件在上传前后各读一次相册媒体列表：上传前用它做去重跳过，
  上传后用差集回报「相册新增 N 张」，读不到只降级为提示、不影响上传本身。媒体列表的 `has_more`
  在上游类型声明里并不存在，缺字段时改按 `attach_info` 是否还在往前走来判断，不会只翻一页就停。
- 去重不能只靠相册文件名：**base64 载荷被 NapCat 改名成 randomUUID**，跨容器部署下文件名里根本没有
  微博 pid。所以插件另记一份"本相册传过哪些 pid"的台账（按 `群号+相册ID` 存在 KV 里，30 天过期、
  每相册上限 2000 条）。代价是：如果有人在 QQ 里手动清空了这个相册，30 天内重传同一条微博会被台账挡下来，
  提示"之前已经传进"，等记录过期即可，或者换个相册传。
- 指令第一件事就是 `event.stop_event()`，避免链接解析类插件把同一批图再往群里刷一遍。
  AstrBot 的管道一旦发现事件已停止就不会再跑 RespondStage，所以本插件**所有**回复都走
  `event.send()` 直发，而不是 `yield`（`yield` 在 stop 之后会被静默丢掉）。
- 同一个群同时只跑一个上传任务，第二个请求会被告知等待，避免两批图互相插。

## 测试

```bash
python tests/mock_napcat_test.py     # 客户端契约：翻页解析、载荷降级与"学一次就记住"、频控重试、错误分类、不重试不支持的接口
python tests/test_plugin_e2e.py      # 端到端：真实微博抓取 + 假 NapCat 相册，含两阶段流程
```

`test_plugin_e2e.py` 会按 AstrBot 的真实加载方式（`data.plugins.<name>.main` 命名空间包 + 相对导入）
导入插件，并用一份**照抄上游算法**的 `CommandFilter` 复刻来做指令匹配和参数绑定——指令是走完整的
"消息文本 → 解析参数 → 调用 handler"链路进来的，不是直接调函数。伪造的 astrbot 面刻意"只严不松"：
`get_kv_data` 的 `default` 保持必填、`event.send()` 只接受带 `.chain` 的消息链。替身比真 API 宽松，
是历史上几个阻断性 bug 能全绿通过的原因。

覆盖：18 图整批下载并落本地（校验每张文件名唯一、字节数不同，防上面那个命名撞车）、
相册名回查、新增数校验、重复执行去重且提示真的发出去、`skip_exists` 关闭后照常重传、
小程序文本、裸 ID、不写相册名时列相册待选、`/传相册` 走编号/相册名/默认绑定三条路、
传完清掉待上传批次、解绑与权限登记、`max_images` 截断、调用带 `self_id` 路由、
非 NapCat 平台时明确拒绝、真实 ENOENT 降级 base64、跨容器时整批只探测一次载荷且后续批次零 ENOENT、
相册文件名被 NapCat 改成 randomUUID 时按上传记录去重、私聊/相册不存在/空参数提示、
同群并发被锁挡住、预览、本地暂存传完即删（跑完一轮后 albums/ 下必须一个不剩）。

同一次运行内会缓存微博抓取结果（首个仍是真实网络），否则 9 个上传环节会打出 160+ 次请求。

## 已知边界

- 已用真实链接验证：单条微博（含 18 图）、`weibo.com/<uid>/<bid>`、小程序分享文本、博主时间线，
  上传通道则是经 AstrBot 已有的那条 OneBot 连接（对端用假 NapCat 演到真实错误码与文案级别）。
- 按官方文档实现但还没拿到真实样本验证：头条文章、转发微博、博主相册容器 `107803`
  （该容器卡片结构与时间线不同，目前抓不到图，需要真实链接来校准）。
- NapCat 没有创建相册的接口，目标相册必须先存在（解析失败时会带上现有相册清单）。
- 传图是"一张一次调用 + 多张并发"，做不到一个请求传多张（上游没暴露批量 action，见上面"上传侧的做法"）。
  并发数受 QQ 侧频控约束，调太高会开始报"分片 N 上传失败/操作频繁"。
- 只对接 NapCat 的接口名（`get_qun_album_list` / `upload_image_to_qun_album`）。LLOneBot 等协议端
  用的是另一套名字（`get_group_album_list` / `upload_group_album` + `files=[]`），本插件未适配。
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
