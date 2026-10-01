<div align="center">

# ✨ astrbot_plugin_weibo_album

**微博 / 小红书图片一键搬进 QQ 群相册**

给一条微博链接（网页版 / 移动端 / 小程序口令）或小红书分享链接（笔记直链 / `xhslink.com` 短链），
或直接引用（回复）QQ 里那条小程序卡片/分享消息，
把整批原图抓下来，通过 NapCat 传进指定的群相册。

[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-informational.svg)]()
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.24-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![NapCat](https://img.shields.io/badge/NapCat-%E2%89%A54.8.101-red.svg)](https://github.com/NapNeko/NapCatQQ)
[![Version](https://img.shields.io/badge/Version-v1.8.0-success.svg)]()

</div>

## 🤝 介绍

插件把上传拆成两个阶段：先整批并发把原图落到 AstrBot 自带的临时目录（`data/temp`，传完即删），
再一起提交到选定的群相册——下载可以并发跑满，相册侧多张并发传、并按 `upload_interval` 控频。
每次抓取的群聊消息固定最多两条（抓取提示 + 完成汇总），不会逐张刷屏。

## ✨ 功能特性

- **引用即传**：回复（引用）群里那条微博/小红书分享卡片或分享文本发指令即可，不用粘链接；
  引用时参数是相册名就一步到位直传
- **一条命令两个平台**：`/传图` 不分微博小红书，发什么链接就走什么平台的解析，
  不用记两套命令；指令全部按功能命名（传图/看图/补传/列相册），无平台词、无拼音缩写
- **live 图转 GIF**：微博 livephoto 会下载它的视频段用 ffmpeg 转成 GIF 传进相册，相册里能看到动图；
  宿主机没有 ffmpeg 时自动回落上传封面静图
- **链接全形态**：网页版 / 移动端 / `t.cn` 短链 / 裸微博 ID / `#小程序://微博/` 口令 /
  头条文章 / 博主时间线与图集容器（按 `max_pages` 翻页）
- **小红书笔记全形态**：`/explore/` 与 `/discovery/item/` 笔记直链、`xhslink.com` 短链、
  App 复制的分享文本、QQ 小程序卡片；无水印大图优先，分享页兜底
- **免登录**：微博自动走访客网关拿 Cookie（`.weibo.cn` / `.weibo.com` 分域管理），
  个别受限微博再填 `weibo_cookie`；小红书游客通道能抓大部分笔记，
  个别笔记再填 `xhs_cookie`
- **原图保真**：微博按 pid 把路径改写回 `/large/` 真原图；小红书去掉 `imageView2` 缩放参数
  请求原尺寸，CDN 不认再回落带参数版本；单张 30MB 上限内流式判停
- **重复防抖**：相册文件名 + 自记上传台账（每相册 30 天 / 2000 条）双通道去重，
  base64 载荷被 NapCat 改名成 randomUUID 也不会重复传
- **跨容器友好**：默认只发 `base64://` 载荷，NapCat 与 AstrBot 分容器部署零条 ENOENT；
  确认同机时可开 `same_host` 用本地路径载荷，保住相册里的 pid 文件名
- **失败闭环**：部分失败时只留失败张在临时目录，`/补传` 直接补传，不用重新抓

## ⌨️ 命令

| 命令 | 说明 |
| --- | --- |
| `/传图 <链接> [\| 相册名]` | 抓取该链接里的全部图片并上传，**微博/小红书自动识别**。链接可省略：**引用分享消息/小程序卡片**发本指令即可；引用时参数不是链接就是相册名，直接一步到位 |
| `/补传 <编号或相册名>` | 把暂存的那批图传进指定相册。不带参数传回这批图上次的目标相册（失败重传）；列表展示期间**编号优先**于同名相册；失败张也靠它重传 |
| `/看图 <链接>` | 只看链接里有哪些图片，不下载不上传，平台自动识别。同样支持引用 |
| `/列相册` | 列出本群相册名和 album_id |

### 使用示例

```text
/传图 https://m.weibo.cn/detail/4990000000000000             # 抓完列出相册让你选
/补传 2                                                      # 传到列表里的第 2 个
/传图 https://m.weibo.cn/detail/4990000000000000 | 微博原图   # 一步到位

/传图 https://www.xiaohongshu.com/explore/0a1b2c3d4e5f60718293a4b5        # 小红书笔记，同一个命令
/传图 https://xhslink.com/a/xyzCDE | 好物分享                 # 短链 + 相册名一步到位
（引用群里那条小红书分享消息）→ /传图 穿搭                     # 直传「穿搭」

（引用群里那条微博小程序卡片）→ /传图 赵今麦                  # 直传「赵今麦」
（引用群里那条微博小程序卡片）→ /传图                         # 先列相册，再 /补传 1
```

> 微博和小红书用同一条 `/传图`：发什么链接就走什么平台的解析，不用记两套命令。
> 指令全部按功能命名（传图 / 看图 / 补传 / 列相册），无平台词、无别名；
> v1.8.0 起旧命令（`/微博相册`、`/小红书相册`、`/原图相册`、`/传相册`、`/绑相册` 等）已全部移除。

上传完成只回一条结果：`上传完成：成功 12/12 张 -> 相册「赵今麦」`；
有失败时是 `上传未完成：成功 X/Y 张` + 失败明细，失败张留 30 分钟，`/补传` 可只补传那几张。

### 相册名的指定方式

1. 一步到位：`/传图 <链接> | 相册名`（微博/小红书链接都行）
2. 引用卡片/分享消息：`/传图 相册名`（参数不是链接就当作相册名）
3. 先抓再选：`/传图 <链接>` 拿编号列表，`/补传 3`
4. 直接补传：`/补传 某个相册名`（忽略空格与大小写）
5. 失败重传：`/补传` 不带参数，自动传回这批图上次的目标相册

批次会记住自己的目标相册，`/补传` 不带参数时就用它——不设全局默认值，
传到哪由每次的指令说了算。
一步到位写法会自动剥掉分享文本里的常见后缀（如"打开微博小程序查看"、"复制打开小红书"），所以
`<链接> <相册名>` 形式现在支持含空格的相册名；其余情况先下载再列相册让你选。
相册可以用名字、`album_id` 或 `<digits>_<串>` 形式的 ID 指定。

## 📦 安装

前置条件：

- NapCat 就是 AstrBot 已经接好的那个 aiocqhttp(OneBot) 适配器，**插件不需要另配地址或 token**。
  群相册接口（`get_qun_album_list` / `upload_image_to_qun_album`）2025-08-25 之后进入 NapCat 主干，
  对 NapCat 的要求是最低 **v4.8.101**（带完整 OpenAPI 声明的最早是 4.12.0）；AstrBot 本体 `>=4.24,<5` 即可
- 群设置里允许成员（机器人）上传相册，并先在群里手动建好目标相册（NapCat 没有创建相册的接口）
- AstrBot 与 NapCat 分开两个容器跑的什么都不用调，`same_host` 保持默认关闭即可

安装方式：

1. **推荐**：AstrBot WebUI → 插件管理 → 上传发布 zip（`astrbot_plugin_weibo_album_v1.8.0.zip`）
2. 从源码目录复制，**目录名要和 `metadata.yaml` 里的 `name` 一致**，且只复制发布内容
   （`main.py`、`weibo_client.py`、`xhs_client.py`、`napcat_album.py`、`metadata.yaml`、
   `_conf_schema.json`、`requirements.txt`、`README.md`），`tests/`、`pyproject.toml`、`.git` 不需要进插件目录：
   ```bash
   cp -r . <AstrBot 根目录>/data/plugins/astrbot_plugin_weibo_album
   ```
3. WebUI → 插件管理 → 重载插件（首次会自动装 `requirements.txt`），然后 `/列相册` 确认相册名

## ⚙️ 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `weibo_cookie` | 空 | 一般不用填，插件会自动走微博访客通道；个别受限微博再填登录 Cookie |
| `xhs_cookie` | 空 | 小红书 Cookie。一般留空即可，游客通道能抓大部分笔记；个别笔记（如需登录可见）再填浏览器里的登录 Cookie |
| `max_images` | `30` | 单次最多上传张数（微博单条上限 18，抓时间线时可调大）。下载是流式落盘不占内存，但 base64 载荷仍会把单张图读进内存，内存紧张可配合 `compress_over_mb` 把大图压小 |
| `compress_over_mb` | `8` | 超过该体积（MB）的图上传前重压成长边 5000、质量 85 的 JPEG：base64 上传会把整张图变成好几份内存拷贝，20MB 级原图一批并发下来小服务器会进 swap 死机，压缩后体积差一个数量级且观感无异。需要 Pillow（requirements 已带，缺了自动跳过照原图传）；动图（GIF/live 转 GIF）不压；填 `0` 关闭 |
| `max_pages` | `3` | 抓博主时间线/图集容器时的翻页数 |
| `upload_concurrency` | `3` | 同时在传相册的张数（全群共享的总闸，多个群同时触发也不会叠加）。NapCat 每张图要在内部串行发几十个 16KB 分片请求，串行传整批非常慢；调大更快，开始报"频繁"就往下调，填 `1` 回到串行 |
| `upload_interval` | `0.5` | 每张传完占着并发槽休息的秒数，整批从头到尾保持节奏；撞频控优先调低并发数，其次调大这项，填 `0` 表示传完立刻接着传 |
| `upload_payload_mb` | `32` | 所有群合计**同时在途的上传载荷总量**上限（MB），超了自动排队（相当于按体积自动降低并发）。base64 载荷会整包驻留插件与 NapCat 两边内存，图片已被 `compress_over_mb` 重压时基本不会触发；未装 Pillow 又传大图时它就是保命闸。填 `0` 关闭 |
| `skip_exists` | `true` | 不重复上传已经传过的图：既比对相册里的文件名，也比对本插件自己的上传记录（每个相册留 30 天 / 2000 条） |
| `live_gif` | `true` | 微博 live 图（livephoto）下载视频段用 ffmpeg 转成 GIF 再上传，相册里能看到动图。需要宿主机装有 ffmpeg（AstrBot 官方 Docker 镜像自带），没装或转换失败时自动回落上传封面静图。混在图里的真视频条目不传（群相册接口仅支持图片），回复里会注明数量 |
| `same_host` | `false` | 声明 NapCat 与 AstrBot 共用文件系统，上传载荷改用本地路径，相册文件名就是微博 pid。**分容器部署别开**：开了 NapCat 读不到那些路径，每张图会在它控制台撞一条 ENOENT 再降级 base64（图照样传得上） |
| `request_timeout` | `25` | 微博请求超时 |
| `proxy` | 空 | 形如 `http://127.0.0.1:7890` |

## 💡 工作原理

- 下载与上传拆成两阶段：整批并发落本地（全群共享的下载总闸限 5 路，流式边下边写），再按 `upload_concurrency` 并发提交上传（同样是全群共享的总闸）。多个群同时触发不会叠加资源占用，ffmpeg 转码与超大图重压另有 CPU 总闸（同时 2 个）。
- 传图的开始/收场尽量不刷屏：在触发指令的消息上贴 QQ 表情回应——恳求(111) 进行中、得意(4) 全部传完、尴尬(100) 整批都已传过、流泪(5) 上传未完成，只有失败才发文字明细。表情 ID 体系与 [astrbot_plugin_emoji_like](https://github.com/Zhalslar/astrbot_plugin_emoji_like) 一致（QQ 小黄脸表情 ID，走 `set_msg_emoji_like`），每个状态备了几个候选按序尝试，全贴不上（协议端太老没有该接口）才回落成原来的文字提示。
- 平台路由按"小红书优先"判定（笔记页/短链特征明显，误判成本为 0），微博链接走微博客户端，
  其余报"没识别到链接"；两套客户端接口对齐（`grab` / `download`），上传侧完全不感知平台。
- 接口的 `large.url` 指向被压过的 `mw2000`，按 pid 把路径 token 改写成 `/large/` 取真原图，
  拿不到才退回接口给的地址；下载带微博 Referer，否则 CDN 返 403。
- 小红书优先读 explore 笔记页（桌面 UA）`window.__INITIAL_STATE__` 里的 `note.noteDetailMap`
  拿无水印大图；拿不到再回落 `discovery/item` 分享页（移动 UA）。图片地址去掉 `imageView2`
  缩放参数请求原尺寸，CDN 不认再回落带参数版本（webp 改 jpg）。笔记 URL 上的 `xsec_token`
  等 query 原样保留——不带 token 接口会报笔记不存在。
- 匿名抓取自动走微博访客网关；`.weibo.cn` 与 `.weibo.com` 的 `SUB` 同名不同值，插件按域分桶
  自管 Cookie，aiohttp 会话用 `DummyCookieJar` 防止默认 jar 把用户 Cookie 悄悄换掉。
  小红书 Cookie 同样按域门控：只有 `xiaohongshu.com` 站域名才携带，CDN 图片请求一律不带。
- 去重不能只靠相册文件名（base64 载荷会被 NapCat 改名成 randomUUID），所以按 `群号+相册ID`
  另记一份"传过哪些 pid"的台账（30 天过期、每相册上限 2000 条）。
- 上传载荷**默认只用 `base64://`**，插件不猜部署拓扑；`same_host` 打开时第一张先探测路径载荷、
  学到的方式写进 KV，整批最多撞一次错误。只有载荷类错误才降级换方式，权限/相册不存在直接报错。

<details>
<summary><strong>引用转发的解析链（点开）</strong></summary>

AstrBot 的 aiocqhttp 适配器收到引用消息时会自己调 `get_msg`，把被引用消息转好的组件放进
`Reply.chain`（小程序卡片是 `Json` 组件、data 已是 dict），插件直接读它，不重复发请求；
chain 为空（适配器回取失败退成裸 Reply）才自己补一次 `get_msg`（message_id 传 int，NapCat 的
schema 强类型）。目标按「小红书 > 微博直链 > 口令 > 短链/中转字段」的优先级对卡片全字段扫描——
QQ 卡片结构没有公开文档，不押注某个固定字段名；`workflow.op.weibo.com` 这类中转链接会跟一次
跳转再落回微博页。回复时 QQ 自动带的 @ 段会把 `@昵称(uin)` 拼进命令参数，插件只在参数里没有可识别的
链接/ID 时才用引用目标（并把它当相册名），两者互不干扰。

</details>

<details>
<summary><strong>抓取侧的实现细节（点开）</strong></summary>

- 移动端 `m.weibo.cn/statuses/show?id=` 的 `id` 接受 bid 也接受数字 mid，**不需要做 base62 换算**
  （网上流传的 `bid = base62(mid)` 是错的）；列表卡片接口只给前 9 张，所以卡片里图数满了、
  或者是长微博/转发微博时才补一次详情请求，其余直接用卡片数据，避免整页 N+1。
- 长微博读 `statuses/extend`，转发微博把原微博的图一起收进来，文章/小程序页面走正文图片扫描兜底。
- 数字 ID 请求会核对返回的微博 ID，接口偶发串号时直接拒绝，不会把别人的图传进相册。
- 下载按 64KB 分块**流式落盘**（先写同目录 `.part`，写完原子改名），内存里只过一个 chunk；
  超过单张上限立刻中止并清掉半截文件。超大原图（默认 >8MB，`compress_over_mb` 可调）会在
  上传前用 Pillow 重压成 JPEG——base64 载荷会把整张图变成好几份内存拷贝，这一步是
  小内存服务器不进 swap 的关键。
- 文件名不能截断：同一位博主的 pid 前若干位是公共前缀，实测一条微博 18 张图前 10 位完全相同，
  截断当唯一键会把整批图写进同一个文件、最后 18 次上传的是同一张。
- Cookie 头按域门控：只有微博系域名（`weibo.cn` / `weibo.com` / `sinaimg.cn` 等）才携带访客或登录
  Cookie，小红书只有 `xiaohongshu.com` 站域名才带；重定向是手动逐跳跟随的，每一跳重新判域，
  跳去外站不带 Cookie。
- 小红书短链（`xhslink.com` / `.cn`）跟跳转落不到笔记页时，还会从落地页 body 里再扫一遍
  笔记链接（有些是 JS 跳转）；explore 页 `noteDetailMap` 的键对不上 note id 时（token 落到了
  别的笔记），取 Map 里唯一的那条兜底。视频笔记的 `imageList` 只是封面，整条按视频跳过。

</details>

<details>
<summary><strong>上传侧的实现细节（点开）</strong></summary>

- 传输只有一条路：复用 AstrBot 与 NapCat 之间已经建好的那条 OneBot 连接（`event.bot.call_action`，
  并按 `self_id` 路由）。消息不是来自 aiocqhttp 平台时直接告知调不到群相册接口。
  注意 `event.bot` 是 aiocqhttp 的 `CQHttp` 实例，动作口在 `bot.call_action` 上，**没有 `.api` 这一层**。
- 权限装饰器用 `filter.PermissionType.ADMIN`，不用 `GROUP_ADMIN`：v4.28.0 及更早的 `PermissionType`
  只有 `ADMIN`/`MEMBER`，写 `GROUP_ADMIN` 会在 **import 阶段**就 `AttributeError`、整个插件加载失败。
- NapCat 的相册 action 全集在 `action/router.ts` 里，唯一上传口 `upload_image_to_qun_album`
  一次只能传一张；慢的真正原因在它内部对每张图**串行**发 16KB 分片 POST（1MB ≈ 61 个请求），
  所以这里按 `upload_concurrency` 把多张一起提交、用 `upload_interval` 控频——休息占着并发槽，
  稳态下也生效。qzone 协议本身支持批量（`FileBatchControl`），只是 NapCat 没暴露成 action。
- 指令第一件事就是 `event.stop_event()`，避免链接解析类插件把同一批图再往群里刷一遍；
  AstrBot 的管道发现事件已停止就不会再跑 RespondStage，所以本插件所有回复走 `event.send()` 直发。
- 同一个群同时只跑一个上传任务，第二个请求会被告知等待。
- 本地文件是**暂存**：整批结束后先写台账再统一 `unlink` 成功的文件（顺序反了，中途崩溃会变成
  "文件没了台账也没记"），空目录 `rmdir`；失败张等 `/补传` 重传，新抓取/过期/进程重启都会清掉。
- NapCat 走 OneBot 连接只会见到 1400/1200/1404 这类码，可操作提示按 message 文本判断，
  不承诺具体 retcode。
- 瞬时故障自己退避重试（默认 3 次尝试，1s/2s 再加抖动）：NapCat 传相册是自己 fetch
  `h5.qzone.qq.com` 串行发 16KB 分片，QQ 相册网关偶发 5xx 时它抛的是
  `HTTP error! status: 502`（OneBot 侧统一包成 retcode 1200）——这跟插件怎么传没关系，
  重试基本就能过，所以不当成这张图的失败。
- 但**拿不到 NapCat 响应时上传不重试**：AstrBot 的反向 WS `api_timeout_sec=180`，等满只代表
  插件这边不等了，NapCat 那边可能还在传分片；上传不幂等，盲重试会让相册里多出两张一样的图。
  这种会把该张判失败，并在失败明细里注明"可能其实已经传上去了"，重传前先看一眼相册。

</details>

## ❓ 已知边界

### 服务器死机后怎么排查

- 独立诊断文件 `data/plugin_data/astrbot_plugin_weibo_album/diagnostic.log`：每批图在**上传开始前**就写入"张数 + 总大小 + 并发/预算配置"并立即刷盘，整机死机重启后仍在——看最后几行就知道死机前在传多大的批，不需要复现
- 插件启动日志会报告 Pillow / ffmpeg 是否可用：**Pillow 缺失时超大图重压不生效**，是最常见的超载根因（`pip install pillow` 后重载插件）
- 磁盘剩余低于 1GB 时本批下载会被拒绝并提示，防止写满磁盘导致整机异常

- 已用真实链接验证：单条微博（含 18 图）、`weibo.com/<uid>/<bid>`、小程序分享文本、博主时间线；
  上传通道对端用假 NapCat 演到真实错误码与文案级别（见测试）。
- 小红书侧按 `astrbot_plugin_parser` 的公开实现与页面结构离线校准（explore / discovery 双通道、
  短链跳转、`imageView2` 去参），离线单测全覆盖；真实笔记链路的验证以你实际使用为准，
  个别笔记抓不到时优先补 `xhs_cookie`。
- 按官方文档实现但还没拿到真实样本验证：头条文章、博主相册容器 `107803`（该容器卡片结构不同，
  目前抓不到图，需要真实链接来校准）。
- **合并转发的消息暂不支持**：请用"引用回复"引用那张卡片，或直接粘链接。
- 小红书**视频笔记只跳过不传**（`imageList` 只是封面，群相册接口只有图片上传），
  预览/完成回复里会注明；微博侧混在图里的真视频条目同样跳过。
- NapCat 没有创建相册的接口，目标相册必须先存在（解析失败时会带上现有相册清单）。
- 传图是"一张一次调用 + 多张并发"，做不到一个请求传多张；QQ 相册的"最近上传"动态对每张图
  各记一条记录，是协议端行为。并发数受 QQ 侧频控约束，调太高会报"分片 N 上传失败/操作频繁"；
  网关侧偶发的 502 也常与并发过高有关，插件会退避重试，仍失败就把 `upload_concurrency` 调低。
- 只对接 NapCat 的接口名。LLOneBot 等协议端用另一套名字（`get_group_album_list` /
  `upload_group_album` + `files=[]`），本插件未适配。
- 只搬图片，不带微博/小红书正文；纯文字微博和纯文字笔记会被跳过。live 图则按 `live_gif`
  转成 GIF 传进相册（小红书暂无 live 图概念）。
- 小程序卡片的解析按 NapCat `get_msg` 回取的消息段做，QQ 卡片的字段结构没有公开文档；
  个别新卡片形态解析不出目标时会有明确提示，直接把链接粘在指令后面即可。
- 容器类抓取依赖 `container/getIndex`，风控比单条微博严，失败时填 `weibo_cookie`。
- 单张原图超过 30MB 会被跳过。
- 指令只处理微博系和小红书系链接：外站链接（包括落在外站的 t.cn / xhslink 短链）会被直接拒绝，
  机器人不会去请求外站，也不会把微博/小红书 Cookie 带出各自域。
- 回复引用时参数若是 8-20 位纯字母数字（恰好是英文相册名的情况），会先被当成微博 ID 去解析；
  这种情况请改用 `<链接> | 相册名` 的写法。

## 🧪 测试

```bash
python tests/mock_napcat_test.py     # NapCat 客户端契约：翻页解析、载荷降级与"学一次就记住"、错误分类
python tests/test_weibo_client.py    # 离线单测（无网）：链接解析、pid 提取、引用卡片解析、引导退避
python tests/test_xhs_client.py      # 离线单测（无网）：小红书链接/短链解析、图集提取、无水印优先与回落
python tests/test_plugin_e2e.py      # 端到端（真实网络）：29 个用例走完指令链路 + 假 NapCat 上传
```

`test_plugin_e2e.py` 按 AstrBot 的真实加载方式导入插件，用一份照抄上游算法的 `CommandFilter`
复刻做指令匹配与参数绑定，伪造的 astrbot 面刻意"只严不松"（替身比真 API 宽松是历史上几个
阻断性 bug 全绿通过的原因）。覆盖：18 图整批链路、小红书笔记整链路（指令路由/上传/去重/跨指令
改道）、去重、两种 `skip_exists` 下的重传闭环、引用卡片/纯文本/异常路径、批次记住目标相册、
TTL 清扫、并发锁、载荷探测学习、出厂默认零 ENOENT、`terminate` 撤任务等。同一次运行内会缓存抓取结果
（首个仍是真实网络），避免 160+ 次请求。

## 🌟 贡献

欢迎提 Issue 与 PR。提交前请跑一遍上面的四个测试脚本和 `ruff check .`
（规则与 AstrBot 上游一致，豁免 E501，中文注释行宽不卡）。

## 🙏 致谢

本项目在实现思路上参考了以下 AstrBot 插件的公开实现（括号内为各自许可证，均与本项目兼容）：

- [Zhalslar/astrbot_plugin_qun_album](https://github.com/Zhalslar/astrbot_plugin_qun_album)（GPL-3.0）
  —— 群相册上传的载荷方式与协议端差异处理
- [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)（MIT）
  —— 微博链接路由表与移动端请求头/缓存戳；小红书笔记解析通道（explore / discovery 双页、
  无水印大图字段与 `xhslink` 短链处理）
- [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)（AGPL-3.0）
  —— 微博访客 Cookie 引导与 ID 一致性校验
- [Chuubururin/astrbot_plugin_group_cloud_storage](https://github.com/Chuubururin/astrbot_plugin_group_cloud_storage)（AGPL-3.0）
  —— 上传后回读校验、文件名即相册展示名
- [Foolllll-J/astrbot_plugin_group_backup](https://github.com/Foolllll-J/astrbot_plugin_group_backup)（AGPL-3.0）
  —— 相册媒体 `attach_info` 翻页与 lloc 去重

规范依据：[AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)、
[NapCat 接口文档](https://napneko.github.io/develop/api)。

## 📄 许可证

本项目以 [GNU AGPL-3.0](LICENSE) 发布。测试中的指令过滤逻辑移植自 AstrBot 本体
（AstrBot 即 AGPL-3.0），灵感来源中列出的参考项目许可也与 AGPL-3.0 兼容。
