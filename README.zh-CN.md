# openaxpp300s

[English](README.md) | 简体中文

一个开源的 Python 包、命令行工具和 MCP 服务器,用于 **AX PP300S 升降压数控电源**(0 到 30 V,0 到 10.1 A,300 W),
通过电源的 USB 串口控制。它的设计目标是让 AI 智能体(Claude Code 或任何 MCP 客户端)能自己设置电压和电流、开关输出、
回读端子上的实际输出、连续记录数小时、运行定时序列,而不需要厂商的上位机软件。每条命令每行输出一个 JSON 对象,
每个错误都有稳定的错误码、说明和退出码,大多数错误还带有提示。

协议来自厂商文档、对电源固件(V1.4.4)的反汇编分析,以及在真实电源上的测量,详见 `docs/PROTOCOL.md`。

本项目与电源厂商 **AxinDIY 没有任何关联**。"AX PP300S" 是其产品名称。

## 状态与已测试的硬件

版本 0.1.0(beta)。2026-11-07 在真实电源上分两轮验证:AX PP300S,固件 V1.4.4,Windows 11,CH340 USB 串口芯片。
第二轮中电源由 Type-C 电源输入口上的 65 W USB-PD 充电器供电(约 19.8 V),以 Fluke 289 和电子负载作为参考。

已在硬件上验证:

