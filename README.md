# 3x-ui 免广告自编译版

基于 [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui) 官方源码，在 GitHub Actions 中自动打补丁并编译的免广告版本。安装、升级方式与官方版一致，但运行时不加载官方赞助商广告，且升级不会把广告带回来。

当前发布：**v3.11.0（linux/amd64）**

> 本仓库不保存上游源代码。每次构建都按上游 release tag 拉取官方源码，先校验广告代码基线，通过后再应用补丁并编译。

## 一键安装

```bash
bash <(curl -Ls https://github.com/Darks41/3x-ui-clean/releases/latest/download/install.sh)
```

装完后管理与官方版相同：

```bash
x-ui          # 管理菜单
x-ui status   # 查看状态
```

## 一键升级

面板里点「检查更新」即可，更新源已指向本仓库；也可以用命令：

```bash
x-ui update
```

已经装过官方版、想原地换成免广告版：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Darks41/3x-ui-clean/main/update.sh)
```

> `update.sh` 会从本仓库 release 下载与当前架构匹配的安装包，校验 `.sha256` 后替换二进制、`x-ui.sh` 和 service 文件并重启面板。

## 为什么升级后广告不会复活

补丁在三个层面把更新通道固定在本仓库：

| 位置 | 改动 |
| --- | --- |
| `internal/web/service/panel/panel.go` | 面板「检查更新」的 release API 与 `update.sh` 下载地址改为 `Darks41/3x-ui-clean` |
| `x-ui.sh` | 安装/更新脚本地址、版本检查地址改为本仓库 |
| `install.sh` / `update.sh` | 所有 `raw.githubusercontent.com`、`api.github.com`、release 包地址改为本仓库 |

因此：

1. 面板检查更新时，拿到的是本仓库的 release（已是打过补丁的源码编译出来的二进制）；
2. `x-ui update` 下载的也是本仓库的 `update.sh` 和安装包；
3. 每次发布前，Action 会重新校验广告代码基线，基线不符就跳过编译，不会把未打补丁的官方包发出来。

运行时不会再请求 `MHSanaei/sponsors`，也不存在从官方源“顺手升级回官方版”的路径（除非你手动执行官方安装脚本）。

## 广告是怎么去掉的

官方广告不是写死在页面里的，而是服务端从 `MHSanaei/sponsors` 仓库拉取 JSON 后渲染的赞助商卡片。本仓库的补丁：

- `internal/web/service/panel/sponsor.go`：替换为桩实现，`GetSponsors` 返回空列表，`GetSponsorLogo` 返回未知，不再发起任何外部请求；
- 登录页、仪表盘的 `SponsorSlot` 挂载移除（v3.11.0 起入口集中在 `panelRoutes.tsx`，补丁已同步适配）；
- 侧边栏 `赞助商` 菜单、`/sponsors` 路由、命令面板入口移除；
- 保留 `GET /sponsors`、`GET /sponsors/logo/:name` 两个路由及 OpenAPI 类型，避免破坏 API 契约与生成代码；它们现在只返回空数据/404。

## 上游变更保护

`.github/workflows/build.yml` 的 guard 任务会在编译前检查：

- 6 个广告相关文件的 sha256 是否仍与 `baseline.json` 一致；
- 关键锚点（挂载点、路由、菜单、更新地址等）是否还在；
- 上游是否已经自己删掉了广告代码。

结果：

- **一致** → 应用补丁并编译；
- **有改动 / 官方已移除** → 跳过编译，在 Action 日志和 Step Summary 里写明原因，不发布任何产物。

人工复核后更新 `baseline.json` 与 `scripts/patch.py`，即可恢复自动构建。

## 自动构建

- 每周五 00:00 UTC 自动检查上游最新 release；
- 手动触发：Actions → `build-adfree` → Run workflow（可勾选 `force` 强制重建已存在的版本）；
- 修改 `scripts/`、`baseline.json` 或 workflow 后自动触发。

构建流程（仅 amd64）：拉取上游 tag → guard 校验 → 打补丁 → 构建前端 → Bootlin musl 静态编译 Go 二进制 → 按官方目录结构打包 → 上传 release（含 `.sha256`、`install.sh`、`update.sh`、`x-ui.sh`）。

Xray 版本不再写死：构建时从上游 `.github/workflows/release.yml` 解析并下载对应版本；`baseline.json` 的 `pinned_tag` 由发布任务自动更新，不需要手动跟随。

目前只发布 `linux/amd64`。

## 目录

```
.github/workflows/build.yml   # guard + 编译 + 发布
scripts/patch.py              # 校验、打补丁、生成 fork 版脚本
baseline.json                 # 广告文件哈希与锚点
```

## 常见问题

**为什么 release 必须是 latest？**
`install.sh` 和面板更新都通过 `/releases/latest` 解析版本；workflow 已固定 `--latest`，不要手动改成 prerelease。

**版本号能带后缀吗？**
不能。面板的版本比较只接受 `MAJOR.MINOR.PATCH`，带 `-custom` 之类后缀会导致更新检查失效。release tag 与 `internal/config/version` 保持一致。

**支持其他架构吗？**
当前 workflow 只编译 amd64。需要其他架构时，把 `.github/workflows/build.yml` 的 matrix 加回去即可。

**包含 Docker 镜像吗？**
不包含，只发布二进制安装包。

**和官方版功能有区别吗？**
除广告相关代码外不做改动；Xray、订阅、节点管理等功能与同版本官方版一致。

## 许可

上游为 GPL-3.0，本仓库的补丁脚本与构建配置同样遵循 GPL-3.0，见 [LICENSE](LICENSE)。
