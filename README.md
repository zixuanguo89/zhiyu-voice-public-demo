# 智语外呼预约服务工作台

这是《开发项目书》选定的服务预约改期实现。默认使用合成预约和文本模式，可以在本机体验查询、改期、取消、转人工、结果回写、质检和候选策略评测。

## 启动

安装 Python 3.13，然后双击 `start.cmd`。首次启动会创建本地运行环境并安装锁定依赖。保持启动窗口开启，访问 [预约服务工作台](http://127.0.0.1:8765/)。

也可在项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

服务状态见 [健康检查](http://127.0.0.1:8765/health)。默认仅绑定本机。业务与会话数据分别保存到 `artifacts/business.sqlite3` 和 `artifacts/app.sqlite3`。

## 体验项目

1. 在 **打电话** 点击 **呼出电话**。按提示报虚构全名和 11 位演示手机号，再回答「是我本人」。可在本机勾选「提交成功后丢失回执」演示对账。
2. 可以先问「到店要带什么材料」，再说「我选第一个可用时段」。听到复述后改口「等一下，改成第二个时段」，再说「确认预约」。
3. 在 **通话记录** 打开这通电话。通话中会自动显示新转写，完整手机号会隐藏。启用丢失回执时，结果先显示待核实，随后通过原操作编号查回回执。
4. 在 **预约后台** 查看预约编号、客户全名和手机号尾号、网点、时段库存、业务操作单和 AI 办理轨迹。换一组姓名和号码再预约一次，就能看到两位客户的独立记录。
5. 如需演示策略候选，展开预约后台的 **策略与评测**。默认管理员口令存于 `artifacts/.admin-token`，重启时更新，也可在 `.env` 设置 `ADMIN_TOKEN`。

同一浏览器工作台内，用相同全名和手机号再次来电可查回原预约，并办理改期或取消。本机记录保存在 SQLite，重启后可继续使用。海棠银行科创支行是虚构演示网点，口头确认用于演示。请使用虚构资料。

## 配置云语音

1. 在阿里云百炼 **华北2（北京）** 创建或复制 API Key，确保该业务空间有权调用 Paraformer、CosyVoice 和 Qwen。 [百炼密钥管理](https://help.aliyun.com/zh/model-studio/get-api-key/)
2. 如果项目正在运行，先在启动窗口按 Ctrl+C。双击 `configure-cloud.cmd`，在本机窗口粘贴 API Key。有业务空间 ID 时填写，可使用专属域名。留空使用百炼兼容域名。 [北京地域接入域名](https://help.aliyun.com/zh/model-studio/beijing-access-information)
3. 脚本会用一条合成测试语句检查 TTS、ASR 和 LLM。三项通过后保存到本机 `.env` 并启动项目。打开工作台，点击 **呼出电话** 并允许麦克风。显示「已接通」后按右侧 Visa 借记卡预约脚本交谈。通话中可切到 **通话记录** 查看实时转写。

联通结果保存在 `artifacts/cloud-preflight.json`。该检查使用合成音频，其时长属于服务联通诊断。浏览器通话时延和真实插话需要另行测量。检查调用计入账户额度。语音验收只使用合成或授权脱敏输入。单次媒体连接默认最长五分钟，可在 `.env` 设置 `VOICE_MAX_SECONDS`。

已有配置可执行 `.\.venv\Scripts\python.exe -m services.cloud_setup --check-only` 重新检测。也可参考 `.env.example` 手动配置。密钥保存在本机，`.env` 已加入忽略清单。

浏览器使用 WebRTC 传送麦克风音频，后端桥接百炼流式 ASR 和 TTS。浏览器通过 WebRTC 数据通道接收带轮次的合成音频，用于打断时清空播放队列。当前环境已用真实云服务和 Chromium 合成麦克风输入跑通四轮改期。真人插话、抗噪和赛方时延口径仍需实测。

## 公网演示

项目可用根目录的 `Dockerfile` 和 `railway.toml` 部署到支持 HTTPS 和 WebSocket 的云托管。Railway 服务的公网端口使用 `8765`，持久卷挂载到 `/data`。公网模式为每个浏览器提供独立的模拟预约和通话记录，并限制通话时长与调用次数。

`render.yaml` 使用 Render 免费实例，导入 Blueprint 时在 Render 中配置 `DASHSCOPE_API_KEY`。云语音仍按语音服务商的额度计费。免费实例休眠或重启后，SQLite 演示数据会清空。

在托管平台的服务变量中配置语音提供商、模型和密钥。语音密钥由服务端读取。上传源码时只使用部署包中的文件，本地配置和既有记录留在本机。上线后先访问 `/health` 确认 `public_demo` 和 `microphone` 都为 `true`，再用实际公网地址开启麦克风做一次端到端验收。

### Oracle 云主机部署

在云主机安装 Docker 和 Compose，放行公网 TCP 80 和 443。将 `ZHIYU_PUBLIC_HOST`（公网 IP 或域名）和 `DASHSCOPE_API_KEY` 写入服务器上的私有环境文件，再从项目根目录运行以下命令。

```bash
docker compose --env-file /path/to/private.env -f compose.oracle.yaml up -d --build
```

浏览器通过 HTTPS 打开公网地址，代理自动申请并续期证书。预约与通话数据库保存在 `appointments` 数据卷中，重新构建容器和重启主机后仍可读取。备份时应一起保存该数据卷内的两份 SQLite 数据库。通过 `/health` 确认 `persistent_storage`、`public_demo` 和 `microphone` 均为 `true`。

## 运行验收

```powershell
.\.venv\Scripts\python.exe -m evals.run --suite regression --dataset R --policy baseline --seed 42
```

输出目录包含 HTML 报告、运行清单、逐项结果和脱敏业务证据。支持 `unit`、`contract`、`integration`、`speech`、`regression`、`rsi`、`acceptance`。自动检查失败时退出码为 1。真实语音或最终验收证据未齐时，`speech` 和 `acceptance` 也会返回 1。

## 交付说明

- [实现与验收](docs/交付与验收.md)
- [赛事适配清单](docs/赛事适配清单.md)
- [组件与模型](docs/组件与模型.md)
- [原始开发项目书](开发项目书.md)与[测试规范](测试与验收规范.md)
