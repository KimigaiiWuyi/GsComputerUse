# GsComputerUse

给 GsCore 的 Agent 提供电脑操作。工具不绑定具体后端。在插件配置里选 InstaBox 或 CUA Driver，然后点「检查」。检查不会下载虚拟机，也不会安装程序。

命令 `电脑状态` 显示本机核数、内存、同时桌面数、屏幕和截图上限。客人也可以看状态。操作电脑默认只有主人能用，可在配置里关掉「仅主人可用」。

## InstaBox

InstaBox 是云端 Linux amd64 桌面。本机只发 HTTPS 请求，所以 2 核 2 GiB 的机器可以跑这个插件。

InstaBox 没有 2 GiB 的规格。插件先申请 2 核 4 GiB。免费账号只有 4 核 8 GiB。账号拒绝这个规格时，插件改用该账号的默认规格，并在检查结果里写明。

API Key 以 `ibk_` 开头，只填写在插件配置「InstaBox API Key」里。空闲后插件暂停云端机器，不删除。

## CUA Driver

CUA Driver 操作这台机器已经登录的桌面，不另开虚拟机。同一时间只有一个用户。插件不会替你安装。

- macOS 14 及以上：自行安装后执行 `cua-driver permissions grant`，并打开辅助功能和屏幕录制。
- Windows 10 / 11：在已登录的桌面会话里启动。可用 `cua-driver autostart kick`，或在该会话里执行 `cua-driver serve`。
- Linux：在图形会话里执行 `cua-driver serve`。没有图形会话时它看不到桌面。Debian / Ubuntu 最小安装还需要 `libxi6` 和 `at-spi2-core`。原生 Wayland 的控件信息有限。

## 本机限额

2 核，或内存不超过 2560 MiB 时：同时 1 个桌面，屏幕 1024×600，超过 512 KiB 的截图会删掉。

高于这个规格时：InstaBox 最多同时 3 个桌面，CUA Driver 仍是 1 个，屏幕 1280×800，截图上限 1536 KiB。

每个 `bot_id + user_id` 单独一台桌面，不按群拆开。群聊里看起来像密码或密钥的文字不会发到桌面，请改走私聊。

以后加功能，在内层包里新建 `gscomputeruse_<功能>/`，目录中放 `__init__.py`。核心启动时会导入这个目录。

- `gscomputeruse_config`：配置、本机规格和限额
- `gscomputeruse_desktop`：租约、会话，以及 InstaBox / CUA Driver
- `gscomputeruse_agent`：代理工具、状态命令和启动钩子
- `gscomputeruse_probe`：检查按钮