- 端口发现、`status`、`selftest`;
- `set` 设置 VSET、ISET、OVP、OCP、DELAY、REV、OPP、OTP(电源对每一项都返回 ACK);
- `on` 与 `off`,包括带负载(CV 和 CC);
- 空载下用 Fluke 289 对比 0.5 V 到 30 V 的 VSET 精度,包括由 20 V 输入升压到 20、24、30 V(见[精度](#精度));
- 用 Fluke 289 对比约 3 V 下 0.1 A 到 1 A 的 CC 限流(ISET),以及带载 CV 状态下的电流(0.5 A)(见[精度](#精度));
- 输出开启时同时降低 VSET 和 OVP(空载),以及负载上流过 1 A 时同时降低 ISET 和 OCP(两种情况都未触发保护;
  先发送设定值,等输出降下来后再发送保护值);
- DELAY 和 REV 的行为、`watch`、`sequence`、`raw` 查询,以及拒绝逻辑(OVP 余量、未文档化或超出范围的 `raw` 帧);
- **MCP 服务器对真实电源的使用**:ports、status、selftest、set(包括被拒绝的 set)、output、watch、
  log_start / job_status / job_stop、sequence_start;
- 中文界面及其参数列表:写入和选择列表、`on` 到所选列表的电压、输出开启时更改列表、OVP 过低的列表被拒绝选择、截止电流和
  截止时间,以及数字界面的寄存器(VSET、DELAY)在中文界面上不起作用;
- 显示颜色(`color set`、`color reset`);
- CV/CC 优先(`--priority`);
- 链路时序(见[时序](#时序))。

第二轮中没有触发任何保护。所用负载都很小:最大约 1.2 A 和 15 W。

**尚未在硬件上验证:**

- OCP、OPP、OTP 触发(只在第一天见过 OVP 触发,共两次);
- 机上设置菜单及其影响;
- 约 1.2 A 以上的电流和约 15 W 以上的功率;
- DC5525 电源输入口(第二轮用的是 Type-C 电源输入口);
- 其他输出电压下的 CC 精度(下面的 CC 表是在约 3 V 输出下测得的);
- EEPROM 写入本身(见[状态查询真的会写 EEPROM 吗?](#状态查询真的会写-eeprom-吗))。

Linux 和 macOS:串口层没有 Windows 专用路径,但只在 Windows 上试过。
测试套件只针对模拟器运行(见[模拟器](#模拟器)),没有任何测试会打开真实端口。

## 安装

要求:Python 3.10 或更高版本、电源、一根 USB 线。唯一的依赖是 `pyserial`。

从 PyPI 安装:

```
pip install openaxpp300s
```

或从源码目录安装(用于开发):`pip install -e .`(运行测试请用 `".[test]"`)。无论哪种方式,都会安装 `openaxpp300s` 和
`openaxpp300s-mcp` 两个命令;`python -m openaxpp300s ...` 与 `openaxpp300s ...` 等价。发布:推送标签 `vX.Y.Z` 会运行
`.github/workflows/release.yml`(测试、构建、GitHub release,并通过 trusted publishing 发布到 PyPI)。

## 连接电源

请使用电源的**串口 Type-C 接口**(数据口),而不是电源输入口。电源会以 CH340 USB 串口设备(VID:PID 1A86:7523)出现,
在 Windows 上得到一个 COM 口编号(COMx)。如果 Windows 没有自动安装,请安装 WCH 的 CH340 驱动。

- 电源必须已开机并显示正常界面(不是白屏的 bootloader 状态)。
- 先关闭厂商上位机、串口终端和 FlyMcu:同一时间只有一个程序能占用该端口。
- 打开端口不会复位电源,也不会改变其输出。

端口发现:

```
openaxpp300s ports --probe
```

它列出串口,并向每个 USB 口发送一次状态查询(跳过蓝牙串口);有应答的端口会被记住,供后续命令使用。不带 `--port` 时,端口依次取自
`$OPENAXPP300S_PORT`、上次有应答的端口、第一个像 PP300S 一样应答的 USB 串口。要明确指定:`--port COMx`(例如
`--port COM14`)。

## 快速开始

```
openaxpp300s ports --probe
openaxpp300s selftest
openaxpp300s limits --max-voltage 12 --max-current 3 --note "test board"
openaxpp300s set -V 5 -I 1 --ovp 6 --ocp 2
openaxpp300s on
openaxpp300s status
openaxpp300s off
```

请先设置台面限值(见[台面限值](#台面限值))。任何命令加上 `--sim` 就可以在没有硬件的情况下对虚拟电源试用。

`set` 的一次真实 `--sim` 运行:

```
$ openaxpp300s --sim set -V 5 -I 1 --ovp 6 --ocp 2
{"ok":true,"cmd":"set","port":"SIM","sent":[
 {"key":"ovp","name":"OVP","value":6.0,"unit":"V","raw":600,"id":"0x02","ack":"01","latency_ms":1.9},
 {"key":"ocp","name":"OCP","value":2.0,"unit":"A","raw":200,"id":"0x03","ack":"01","latency_ms":5.0},
 {"key":"voltage","name":"VSET","value":5.0,"unit":"V","raw":500,"id":"0x00","ack":"01","latency_ms":5.2},
 {"key":"current","name":"ISET","value":1.0,"unit":"A","raw":100,"id":"0x01","ack":"01","latency_ms":5.9}],
 "output_on_before":false,"status":{...}}
```

(这里为了阅读加了换行,工具实际输出为一行。)保持在输出之上的保护先发送,然后是设定值。`on` 的输出:

```
$ openaxpp300s --sim on
{"ok":true,"cmd":"on","port":"SIM","sent":[{"key":"output","name":"ON/OFF","value":true,"unit":"","raw":1,
 "id":"0x08","ack":"01","latency_ms":2.2}],"settled":true,"on_after_s":1.12,"status":{"vout_v":5.0,"iout_a":0.0,
 "pout_w":0.0,"vin_v":19.8,"temp_c":28.5,"output_on":true,"mode":"CV","cc_flag":0,"output_flag":1,...}}
```

`--text` 输出简短的可读形式,而不是 JSON:

```
$ openaxpp300s --sim --text status
SIM  output OFF  OFF
out  0.000 V  0.000 A  0.00 W
in   19.80 V   temp 28.5 C   (reply 47.3 ms)
```

## 命令行参考

全局选项,可放在子命令之前或之后:

| 选项 | 含义 |
|---|---|
| `-p`, `--port PORT` | 串口(默认:`$OPENAXPP300S_PORT`,上次有应答的端口,否则第一个像 PP300S 一样应答的 USB 口) |
| `--sim` | 使用虚拟电源(也可用 `OPENAXPP300S_SIM=1`) |
| `--text` | 输出可读文本而不是 JSON |
| `--verbose` | 在 stderr 上打印收发的每一帧 |
| `--lock-wait S` | 等待其他命令释放端口的秒数(默认 5) |
| `--timeout S` | 等待每个应答的秒数(默认 0.3) |
| `--retries N` | 无应答时重发帧的次数(默认 2) |
| `--version` | 打印版本(只能放在子命令之前) |

命令:

| 命令 | 作用 |
|---|---|
| `ports [--probe] [--all]` | 列出串口;`--probe` 向每个 USB 口查询 PP300S 状态;`--all` 还会探测非 USB 和蓝牙端口(较慢) |
| `status` | 实测输出(V、A、W)、输入电压、温度、CV/CC、输出状态,以及本工具上次发送的值和台面限值;输出已开启但不是本工具打开的,会给出一条提示(见下) |
| `set` | 按安全顺序发送设置(选项见下) |
| `on [--no-wait] [--wait-timeout S]` | 打开输出(遵守 DELAY 和 REV),并等待电压建立(见[开机、稳定与中文界面](#开机稳定与中文界面));若上次发送的 DELAY 或 REV 超过 20 s,则不等待直接返回(除非给出 `--wait-timeout`) |
| `off [--no-wait] [--wait-timeout S]` | 关闭输出,并等待电压降下来 |
| `list select N` / `list set N ...` | 中文界面的五个参数列表 0..4(见下) |
| `color set ELEMENT=COLOUR ...` / `color reset` / `color list` | 数字界面的显示颜色(固件 1.4.4 及以上;保存在电源的 EEPROM 中) |
| `watch [--every S] [-n COUNT] [--for DUR] [--quiet] [--reconnect-for DUR]` | 每隔 `--every` 秒(默认 1;未给出 `-n` 或 `--for` 时采样 10 次)输出一行状态(JSONL),最后给出汇总 |
| `log [-o FILE] [--format csv\|jsonl] [--every S] [--duration DUR] [--status-every DUR] [--reconnect-for DUR]` | 把状态连同 Ah 和 Wh 记录到 CSV 或 JSONL,直到 `--duration` 或 `stop` |
| `sequence [FILE] [--steps "5,2,10; 9,1,20"] [--repeat N] [--end off\|keep] [--every S] [--log FILE] [--format csv\|jsonl] [--dry-run]` | 定时电压/电流步骤(CSV 格式同厂商模板:`voltage,current,seconds`);`--repeat 0` 一直运行到 `stop` |
| `stop [--pid PID] [--only watch\|log\|sequence]` | 干净地停止正在运行的 `watch`、`log`、`sequence` |
| `running` | 列出正在运行的这些命令 |
| `limits [--max-voltage V] [--max-current A] [--note TEXT] [--clear]` | 显示或修改台面限值 |
| `registers` | 寄存器表:编号、范围、所属界面 |
| `raw ID DATA [--unsafe]` | 发送一帧(十进制或 `0x..`);ID `0xFF` 是状态查询。不带 `--unsafe` 时,已文档化的寄存器走 `set`、`on`、`list`、`color` 的检查,未文档化的寄存器被拒绝;`--unsafe` 不做任何检查直接发送(不查范围,也不查台面限值) |
| `selftest` | 对链路和电源做只读检查(状态应答、延迟、数值合理性、校验和错误的帧被忽略);从不修改任何设置;有检查失败时退出码为 1 |

`set` 的选项:

| 选项 | 范围与步进 |
|---|---|
| `-V`, `--voltage` | VSET,0 到 30.00 V,步进 10 mV |
| `-I`, `--current` | ISET,0.01 到 10.10 A,步进 10 mA |
| `--ovp` | 过压保护,0.5 到 30.50 V |
| `--ocp` | 过流保护,0.1 到 10.50 A |
| `--delay` | DELAY,0 到 9999 s:`on` 之后输出延迟这么久才打开(0 = 关闭) |
| `--rev` | REV,0 到 9999 s:`on`(及 DELAY)之后,输出在 REV 秒后打开,然后每隔 REV 秒翻转一次(0 = 关闭) |
| `--opp` | 过功率保护,10 到 310 W,步进 10 W |
| `--otp` | 过温保护,60 到 100 C,步进 10 C |
| `--priority cv\|cc` | CV 优先或 CC 优先(固件 1.4.2 及以上;出厂默认为 CC 优先;见[CV 优先还是 CC 优先](#cv-优先还是-cc-优先)) |
| `--no-wait` | 不等待输出跟随新的 VSET |

输出开启时,`set` 会等待输出跟随新的 VSET(最多 3 s,否则报 `NOT_APPLIED`)。新的 OVP 或 OCP 如果不高于端子当前读数
(再加一点余量),在输出开启时会被拒绝,除非同一次调用把 VSET(或 ISET)降到它之下:这时工具先发送设定值,等输出降下来,
然后才发送保护值。输出关闭时,它先等端子放电。

`list select N` 把**中文界面**的列表 N 设为当前列表;`list set N` 写入列表 N:`--voltage`、`--current`、`--ovp`、
`--ocp`、`--cut-current A`(电流低于它持续约 6 s 后输出关闭,这是固件的行为,实测为 5.5 到 6 s,说明书写的是 5 s;0 = 关闭)、
`--cut-time`(累计开启时间达到它时输出关闭:`1h30m`、`45m`、`50s`、`H:M:S`,或 `off` = 99:60:60;0:0:0 会被拒绝)
和 `--select`(同时设为当前列表,在各值之后发送)。VSET、ISET、OVP、OCP、DELAY 和 REV 只在数字和波形界面有效,
列表寄存器只在中文界面有效;OPP、OTP 和 CV/CC 优先在所有界面都有效。电源在任何界面上都会对每一帧返回 ACK 并保存,
所以发给另一种界面的寄存器只是不起作用。这一点已在硬件上验证:在中文界面上,数字界面的 VSET(以及 DELAY)被忽略,
输出开启时 `set -V` 在 3 s 后以 `NOT_APPLIED` 结束。

中文界面的说明(除注明外均为硬件实测):

- 通过串口写入的列表值只存在于 RAM 中,断电即丢失,除非它属于所选列表且前面板光标正好在该字段上。列表的**选择**会被保存:
  光标在中文界面的默认位置时,每一帧都会保存它。(来自固件反汇编;未在硬件上做断电试验。)请先写入各值,最后再选择
  (`--select` 就是这样做的)。
- 输出开启时更改所选列表的值,输出会立即跟着变(2.0 V 立即变为 2.4 V)。如果列表 N 的 OVP(取上次发送的值)不高于端子
  当前的电压(再加一点余量),`list select N` 会在发送任何内容之前就被拒绝,因为输出会触发保护;请先提高该列表的 OVP。
- 截止功能是**无声的**:输出只是关闭,电源**不会**鸣叫,这与保护触发不同。设置 `--cut-current 0.1` 且没有电流时,
  输出在约 5.5 到 6 s 后关闭。在串口上它看起来就是普通的"输出关闭";如果输出是本工具打开的,`status` 会加一条提示,
  把截止列为可能的原因之一。
- 截止时间比较的是自上电以来(或自上一次时间截止、统计清零以来)**累计的开启时间**,而不是自上次 `on` 以来的时间。
  截止时间设为 10 s 时,设置之后的第一次 `on` 立刻就被截止,因为计数器里已经有自上电以来累计的开启时间;时间截止之后
  计数器重新开始,下一次 `on` 保持了 9 到 10 s。

### 开机、稳定与中文界面

`on` 会等到输出电压建立。当 CV 读数等于数字界面的 VSET,或等于所选列表的 VSET(都取上次发送的值)时,它就认为输出已稳定,
所以在中文界面上 `on` 接受所选列表的电压(实测:2.0 V 的列表读到 1.9975 V)。如果读数两者都不是,但软启动后 1 s 时
CV 读数稳定,`on` 会返回并附上一条说明。如果等待结束时输出标志仍为关闭,说明里会把中文界面的截止功能列为可能的原因之一
(截止是无声的,开启时间自上电起累计)。接 CC 负载时,`on` 在约 1.7 s 后于 CC 状态稳定。

当输出**已开启但不是本工具打开的**时,`status` 会加一条提示:有人按了前面板的输出键,或是别的程序打开的。此时生效的设置
可能与 `last_sent` 不同。(这条提示是在硬件上遇到这种情况之后加上的:用户按了三次输出键,万用表看到输出开启,
而本工具没有发过任何 `on` 帧。)

### CV 优先还是 CC 优先

`--priority cc`(出厂默认)限制开机瞬间的电流过冲;`--priority cv` 则让电流短暂超过 ISET。实测:开机接入 3 V 恒压负载,
VSET 5 V,ISET 0.5 A,以 20 Hz 轮询电源的回读值:CV 优先时峰值为 0.811 A 和 4.263 V,CC 优先时为 0.541 A 和 3.733 V
(分别超出 ISET 约 60 % 和约 8 %)。CV 优先时,接近 ISET 的 OCP 可能在这段过冲中触发(OCP 需要 100 ms)。
通过串口发送的优先设置断电后丢失,除非之后退出过设置菜单(固件)。

### 用电子负载测试限流

用电子负载测试 CC(ISET)时,请把负载设为**恒压模式**,电压低于 VSET,或者改用真正的电阻。**不要**用恒阻模式:电源需要限流时,
负载的 CR 环路与电源的 CC 环路会互相对抗,电流每隔 0.5 到 2 s 就在约 0 和 1.2 A 之间跳变,而电源大部分时间处于 CV 状态
(万用表上也看到了)。即便如此,在这种状态下 `log` 在 10 s 内记录的电荷量(10 Ohm,VSET 12 V,ISET 1 A)为 0.00152 Ah,
与万用表(平均电流乘以时间:0.00158 Ah)相差在 4 % 以内。在恒压模式下,CC 限流稳定且线性。

`sequence --dry-run` 只对照范围、台面限值和 OVP 余量检查各步骤,不发送任何设置:

```
$ openaxpp300s --sim sequence --steps "5,1,2;9,1,2" --dry-run
{"ok":false,"cmd":"sequence","error":{"code":"UNSAFE_OPERATION","message":"step 2: VSET 9 V needs OVP >= 9.2 V,
 but the OVP sent last is 6 V (4 s ago): the output would trip","hint":"set an OVP above the highest step first:
 openaxpp300s set --ovp 9.21"}}
```

### 监视与记录

`watch` 每个采样输出一行 JSON,结束时输出汇总:

```
{"type":"sample","t":1794040307.969,"vout_v":0.0,"iout_a":0.0,"pout_w":0.0,"vin_v":19.8,"temp_c":28.5,"mode":"OFF","output_on":false,"latency_ms":13.9}
{"ok":true,"cmd":"watch","event":"done","stopped_by":"count","elapsed_s":0.18,"last":{...},"samples":2,"ah":0.0,
 "wh":0.0,"on_s":0.0,"modes":{"OFF":2},"stats":{"vout_v":{"min":0.0,"max":0.0,"mean":0.0},...}}
```

`log` 写入文件(默认 `data/log_<时间>.csv`,或 `.jsonl`),CSV 列为 `pc_time, local_time, vout_v, iout_a, pout_w, vin_v,
temp_c, mode, output_on, ah, wh, step`。电源停止应答时,它最多重连 `--reconnect-for` 这么久(默认 5 分钟;`watch` 为 10 s)。
`watch`、`log` 和 `sequence` 可以在后台运行;`running` 列出它们,`stop` 结束它们。按固件的行为,每次状态查询都会让电源重写一个 EEPROM
字段(选中 DATA 框时除外),所以不要超出需要地提高查询频率(见[安全](#安全))。

## 输出格式

每条命令在 stdout 上输出 JSON,每行一个对象:

- 成功:`{"ok":true,"cmd":"...", ...}`;
- 失败:`{"ok":false,"cmd":"...","error":{"code":"...","message":"...","hint":"..."}}`(没有提示时不含 `hint`),
  同样输出在 stdout,并返回非零退出码;
- `watch` 输出采样行(`"type":"sample"`);`log` 和 `sequence` 输出事件行(`"event":"started"`、`"step"`、
  `"link_lost"` 等);三者都以一行带 `"event":"done"` 的汇总结束。

单位为国际单位制:V、A、W、s、C。时间戳 `t` 是以秒计的 Unix 时间。

## 错误与退出码

| 错误码 | 退出码 | 含义与提示 |
|---|---|---|
| `INVALID_ARG` | 2 | 选项或值错误,或值超出电源范围;提示中给出范围 |
| `NOT_FOUND` | 3 | 没有任何 USB 串口上有 PP300S 应答,或指定的端口不存在;请用串口 Type-C 口连接电源、开机、关闭厂商软件,或用 `--port COMx` 指定 |
| `BUSY` | 4 | 端口被其他程序占用(请关闭厂商上位机、串口终端或 FlyMcu 后重试),或另一条 openaxpp300s 命令占用端口超过了 `--lock-wait` |
| `NOT_APPLIED` | 5 | 电源返回了 ACK,但回读显示没有生效;通常是电源不在对应的界面,或输出端有已充电的电容或电池 |
| `NO_REPLY` | 6 | 无应答:电源未开机、处于 bootloader,或端口不对;请试 `ports --probe` |
| `BAD_REPLY` | 6 | 应答的帧头、帧尾或校验和有误;可能有其他程序也在与电源通信 |
| `PORT_ERROR` | 6 | 串口 I/O 错误(线被拔掉、驱动错误);命令已在重新打开端口后重试过 |
| `UNSAFE_OPERATION` | 7 | 出于安全被拒绝,例如:超过台面限值(或输出升到电压限值以上而被重新关闭),OVP 离 VSET 比余量规则允许的更近,OVP 或 OCP 不高于端子当前读数,OPP 或 OTP 与当前功率或温度相差不到 5 W 或 5 C,或 `raw` 中的寄存器未文档化 |
| (其他) | 1 | 其他任何失败(`INTERNAL`),以及有检查失败的 `selftest` |

应请求停止的 `watch`、`log` 或 `sequence`(`stop`、Ctrl+C)以退出码 0 结束,并给出已有的汇总。其他命令被 Ctrl+C
中断时退出码为 130。

## 台面限值

`openaxpp300s limits --max-voltage 12 --max-current 3 --note "接了什么"` 把最大电压(0 到 30 V)和最大电流
(0 到 10.1 A)保存到 `state/limits.json`。除 `raw --unsafe` 外,它们对每条命令和每次 MCP 调用都有效:

- 超过限值的电压类数值(VSET、OVP、列表的 VSET 和 OVP)和电流类数值(ISET、OCP、列表的 ISET 和 OCP)会被以
  `UNSAFE_OPERATION` 拒绝;
- 上次发送的 VSET 高于电压限值时,`on` 被拒绝;设置了电压限值且列表的电压对工具未知时,选择该列表被拒绝;
- 在 `on` 跟踪输出期间(`--no-wait` 时不检查),以及输出开启时选择列表后的 1 s 内,如果实测电压超过电压限值
  2 % + 50 mV 以上,输出会被立即再次关闭(前面板可能设成了任何值);
- 序列在第一步开始之前逐步检查,运行中的序列每个采样都对照电压限值检查。

```
$ openaxpp300s --sim limits --max-voltage 12 --max-current 3 --note "test board"
{"ok":true,"cmd":"limits","changed":true,"limits":{"max_voltage":12.0,"max_current":3.0,"note":"test board",
 "updated":"2026-11-07 03:31:28"},"file":".../state/limits.json"}
```

(`file` 是完整路径,这里做了缩写。)

`limits --clear` 清除它们。没有设置限值时,只受电源自身的范围约束。让智能体控制电源之前,请按所接负载设置好限值。

## 安全

电源输出的是真实的功率。接负载之前请先读这一节。

- **余量规则。** 工具拒绝低于 `VSET + max(0.2 V, 1.5 %)` 的 OVP,无论它是在同一次调用中给出的,还是上次发送的;
  上次发送的 VSET 和 OVP 不满足这条规则时,`on` 也拒绝打开输出。低于 `ISET + max(0.05 A, 2 %)` 的 OCP 会照常发送,
  但带一条警告(负载一把输出拉进 CC 就会触发);请保持在这个值之上。OVP 规则的原因来自实测:VSET 3.00 V、
  OVP 3.00 V 时,电压从 2 V 阶跃上升时输出触发了保护;OVP 2.00 V、VSET 3.00 V 时,输出上升时触发了保护。
  设定值阶跃会有一点过冲(曾出现 1.00 V 设定读到 1.0068 V)。
- **绝不要触发保护。** 保护触发会关闭输出并响起报警。报警和触发标志**只能靠按一下前面板按键清除**;电源无法报告报警状态,
  在串口上一次触发看起来就像普通的"输出关闭"(开关标志变为 0)。触发之后,远程 `on` 仍然可以再次打开输出,
  但报警会一直响到有人按键。每次更改之后都要检查 `status`。
- **电源无法报告自己的设置。** 链路上只有实测输出和一个开关标志,没有设定值、界面、光标和报警。工具会记住自己发送过什么
  (`status` 中的 `last_sent`),而前面板可能在那之后改动过。`set` 会拒绝不高于端子当前读数(再加一点余量)的
  OVP 或 OCP,除非同一次调用降低了设定值(见[命令行参考](#命令行参考))。
- **清楚接了什么。** `on` 之后出现的电压是电源当前生效的那个值,在中文界面上就是所选的参数列表。打开输出后请检查 `vout_v`。
- **截止功能是无声的。** 在中文界面上,截止电流和截止时间关闭输出时不会报警。所以一次无声的"输出关闭"可能是截止,
  而不是保护触发,也不是某个程序所为;截止时间统计的是自上电以来累计的开启时间(见[命令行参考](#命令行参考))。
- **命令运行期间保持设置菜单关闭。** 进入机上设置菜单会关闭一次输出。菜单打开期间,链路上的测量值是冻结的,OPP 和 OTP
  不被检查,远程 `on` 可能在调节被冻结的情况下打开输出。菜单打开时不要使用本工具。(来自固件反汇编,未在硬件上试过。)
- **EEPROM 磨损与 DATA 框。** 电源在收到**每一个有效帧时**(包括状态查询)都会把光标所在的前面板字段保存到 EEPROM
  (来自固件反汇编,写入本身尚未在硬件上观察到;见[状态查询真的会写 EEPROM 吗?](#状态查询真的会写-eeprom-吗))。光标在 V-SET 上(开机默认位置)时,以厂商上位机每 100 ms 一次的轮询速率,
  相当于每秒约 10 次写入 VSET 字节:约 28 小时一百万次,接近 24Cxx 器件典型的约 1e6 次擦写额定值(实际器件及其额定值未知)。
  EEPROM 使用固定地址,没有磨损均衡。**小技巧:**在数字或波形界面上,把前面板光标移到 **DATA** 框(设置栏的最后一个框,
  在 REV 之后);选中它时,帧不保存任何内容(固件)。选中 DATA 时转动旋钮会清除 Ah、Wh 和时间统计(厂商说明书),
  所以请不要碰旋钮。`watch` 和 `log` 默认每秒查询一次;不要超出需要地加快。
- **断电后设置的保留情况**(来自固件反汇编)。通过串口发送的值只有一部分会被保存。VSET、ISET、OVP、OCP、DELAY 和 REV
  只有在帧到达时其字段正是光标所在字段才会写入 EEPROM(默认是 VSET);否则断电后就丢失。OPP、OTP 和 CC 优先只有在之后退出
  前面板设置菜单时才会保存。输出状态从不保存;上电后输出为关闭。显示颜色总是被保存。
- **关闭端口是安全的,输出保持原状。** 被中断的命令不会自行关闭输出;`sequence` 例外,它在结束时、收到 `stop` 时或按下
  Ctrl+C 时会关闭输出,除非以 `--end keep` 启动(超过台面限值或 `NOT_APPLIED` 错误之后则总会关闭)。离开时请自己关闭输出。
- **硬件覆盖有限**(见[状态](#状态与已测试的硬件))。在你的电源上测试之前,不要假定触发、过功率或过温的情况与这里写的一致。

## MCP 服务器

`openaxpp300s-mcp` 是一个 MCP 服务器(stdio,只用标准库)。每个工具都在子进程中运行命令行,所以检查、台面限值和
错误码与命令行相同。它没有 `raw` 工具,台面限值只能用命令行修改。添加到 Claude Code:

```
claude mcp add -s user openaxpp300s -- openaxpp300s-mcp
```

如果 Python 的 `Scripts` 文件夹不在 `PATH` 中,请给出可执行文件的完整路径,例如 `<Python>\Scripts\openaxpp300s-mcp.exe`。
在 **PowerShell** 中要给双横线加引号,因为 PowerShell 会吞掉单独的 `--`:

```
claude mcp add -s user openaxpp300s '--' openaxpp300s-mcp
```

对于其他 MCP 客户端,把 `openaxpp300s-mcp`(或 `python -m openaxpp300s.mcp_server`)作为 stdio 服务器启动。较长的任务作为后台任务
(分离的子进程)运行,其输出保存在 `state/mcp_jobs/<job>/` 中,所以服务器重启后仍能看到它们。

12 个工具:

| 工具 | 作用 |
|---|---|
| `status` | 实测输出、输入电压、温度、模式、`output_on`、`last_sent` 和台面限值(只读) |
| `set` | 按安全顺序更改设置,带有命令行的全部拒绝逻辑 |
| `output` | 打开或关闭输出,并等待其跟随 |
| `param_list` | 写入或选择中文界面的参数列表 0..4 之一 |
| `display_colors` | 设置或重置数字界面的颜色 |
| `watch` | 采样输出几秒,返回统计和最后一个样本 |
| `log_start` | 启动后台记录到 CSV 或 JSONL;返回任务 id |
| `sequence_start` | 在后台运行定时电压/电流步骤 |
| `job_status` | 记录或序列任务的状态(只读) |
| `job_stop` | 干净地停止记录或序列任务 |
| `selftest` | 对链路和电源做只读检查 |
| `ports` | 列出串口并探测其中的 USB 口是否有 PP300S |

MCP 服务器既在模拟器上测试过,也在 2026-11-07 对真实电源测试过:`ports`、`status`、`selftest`(9 项检查全部通过)、`set`(先发 OVP 和 OCP,再发设定值;同时降低 VSET 和 OVP 时先发 VSET;VSET 高于上次发送的 OVP 时被拒绝)、`output` 开关、`watch`、`log_start` 配合 `job_status`(返回了 CSV 的最后一行)和 `job_stop`,以及 `sequence_start`(两个步骤,自行结束并关闭了输出)。

## 状态查询真的会写 EEPROM 吗?

按固件代码是会的,但**尚未在硬件上观察到**。证据在固件 V1.4.4 的反汇编中(手工核对过):收到的帧通过校验和与帧尾检查之后,
0x0800090C 到 0x08000910 的代码会把 1 写入 RAM 标志 0x20000216,这发生在 0x08000912 读取寄存器 ID 并分发**之前**。
所以状态查询(ID 0xFF)和其他任何帧一样会置位该标志;它自己的分支(0x08000EC0)只是置位等待应答的标志 0x20000207。
主循环的保存路径(0x08001DEE,跳转到 0x08001F86 处的光标字段保存)只要见到该标志,就把光标所在的前面板字段写入 EEPROM
(数字和波形界面;中文界面有自己的保存路径)。要看到写入本身,需要在 EEPROM 的 I2C 线上接探头。对策并不依赖于此:用前面板光标选中 **DATA** 框(见[安全](#安全)),帧就不会保存任何内容。

## 模拟器

`--sim`(或 `OPENAXPP300S_SIM=1`)用一个虚拟 AX PP300S 替代串口,它遵循固件 V1.4.4 镜像和实测结果:字节解析器
(损坏的帧,或在 0xA5 之后 10 ms 内未收齐的帧会被丢弃;多出的 0xA5 会吞掉下一帧)、除状态查询外每个有效帧的 ACK、
50 ms 的状态节拍、软启动、DELAY 和 REV、带锁存报警的 OVP/OCP/OPP/OTP 触发、数字界面和中文界面(含截止功能)、
带电阻负载的 CV/CC(默认 10 MOhm),以及 EEPROM 写入计数。OVP 以外的触发只依据固件,尚未在硬件上见过。
它的状态是 `state/sim/` 中的一个 JSON 文件,所有进程共享。控制方式:

```
python -m openaxpp300s.sim reset | show | set key=value ... | advance SECONDS | press | audit [--clear]
```

`press` 模拟一次前面板按键(会清除报警),`advance` 把模拟时钟向前拨。`OPENAXPP300S_SIM_DELAY` 缩放模拟的应答延迟;`0` 去掉延迟。
`OPENAXPP300S_NO_SERIAL=1` 使任何真实端口都拒绝打开。

## 测试

```
pip install -e ".[test]"
python -m pytest
```

测试只使用模拟器。`tests/conftest.py` 隔离每个测试:`OPENAXPP300S_SIM=1`、`OPENAXPP300S_NO_SERIAL=1`、
`OPENAXPP300S_SIM_DELAY=0` 和全新的 `OPENAXPP300S_HOME`,所以测试不可能触及真实电源。标记 `hw`(`pytest -m hw`)
留给需要真实电源的测试;目前还没有这样的测试,默认运行也排除它。硬件检查是几个单独的脚本,不属于 pytest 运行。它们需要真实电源,
并且会打开输出;三个 `bench_289` 脚本还要通过 [open289](https://github.com/FrostFlops/open289) 读取 Fluke 289,
两个 CC 脚本还需要电子负载:

- `tests/hw/bench_289.py`:空载电压精度,与 289 对比;
- `tests/hw/bench_289_cc.py`:CV 接恒阻负载,289 串联作电流表(也说明了为什么恒阻模式不适合测试 CC);
- `tests/hw/bench_289_cc_cvload.py`:负载为恒压模式时的 CC 限流:CC 精度、带电流时同时降低 ISET 和 OCP,以及 CV 优先与
  CC 优先的开机对比;
- `tests/hw/bench_chinese.py`:中文界面(列表、`on` 到列表电压、截止功能、被忽略的数字界面寄存器);不需要万用表,
  但需要一个在 3 V 以下几乎不取电流的负载,并且要手动把电源切换到中文界面。

## 文件

| 位置 | 内容 |
|---|---|
| `state/` | 锁(同一时间只有一条命令占用端口)、记住的端口、`last_sent.json`(上次发送的值)、`limits.json`(台面限值)、停止请求、运行中的任务、`mcp_jobs/`、`sim/` |
| `data/` | `log`(以及 MCP 工具 `log_start`)在没有给出路径时写入的日志 |

这些文件夹位于 `$OPENAXPP300S_HOME`(如果设置了);否则在源码目录中(包括 `pip install -e .`)放在源码目录内;
否则对安装后的包,在 Windows 上用 `%LOCALAPPDATA%\openaxpp300s`,其他系统用 `~/.local/share/openaxpp300s`,
所以不会向 `site-packages` 写入任何内容。设置 `OPENAXPP300S_HOME` 可把全部内容移到另一个文件夹。

环境变量:`OPENAXPP300S_PORT`(默认端口)、`OPENAXPP300S_HOME`、`OPENAXPP300S_SIM`、`OPENAXPP300S_NO_SERIAL`、
`OPENAXPP300S_SIM_DELAY`。

## 精度

参考仪器:Fluke 289(电压用 10 MOhm 输入,电流用串联的 A 插孔)。下面各表均来自 2026-11-07 的第二轮测试,
当时电源由 Type-C 电源输入口上的 65 W USB-PD 充电器(约 19.8 V)供电。

### 电压,空载

输出开启、空载,直流电压,每个点稳定 3 s(第二轮;第一轮的结果一致,0.5 V 到 15 V,+1.7 到 +7.0 mV):

| VSET (V) | 万用表 (V) | 万用表减 VSET (mV) | 电源回读减万用表 (mV) |
|---|---|---|---|
| 0.5 | 0.5026 | +2.6 | -8.6(平均值包含建立过程) |
| 1.0 | 1.0054 | +5.4 | -2.4 |
| 2.5 | 2.50415 | +4.2 | -2.8 |
| 3.3 | 3.3039 | +3.9 | -1.8 |
| 5.0 | 5.0028 | +2.8 | -1.1 |
| 9.0 | 9.005 | +5.0 | -2.2 |
| 12.0 | 12.007 | +7.0 | -3.6 |
| 15.0 | 15.006 | +6.0 | -2.4 |
| 20.0 | 20.003 | +3.0 | -0.4 |
| 24.0 | 24.006 | +6.0 | -3.5 |
| 30.0 | 30.005 | +5.0 | -2.9 |

所有点的设定值都在规格的 0.1 % + 5 mV 之内(12 V 时 +7.0 mV,允许 17 mV)。回读也是如此,只有 0.5 V 这一点的平均值例外
(-8.6 mV,限值为 5.5 mV),它包含了建立过程。空载下由 20 V 输入升压到 20、24、30 V 工作正常。

### 限流(CC),负载为恒压模式

负载保持在 3.0 V,VSET 为 5 V,所以每一步电源都处于 CC 状态:

| ISET (A) | 万用表 (A) | 万用表减 ISET (mA) | 电源回读减万用表 (mA) | 万用表波动 (mA) |
|---|---|---|---|---|
| 0.10 | 0.0864 | -13.6 | -6.2 | 2.7 |
| 0.25 | 0.2366 | -13.4 | -7.3 | 0.8 |
| 0.30 | 0.2867 | -13.3 | -2.7 | 0.8 |
| 0.50 | 0.4864 | -13.6 | -2.6 | 0.4 |
| 1.00 | 0.9862 | -13.8 | -2.2 | 0.5 |

CC 稳定且线性,但每个设定值实际输出都比 ISET **少约 13.5 mA**:这是恒定的偏移,超出了规格的 0.1 % + 5 mA(0.5 A 时为
5.5 mA)。问题出在**电源**,不在工具:工具发送的原始值是正确的,最可能的原因是这台机器的电流校准偏移(说明书写明电流校准需要
10 A 负载,不建议用户自行操作)。其他机器可能不同。如果依赖 ISET,请把这个偏移考虑进去;回读更接近万用表(比万用表低 2 到 7 mA),
在小电流时噪声更大(ISET 0.1 时为 0.065 到 0.084 A)。

### CV 状态下的带载电流

5 V 接 10 Ohm 负载(ISET 1 A,所以是 CV,0.5 A):电源回读 0.4943 A,万用表 0.4940 A(+0.3 mA);第二次为 0.4919 A 对
0.4938 A。同一负载在恒阻模式下、电源处于限流时记录的电荷量,见[用电子负载测试限流](#用电子负载测试限流)。
这些测试最大只到约 1.2 A 和 15 W。

### 时序

- 电源在约 12 到 14 ms 内返回帧的 ACK(USB 延迟)。状态应答需要 15 到 55 ms,中位数约 48 ms(它在电源的下一个 50 ms 节拍上发出)。
- DELAY = REV = 0 时,`on` 之后输出标志立即置位;电压在约 0.011 V 停留约 1.1 s(软启动),然后在约 0.3 s 内稳定。输出开启时,
  VSET 阶跃之后约 0.2 s 稳定(从 1.0 V 降到 0.5 V 时为 0.94 s)。
- `off`:接 10 MOhm 万用表时,电压在约 0.3 s 内从 2 V 降到 0.13 V。
- DELAY 3 s:输出在 `on` 命令之后 4.39 s 打开(3 s 加软启动;第二轮为 4.23 s)。REV 2 s:约 2 s 后打开,然后每 2 s 翻转一次。
- 校验和错误的帧得不到应答,拆开发送且超过 10 ms 的帧也得不到应答;帧前面含 0xA5 的杂乱字节会吞掉该帧;
  一次写入中的两个查询只得到一个应答。工具把每一帧用一次写入发出,一次只发一帧,没有应答的帧会重发。

## 这里没有的内容

- 不读取电源的设置、界面、光标或报警:固件不报告它们。
- 没有固件升级和烧录;本工具不使用 FlyMcu。
- 不能远程按前面板按键(固件没有对应的串口命令)。
- 没有蓝牙或其他链路;只支持 USB 串口。

## 许可证

GPL-3.0-only。见 `LICENSE`。

与 AxinDIY 没有关联。厂商的协议文档和手册仅供参考阅读;其文字和图片都不是本项目的一部分。
